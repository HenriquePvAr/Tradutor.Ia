"""Runtime bridge between the custom local frontend and the persistent job queue.

The browser never executes the pipeline directly, and neither does this module: it
validates UI payloads, records jobs in the persistent SQLite store, and exposes
serializable state read back from that store. The pipeline runs in the independent
worker/runner processes, so closing or restarting the UI never touches a running job.
The local history/profile persistence is unchanged.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from community_auth import RequestPrincipal, bind_is_loopback, peer_is_loopback
from job_store import JobStatus, JobStore
from output_manifest import MANIFEST_FILENAME, load_verified_run_manifest, sanitize_source_url
from ui_helpers import (
    OUTPUT_ROOT,
    REPO_ROOT,
    build_run_command,
    clean_url,
    env_status,
    sanitize_diagnostic_text,
    sanitize_output_name,
    suggest_chapter_details,
)
from ui_history import UIHistoryStore, utc_now


PROFILE_PATH = REPO_ROOT / ".cache" / "ui_profile.json"
PROFILE_MEDIA_DIR = REPO_ROOT / ".cache" / "ui_profile"
JOBS_DB_PATH = REPO_ROOT / ".cache" / "runtime" / "jobs.sqlite3"
JOB_LOG_DIR = REPO_ROOT / ".cache" / "runtime" / "logs"
MAX_LOG_LINES = 3000
SOURCE_ANALYSIS_TIMEOUT_SECONDS = 180
_UI_STAGE_LABELS = {
    "created": "Preparando",
    "source_validation": "Validando fonte",
    "source_analysis": "Analisando fonte",
    "validating_local_source": "Validando pasta local",
    "creating_snapshot": "Criando cópia segura",
    "browser_loading": "Abrindo leitor",
    "collecting_candidates": "Coletando páginas",
    "clustering_candidates": "Validando páginas",
    "awaiting_source_review": "Aguardando revisão das páginas",
    "downloading_pages": "Baixando imagens",
    "validating_pages": "Validando imagens",
    "download": "Baixando imagens",
    "smart_split": "Reconstrução",
    "ocr": "OCR",
    "translate": "Tradução NVIDIA",
    "render": "Renderização",
    "pdf": "Geração de PDF",
    "final": "Finalizado",
}
MAX_PROFILE_MEDIA_BYTES = 12 * 1024 * 1024
PROFILE_MEDIA_TYPES = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".mp4": "video/mp4",
    ".png": "image/png",
    ".webm": "video/webm",
    ".webp": "image/webp",
}


UNAVAILABLE_DURATION = "Tempo indisponível"


def local_folder_ui_allowed(*, bind_host: str, peer_host: str) -> bool:
    """Return whether a browser may submit a local filesystem source.

    A folder path is intrinsically machine-local data.  Both the server binding and the
    connected browser must therefore be loopback addresses; checking only the request peer
    would still allow a remotely reachable server to turn an exposed API into a local-file
    reader.
    """

    return bind_is_loopback(bind_host) and peer_is_loopback(peer_host)

# A timestamp far above plausible epoch-seconds is a milliseconds value written by mistake;
# anything below this is treated as seconds. (2001-09-09 in seconds / 1970 in ms.)
_MS_THRESHOLD = 1e11


def _normalize_epoch(value: Any) -> float | None:
    """Epoch seconds, or None when the value cannot be trusted as a timestamp."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0 or number != number or number in (float("inf"), float("-inf")):
        return None
    if number >= _MS_THRESHOLD:      # milliseconds written where seconds were expected
        number /= 1000.0
    return number


def _format_seconds(value: Any) -> str:
    """34s | 12min 34s | 1h 12min. None/invalid never renders as a number."""
    if value is None:
        return UNAVAILABLE_DURATION
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return UNAVAILABLE_DURATION
    if seconds != seconds or seconds in (float("inf"), float("-inf")) or seconds < 0:
        return UNAVAILABLE_DURATION
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}min"
    if minutes:
        return f"{minutes}min {secs:02d}s"
    return f"{secs}s"


def _epoch_to_iso(value: Any) -> str:
    try:
        if not value:
            return ""
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return ""


def _duration(job: dict[str, Any] | None, *, live: bool = False) -> float | None:
    """Elapsed seconds, or None when it cannot be computed honestly.

    The clock only advances while the job is *proven* live. For anything else the end is a
    stored timestamp (finished_at, then heartbeat_at, then updated_at), so an abandoned job
    freezes at its last sign of life instead of counting up forever.
    """
    if not job:
        return None
    started = _normalize_epoch(job.get("started_at"))
    if started is None:
        return None
    if live:
        end: float | None = time.time()
    else:
        end = next(
            (stamp for stamp in (
                _normalize_epoch(job.get("finished_at")),
                _normalize_epoch(job.get("heartbeat_at")),
                _normalize_epoch(job.get("updated_at")),
            ) if stamp is not None),
            None,
        )
    if end is None:
        return None
    elapsed = end - started
    return None if elapsed < 0 else elapsed        # negative = corrupt clock, never shown


def _run_git(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _current_commit() -> str:
    return _run_git("rev-parse", "HEAD")


def _current_branch() -> str:
    return _run_git("rev-parse", "--abbrev-ref", "HEAD")


def _runner_still_alive(job: dict[str, Any]) -> bool:
    """True when the job's recorded runner process is still the live runner for it."""
    try:
        import process_tree
    except Exception:  # noqa: BLE001 - process checks are best-effort in the UI
        return False
    return process_tree.is_alive(
        job.get("runner_pid"),
        create_time=job.get("runner_create_time"),
        substrings=["job_runner.py", job["id"]],
    )


def _read_env_file() -> dict[str, str]:
    path = REPO_ROOT / ".env"
    values: dict[str, str] = {}
    try:
        for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    except OSError:
        pass
    return values


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "não instalado"


def _profile_default() -> dict[str, Any]:
    return {
        "display_name": "você",
        "title": "",
        "pronouns": "",
        "status": "online",
        "status_text": "",
        "bio": "",
        "avatar_mode": "letter",
        "avatar_color": "#c5372c",
        "banner": "ink",
        "avatar_media_path": "",
        "avatar_media_type": "",
        "avatar_media_name": "",
        "avatar_media_size": 0,
        "banner_media_path": "",
        "banner_media_type": "",
        "banner_media_name": "",
        "banner_media_size": 0,
        "created_at": utc_now(),
    }


class UiBridge:
    """Own the single local worker, queue and persistent UI state."""

    def __init__(self) -> None:
        self.history_store = UIHistoryStore()
        self.history = self.history_store.discover_outputs()
        self.profile = self._load_profile()
        self.store = JobStore(JOBS_DB_PATH)
        self.history_revision = 1
        # A job left in flight by a crash must not come back as PROCESSANDO after a restart.
        self._recover_staged_source_analyses()
        self.reconcile_orphans()

    # ---- orphan reconciliation ----------------------------------------------
    def _recover_staged_source_analyses(self) -> None:
        """A staging analysis belongs to the UI process, never to the worker.

        On a fresh UI instance no in-memory source-analysis coroutine can own a persisted
        staging row. Freeze it instead of showing an indefinitely live browser analysis.
        """
        try:
            staged = self.store.list_jobs(statuses=[JobStatus.STAGING], limit=None)
        except Exception:  # noqa: BLE001 - startup remains best effort
            return
        for job in staged:
            try:
                self.store.transition(
                    job["id"], JobStatus.FAILED,
                    reason_code="source_analysis_interrupted", stage="source_analysis",
                    error_type="source_analysis", error_message="source_analysis_interrupted",
                )
            except Exception:  # noqa: BLE001 - concurrent submit/cancel wins
                continue

    def _completion_evidence(self, job: dict[str, Any]) -> str:
        """Complete proof the run really finished: exit code 0, a manifest and a real PDF.

        A vanished PID is never proof of success on its own — a killed process and a
        completed one look identical from the outside.
        """
        if job.get("exit_code") not in (0, "0"):
            return ""
        output_dir = job.get("output_dir")
        if not output_dir:
            return ""
        base = Path(output_dir)
        try:
            # The manifest is part of the output contract.  Do not accept the old
            # ad-hoc filename or merely well-formed JSON: only a schema-verified
            # run manifest proves that this output belongs to a completed run.
            if not (base / MANIFEST_FILENAME).is_file():
                return ""
            manifest = load_verified_run_manifest(base)
            if not manifest:
                return ""
            # A PDF that exists but is empty/truncated is not evidence either.
            pdfs = [p for p in base.rglob("*.pdf") if p.is_file() and p.stat().st_size > 1024]
            if not pdfs:
                return ""
            with open(pdfs[0], "rb") as handle:
                if handle.read(5) != b"%PDF-":
                    return ""
            status = str(manifest.get("final_status") or "")
            if status == "finished" and manifest.get("quality_passed") is True:
                return JobStatus.FINISHED
            if status == "review_required" or (
                status == "finished" and manifest.get("quality_passed") is False
            ):
                return JobStatus.REVIEW_REQUIRED
            return ""
        except OSError:
            return ""

    def _success_evidence(self, job: dict[str, Any]) -> bool:
        """Backward-compatible boolean: any proven completed terminal outcome."""
        return bool(self._completion_evidence(job))

    def reconcile_orphans(self) -> list[dict[str, str]]:
        """Freeze in-flight jobs whose runner process is gone. Outputs are never touched."""
        reconciled: list[dict[str, str]] = []
        try:
            in_flight = self.store.list_jobs(statuses=list(JobStatus.IN_FLIGHT), limit=None)
        except Exception:  # noqa: BLE001 - the UI must start even with an unreadable store
            return reconciled
        for job in in_flight:
            if _runner_still_alive(job):
                continue                      # a real, verified process owns this job
            job_id = job["id"]
            frozen = _normalize_epoch(job.get("heartbeat_at")) or \
                _normalize_epoch(job.get("updated_at")) or time.time()
            if job.get("cancel_requested"):
                target, reason = JobStatus.CANCELLED, "cancelled_process_not_found"
            elif completion := self._completion_evidence(job):
                target = completion
                reason = (
                    "quality_review_completed_before_shutdown"
                    if target == JobStatus.REVIEW_REQUIRED else "completed_before_shutdown"
                )
            else:
                target, reason = JobStatus.INTERRUPTED, "process_not_found"
            try:
                self.store.transition(job_id, target, interrupted_reason=reason,
                                      finished_at=frozen, recoverable=1)
            except Exception:  # noqa: BLE001 - a concurrent owner wins; leave the row alone
                continue
            reconciled.append({"job_id": job_id, "status": target, "reason": reason})
        return reconciled

    # ---- job <-> UI record mapping -----------------------------------------
    def _job_record(self, job: dict[str, Any] | None) -> dict[str, Any] | None:
        if not job:
            return None
        config = job.get("configuration") or {}
        source_type = str(job.get("source_type") or config.get("source_type") or "url")
        local_summary = config.get("local_source_summary")
        if not isinstance(local_summary, dict):
            local_summary = {}
        return {
            "id": job["id"],
            "run_id": job.get("run_id"),
            "job_id": job["id"],
            "chapter_name": config.get("chapter_name") or job.get("series_title") or job.get("series_slug") or "",
            "slug": Path(job.get("output_dir") or "").name,
            # The queue retains the raw URL only to execute a remote job. The browser-facing
            # record is diagnostic data and must not echo signed query material. Local jobs
            # carry no source path in their row; their snapshot reference stays opaque.
            "url": (
                sanitize_source_url(str(job.get("source_url") or ""))
                if source_type != "local_folder" else ""
            ),
            "source_type": source_type,
            "source_label": (
                str(local_summary.get("folder_name") or "Pasta local")[:120]
                if source_type == "local_folder" else ""
            ),
            "mode": config.get("mode") or "fast",
            "scope": "full" if config.get("full", True) else "partial",
            "cache_mode": "force" if config.get("force") else "cache",
            "status": job.get("status"),
            "output_folder": job.get("output_dir") or "",
            "pdf_path": job.get("pdf_path") or "",
            "quality_report_path": job.get("quality_report_path") or "",
            "attempt": job.get("attempt") or 1,
            "recoverable": bool(job.get("recoverable")),
            "interrupted_reason": job.get("interrupted_reason") or "",
            "reason_code": job.get("reason_code") or "",
            "stage": str(job.get("stage") or "created"),
            "error_message": sanitize_diagnostic_text(job.get("error_message") or ""),
            "source_analysis": job.get("source_analysis") or {},
            "source_selection": job.get("source_selection") or {},
            "source_provenance": self._public_job_source_provenance(job),
            "started_at": _epoch_to_iso(job.get("started_at")),
            "finished_at": _epoch_to_iso(job.get("finished_at")),
            "total_seconds": _duration(job),
        }

    @staticmethod
    def _public_job_source_provenance(job: dict[str, Any]) -> dict[str, Any]:
        """Return only scalar, path-free source evidence for browser/history records."""

        def identifier(value: Any) -> str:
            text = str(value or "").strip()
            return text if re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", text) else ""

        def count(value: Any) -> int:
            try:
                result = int(value)
            except (TypeError, ValueError):
                return 0
            return result if 0 <= result <= 10_000 else 0

        def score(value: Any) -> float | None:
            try:
                result = float(value)
            except (TypeError, ValueError):
                return None
            return result if 0.0 <= result <= 1.0 else None

        return {
            "source_type": "local_folder" if job.get("source_type") == "local_folder" else "url",
            "adapter_name": identifier(job.get("adapter_name")),
            "adapter_version": identifier(job.get("adapter_version")),
            "transport_name": identifier(job.get("transport_name")),
            "score": score(job.get("source_score")),
            "candidate_count": count(job.get("candidate_count")),
            "accepted_page_count": count(job.get("accepted_count")),
            "rejected_page_count": count(job.get("rejected_count")),
            "reason_code": identifier(job.get("reason_code")),
        }

    @staticmethod
    def _is_translation_job(job: dict[str, Any] | None) -> bool:
        if not job:
            return False
        # Jobs created before job_type was introduced are legacy translation jobs.
        job_type = str((job.get("configuration") or {}).get("job_type") or "translation")
        return job_type == "translation"

    def _displayed_source_review(self) -> dict[str, Any] | None:
        """The one pending review currently shown by the single-review UI surface."""
        return max(
            (job for job in self.store.list_jobs(
                statuses=[JobStatus.AWAITING_SOURCE_REVIEW], limit=None)
             if self._is_translation_job(job)),
            key=lambda job: float(job.get("updated_at") or 0), default=None,
        )

    def bootstrap(self, cursor: int = 0) -> dict[str, Any]:
        return {
            **self.runtime_state(cursor),
            "history": self._history_payload(),
            "profile": self._profile_payload(),
            "settings": self.settings(),
            "community": {"available": False, "posts": 0},
        }

    def _history_payload(self) -> list[dict[str, Any]]:
        # Terminal jobs from the store, plus legacy output discovery for old runs.
        self.history = self.history_store.discover_outputs()
        return self.history

    def runtime_state(self, cursor: int = 0) -> dict[str, Any]:
        # Reconcile on every poll, not only at startup: a runner can die at any moment and
        # the UI must stop claiming PROCESSANDO within one polling interval.
        self.reconcile_orphans()
        active_jobs = self.store.list_jobs(statuses=JobStatus.IN_FLIGHT, limit=None)
        active = max(
            (job for job in active_jobs if self._is_translation_job(job)),
            key=lambda job: float(job.get("claimed_at") or 0),
            default=None,
        )
        waiting = self._displayed_source_review()
        staging_jobs = self.store.list_jobs(statuses=[JobStatus.STAGING], limit=None)
        staging = max(
            (job for job in staging_jobs if self._is_translation_job(job)),
            key=lambda job: float(job.get("updated_at") or 0), default=None,
        )
        present_job = active or waiting or staging
        record = self._job_record(present_job)
        status = (present_job["status"] if present_job else "ready")
        stage = str((present_job or {}).get("stage") or "created")
        current = int((present_job or {}).get("progress_current") or 0)
        total = int((present_job or {}).get("progress_total") or 0)
        # The counter only belongs to the stage that produced it; otherwise a download's
        # 99/99 would be rendered under the translation label.
        counter_stage = str((present_job or {}).get("progress_counter_stage") or "")
        counter_matches = (not counter_stage) or counter_stage == stage
        if not counter_matches:
            current = total = 0
        stage_fraction = (current / total) if current and total else None
        running = status in JobStatus.IN_FLIGHT or status == JobStatus.STAGING
        # "running" now means a verified live process, because reconcile_orphans() already
        # demoted every in-flight job whose runner is gone.
        # Staging is owned by this async request and bounded by ``_run_source_analysis``;
        # it is therefore live while present. Worker stages still require a verified runner.
        live = running and (
            bool(staging) or (bool(active) and _runner_still_alive(active))
        )
        elapsed = _duration(present_job, live=live) if present_job else None
        last_update = _normalize_epoch((present_job or {}).get("updated_at"))
        stale_updates = bool(
            running and last_update is not None and (time.time() - last_update) > 120
        )
        queued = [
            self._job_record(job)
            for job in self.store.list_jobs(statuses=[JobStatus.QUEUED])
            if self._is_translation_job(job)
        ]
        resumable = [self._job_record(job) for job in self.store.list_jobs(
            statuses=[JobStatus.INTERRUPTED, JobStatus.RESUMABLE])
            if self._is_translation_job(job)]
        worker = self.store.healthy_worker(stale_seconds=15)
        logs = self._tail_job_logs(active, cursor)
        # A queued job is pending work, not "pronto". Reporting it as ready is what made a
        # successful submit look like nothing happened.
        pending = bool(queued) and not running and waiting is None and staging is None
        if pending:
            status = JobStatus.QUEUED
        blocked = pending and not worker
        return {
            "status": status if status in JobStatus.ALL else "ready",
            "pending": pending,
            "blocked": blocked,
            "blocked_reason": "worker_offline" if blocked else "",
            "active": record,
            "latest": record or self._job_record(self._latest_terminal_job()),
            "progress": {
                "stage": _UI_STAGE_LABELS.get(stage, stage),
                "stage_key": stage,
                "current": current,
                "total": total,
                "fraction": stage_fraction,
                "indeterminate": stage_fraction is None and running,
                "pages": current,
                "groups": int((present_job or {}).get("progress_current") or 0),
                "errors": 0,
                "last_message": (present_job or {}).get("progress_message") or "",
                "elapsed_seconds": elapsed,
                "elapsed_label": _format_seconds(elapsed),
                "updated_at": last_update,
                "stale": stale_updates,
                "stale_label": "Sem atualização recente" if stale_updates else "",
                "live": live,
            },
            "logs": logs["entries"],
            "log_cursor": logs["cursor"],
            "queue": queued,
            "queue_running": running or pending,   # a queued job is still "em andamento"
            "resumable": resumable,
            "source_review": self._job_record(waiting),
            "worker": {
                "online": bool(worker),
                "worker_id": (worker or {}).get("worker_id", ""),
                "pid": (worker or {}).get("pid", 0),
            },
            "history_revision": self.history_revision,
        }

    @staticmethod
    def _is_presentable_result(job: dict[str, Any]) -> bool:
        """A job worth showing as "the last result".

        Smoke/fixture rows (from authorized smokes) are real jobs but not real chapters:
        they are flagged in configuration, and their output directory does not exist. Either
        signal disqualifies them, so a fixture can never be presented as a translation.
        """
        config = job.get("configuration") or {}
        if config.get("fixture") or config.get("smoke"):
            return False
        # A cancelled or failed run produced no result: showing it as "the last result" is
        # how a stopped job kept appearing as a finished chapter.
        if job.get("status") not in (JobStatus.FINISHED, JobStatus.REVIEW_REQUIRED):
            return False
        output_dir = str(job.get("output_dir") or "")
        return bool(output_dir) and Path(output_dir).is_dir()

    def _latest_terminal_job(self) -> dict[str, Any] | None:
        jobs = self.store.list_jobs(statuses=list(JobStatus.TERMINAL), limit=None)
        return next((job for job in jobs
                     if self._is_translation_job(job) and self._is_presentable_result(job)), None)

    def _tail_job_logs(self, active: dict[str, Any] | None, cursor: int) -> dict[str, Any]:
        if not active or not active.get("log_path"):
            return {"entries": [], "cursor": int(cursor or 0)}
        path = Path(active["log_path"])
        if not path.is_file():
            return {"entries": [], "cursor": int(cursor or 0)}
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return {"entries": [], "cursor": int(cursor or 0)}
        start = int(cursor or 0)
        entries = [
            {"seq": index + 1, "time": "", "kind": "info", "text": sanitize_diagnostic_text(line)}
            for index, line in enumerate(lines[-MAX_LOG_LINES:])
            if index + 1 > start
        ]
        return {"entries": entries, "cursor": len(lines[-MAX_LOG_LINES:])}

    def settings(self) -> dict[str, Any]:
        values = _read_env_file()
        env = env_status()
        model = os.getenv("NVIDIA_TRANSLATION_MODEL") or values.get(
            "NVIDIA_TRANSLATION_MODEL", "nvidia/nemotron-3-super-120b-a12b"
        )
        return {
            "env_exists": env["env_exists"],
            "nvidia_configured": env["nvidia_configured"],
            "translation_mode": os.getenv("TRANSLATION_MODE") or values.get("TRANSLATION_MODE", "nvidia"),
            "translation_model": model,
            "translation_batch_size": os.getenv("NVIDIA_TRANSLATION_BATCH_SIZE")
            or values.get("NVIDIA_TRANSLATION_BATCH_SIZE", "20"),
            "max_requests_per_minute": os.getenv("NVIDIA_MAX_REQUESTS_PER_MINUTE")
            or values.get("NVIDIA_MAX_REQUESTS_PER_MINUTE", "20"),
            "ocr_engine": os.getenv("OCR_ENGINE") or values.get("OCR_ENGINE", "paddle"),
            "rapidocr_min_confidence": os.getenv("RAPIDOCR_MIN_CONFIDENCE")
            or values.get("RAPIDOCR_MIN_CONFIDENCE", "0.55"),
            "ocr_parallel": os.getenv("OCR_PARALLEL") or values.get("OCR_PARALLEL", "False"),
            "ocr_text_repair_mode": os.getenv("OCR_TEXT_REPAIR_MODE")
            or values.get("OCR_TEXT_REPAIR_MODE", "conservative"),
            "translate_sfx": os.getenv("TRANSLATE_SFX") or values.get("TRANSLATE_SFX", "False"),
            "prioritize_enclosed_text": os.getenv("PRIORITIZE_ENCLOSED_TEXT")
            or values.get("PRIORITIZE_ENCLOSED_TEXT", "True"),
            "rapidocr_available": bool(
                importlib.util.find_spec("rapidocr_onnxruntime")
                or importlib.util.find_spec("rapidocr")
            ),
            "paddle_available": bool(importlib.util.find_spec("paddleocr")),
            "python_version": platform.python_version(),
            "nicegui_version": _package_version("nicegui"),
            "rapidocr_version": _package_version("rapidocr-onnxruntime"),
            "paddleocr_version": _package_version("paddleocr"),
            "port": int(os.getenv("TRADUTOR_UI_PORT", "8080")),
        }

    async def start(
        self,
        payload: dict[str, Any],
        *,
        principal: RequestPrincipal | None = None,
        local_folder_allowed: bool = False,
    ) -> dict[str, Any]:
        source_type = self._requested_source_type(payload)
        if source_type == "local_folder":
            # The HTTP boundary calculates this from both the bind address and the peer.
            # Direct callers are deliberately denied by default so a future endpoint cannot
            # accidentally turn a local path into a remotely reachable capability.
            if local_folder_allowed is not True:
                raise ValueError("local_folder_requires_loopback_ui")
            return await self._start_local_folder(payload, principal=principal)
        # A double click (or a retried request) must not queue the same chapter twice.
        duplicate = self._pending_duplicate(payload)
        if duplicate:
            result = {"ok": True, "duplicate": True, "run_id": duplicate.get("run_id") or "",
                      "job_id": duplicate["id"]}
            if duplicate.get("status") == JobStatus.AWAITING_SOURCE_REVIEW:
                result["awaiting_source_review"] = True
                result["analysis"] = duplicate.get("source_analysis") or {}
                result["source_provenance"] = self._public_job_source_provenance(duplicate)
                return result
            result["worker"] = self.ensure_worker()
            return result
        # Persist a controllable staging row before the browser work starts. The analysis is
        # sent to a worker thread so the async UI can keep polling and cancel it; no OCR,
        # downloader, output folder or pipeline worker starts at this point.
        normalized = self._normalize_payload(payload, require_environment=False)
        from universal_chapter_adapter import (
            REVIEW_REQUIRED_MEDIUM_CONFIDENCE,
            SUPPORTED_GENERIC_HIGH_CONFIDENCE,
            SUPPORTED_SPECIFIC_ADAPTER,
        )

        job = self._create_job(
            {**payload, "source_candidate_ids": []}, principal=principal,
            require_environment=False, initial_status=JobStatus.STAGING,
            source_analysis={"adapter": "", "outcome": "source_analysis_pending", "accepted": []},
        )
        self.store.update_fields(
            job["id"], source_type="url", stage="source_analysis", reason_code="",
            started_at=time.time(), heartbeat_at=time.time(),
        )

        def analysis_cancelled() -> bool:
            current = self.store.get_job(job["id"])
            # ``asyncio.wait_for`` cannot kill a running thread. Once this staging row moves
            # to *any* other state (timeout, cancellation or recovery), the browser helper
            # must observe cancellation at its next safe boundary and tear itself down.
            return (
                not current
                or current.get("status") != JobStatus.STAGING
                or bool(current.get("cancel_requested") if current else False)
            )

        try:
            analysis = await self._run_source_analysis(
                normalized["url"], cancel_check=analysis_cancelled)
        except asyncio.CancelledError:
            current = self.store.get_job(job["id"])
            if current and current.get("status") == JobStatus.STAGING:
                self.store.transition(
                    job["id"], JobStatus.CANCELLED, reason_code="cancelled",
                    interrupted_reason="source_analysis_request_cancelled",
                )
            raise
        except asyncio.TimeoutError as exc:
            from chapter_source import SOURCE_NOT_READY, SourceError

            source_error = SourceError(SOURCE_NOT_READY, "source_analysis_timeout")
            current = self.store.get_job(job["id"])
            if current and current.get("status") == JobStatus.STAGING:
                self.store.transition(
                    job["id"], JobStatus.FAILED, reason_code=source_error.code,
                    stage="source_analysis", error_type="source_analysis",
                    error_message=source_error.code,
                )
            raise source_error from exc
        except Exception as exc:
            # Remote source outcomes (challenge/access/auth/etc.) deserve an auditable,
            # frozen terminal record.  Programming errors still propagate rather than being
            # relabelled as an unsupported chapter.
            from chapter_source import SOURCE_NOT_READY, SourceError

            current = self.store.get_job(job["id"])
            if current and current.get("status") == JobStatus.CANCELLED:
                return {"ok": False, "cancelled": True, "run_id": job["run_id"], "job_id": job["id"]}
            if isinstance(exc, SourceError):
                source_error = exc
            else:
                source_error = SourceError(SOURCE_NOT_READY, type(exc).__name__)
            if current and current.get("status") == JobStatus.STAGING:
                self.store.transition(
                    job["id"], JobStatus.FAILED, reason_code=source_error.code,
                    stage="source_analysis", error_type="source_analysis",
                    error_message=source_error.code,
                )
            if isinstance(exc, SourceError):
                raise
            raise source_error from exc
        current = self.store.get_job(job["id"])
        if not current or current.get("status") == JobStatus.CANCELLED:
            return {"ok": False, "cancelled": True, "run_id": job["run_id"], "job_id": job["id"]}
        return self._apply_source_analysis(job, analysis)


    def _apply_source_analysis(self, job: dict[str, Any], analysis: Any) -> dict[str, Any]:
        """Persist an analysis outcome and decide the job's next state.

        Extracted verbatim from the submit path so the identical decision can be made by
        whoever ran the analysis — today the HTTP request, next the worker.
        """
        from universal_chapter_adapter import (
            REVIEW_REQUIRED_MEDIUM_CONFIDENCE,
            SUPPORTED_GENERIC_HIGH_CONFIDENCE,
            SUPPORTED_SPECIFIC_ADAPTER,
        )

        public_analysis = analysis.public()
        provenance = self._remote_source_provenance(public_analysis)
        if self._source_analysis_is_incomplete(public_analysis):
            # Manual review may choose among an uncertain *complete* reader manifest; it is
            # never an override for evidence that the collector already knows is partial.
            #
            # This is analysis coverage, not download coverage. Reporting it as
            # incomplete_download blamed a download that had not been attempted yet — the
            # runner has not even started at this point — and sent the user chasing a
            # network problem for what is a collector that could not see the whole reader.
            from chapter_source import INCOMPLETE_SOURCE_COVERAGE

            job = self.store.transition(
                job["id"], JobStatus.FAILED, reason_code=INCOMPLETE_SOURCE_COVERAGE,
                source_analysis_json=json.dumps(public_analysis, ensure_ascii=False),
                stage="source_analysis", error_type="source_analysis",
                error_message="source_coverage_incomplete", **provenance,
            )
            return {
                "ok": False, "run_id": job["run_id"], "job_id": job["id"],
                "analysis": public_analysis, "reason_code": INCOMPLETE_SOURCE_COVERAGE,
            }
        selected_ids = [candidate.id for candidate in analysis.accepted]
        if analysis.outcome == REVIEW_REQUIRED_MEDIUM_CONFIDENCE:
            job = self.store.transition(
                job["id"], JobStatus.AWAITING_SOURCE_REVIEW,
                reason_code=analysis.outcome,
                source_analysis_json=json.dumps(public_analysis, ensure_ascii=False),
                stage="awaiting_source_review",
                **provenance,
            )
            return {"ok": True, "awaiting_source_review": True, "run_id": job["run_id"],
                    "job_id": job["id"], "analysis": public_analysis,
                    "source_provenance": self._public_job_source_provenance(job)}
        if analysis.outcome not in {SUPPORTED_SPECIFIC_ADAPTER, SUPPORTED_GENERIC_HIGH_CONFIDENCE}:
            # Record a terminal, sanitised outcome so the user never sees a source failure as
            # a silently abandoned submission.  No worker or child process is started.
            job = self.store.transition(
                job["id"], JobStatus.FAILED, reason_code=analysis.outcome,
                source_analysis_json=json.dumps(public_analysis, ensure_ascii=False), stage="source_analysis",
                error_type="source_analysis", error_message=analysis.outcome,
                **provenance,
            )
            return {"ok": False, "run_id": job["run_id"], "job_id": job["id"],
                    "analysis": public_analysis, "reason_code": analysis.outcome}

        # Automatic high-confidence extraction is re-analysed by the runner. Storing the
        # opaque IDs gives auditability, but does not pin a volatile DOM order in its command.
        status = env_status()
        if not status["env_exists"] or not status["nvidia_configured"]:
            reason_code = "environment_not_configured"
            job = self.store.transition(
                job["id"], JobStatus.FAILED, reason_code=reason_code,
                source_analysis_json=json.dumps(public_analysis, ensure_ascii=False),
                stage="source_analysis", error_type="configuration", error_message=reason_code,
                **provenance,
            )
            return {
                "ok": False, "run_id": job["run_id"], "job_id": job["id"],
                "analysis": public_analysis, "reason_code": reason_code,
                "message": "Configure o arquivo .env e a NVIDIA_API_KEY antes de processar.",
                "stage": "configuração", "action": "Configure o ambiente e envie a fonte novamente.",
            }
        automatic_selection = {
            "candidate_ids": selected_ids,
            "automatic": True,
            "accepted_candidate_count": len(selected_ids),
            "selected_candidate_count": len(selected_ids),
            "manual_subset": False,
        }
        job = self.store.transition(
            job["id"], JobStatus.QUEUED,
            source_analysis_json=json.dumps(public_analysis, ensure_ascii=False),
            source_selection_json=json.dumps(automatic_selection, ensure_ascii=False),
            stage="created",
            **provenance,
        )
        # Persisting a job nobody claims is the whole bug: make the consumer exist, and
        # report honestly when it could not be started instead of looking like a no-op.
        worker = self.ensure_worker()
        return {"ok": True, "run_id": job["run_id"], "job_id": job["id"], "worker": worker,
                "analysis": public_analysis}

    @staticmethod
    def _requested_source_type(payload: dict[str, Any]) -> str:
        """Validate source fields without loading the local image stack for URL jobs."""

        from chapter_source import SourceError

        source_url = str(payload.get("url") or "").strip()
        local_folder = str(payload.get("local_folder") or "").strip()
        if source_url and local_folder:
            raise SourceError("invalid_request", "url_and_folder")
        if local_folder:
            actual = "local_folder"
        elif source_url:
            actual = "url"
        else:
            raise SourceError("invalid_request", "missing_source")
        declared = str(payload.get("source_type") or "").strip().casefold()
        if declared and declared not in {"url", "local_folder"}:
            raise SourceError("invalid_request", "unknown_source_type")
        if declared and declared != actual:
            raise SourceError("invalid_request", "source_type_mismatch")
        return actual

    @staticmethod
    def _remote_source_provenance(public_analysis: dict[str, Any]) -> dict[str, Any]:
        """Return fixed-width, URL-free provenance fields for a remote source job.

        The full public analysis remains in its JSON diagnostic column.  The indexed job
        columns intentionally receive only the small adapter/count subset needed to identify
        a run in history, never a page URL, cookie, selector, source title or signed token.
        """

        analysis = public_analysis if isinstance(public_analysis, dict) else {}

        def identifier(value: Any) -> str:
            text = str(value or "").strip()
            return text if re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", text) else ""

        def count(name: str) -> int:
            value = analysis.get(name, 0)
            try:
                result = int(value)
            except (TypeError, ValueError):
                return 0
            return result if 0 <= result <= 10_000 else 0

        def score() -> float:
            try:
                value = float(analysis.get("confidence", 0.0))
            except (TypeError, ValueError):
                return 0.0
            return value if 0.0 <= value <= 1.0 else 0.0

        return {
            "source_type": "url",
            "adapter_name": identifier(analysis.get("adapter")),
            "adapter_version": identifier(analysis.get("adapter_version")),
            # A browser analysis has not selected a network transport yet.  Represent that
            # explicitly; the runner replaces it with the transport actually used.
            "transport_name": "pending",
            "source_score": score(),
            "candidate_count": count("candidate_count"),
            "input_count": count("candidate_count"),
            "accepted_count": count("accepted_count"),
            "rejected_count": count("discarded_count"),
        }

    @staticmethod
    def _source_analysis_is_incomplete(public_analysis: dict[str, Any]) -> bool:
        """Recognise only bounded collector coverage warnings in a public analysis."""

        warnings = public_analysis.get("warnings", ()) if isinstance(public_analysis, dict) else ()
        if isinstance(warnings, (str, bytes)):
            warnings = (warnings,)
        return bool({
            str(value or "").strip()
            for value in (warnings or ())
        } & {"page_limit_exceeded", "scroll_incomplete", "pagination_incomplete"})

    async def _start_local_folder(
        self,
        payload: dict[str, Any],
        *,
        principal: RequestPrincipal | None,
    ) -> dict[str, Any]:
        """Snapshot a loopback-only folder selection and queue its opaque reference.

        The raw path exists only as an argument to the local adapter.  The SQLite row,
        command, browser DTO, output name and source-analysis JSON contain generated ids,
        counts, and a non-reversible fingerprint instead.
        """

        normalized = self._normalize_local_payload(payload)
        job = self._create_local_folder_staging_job(normalized, principal=principal)
        self.store.update_fields(
            job["id"], stage="validating_local_source", reason_code="",
            started_at=time.time(), heartbeat_at=time.time(),
        )

        # Do not read or copy a local folder when translation execution is unavailable. The
        # failure is still a visible terminal job, but there is no orphaned snapshot/cache.
        status = env_status()
        if not status["env_exists"] or not status["nvidia_configured"]:
            reason_code = "environment_not_configured"
            failed = self.store.transition(
                job["id"], JobStatus.FAILED, reason_code=reason_code,
                stage="validating_local_source", error_type="configuration",
                error_message=reason_code,
            )
            return {
                "ok": False, "run_id": failed["run_id"], "job_id": failed["id"],
                "reason_code": reason_code,
                "message": "Configure o arquivo .env e a NVIDIA_API_KEY antes de processar.",
                "stage": "configuração", "action": "Configure o ambiente e envie a pasta novamente.",
            }

        raw_folder = str(payload.get("local_folder") or "").strip()
        try:
            self.store.update_fields(job["id"], stage="creating_snapshot", heartbeat_at=time.time())
            snapshot_ref, summary, candidate_ids = await self._snapshot_local_folder(raw_folder)
        except asyncio.CancelledError:
            current = self.store.get_job(job["id"])
            if current and current.get("status") == JobStatus.STAGING:
                self.store.transition(
                    job["id"], JobStatus.CANCELLED, reason_code="cancelled",
                    interrupted_reason="local_snapshot_request_cancelled",
                )
            raise
        except Exception as exc:
            from chapter_source import SOURCE_NOT_READY, SourceError

            current = self.store.get_job(job["id"])
            if current and current.get("status") == JobStatus.CANCELLED:
                return {"ok": False, "cancelled": True, "run_id": job["run_id"], "job_id": job["id"]}
            source_error = exc if isinstance(exc, SourceError) else SourceError(
                SOURCE_NOT_READY, type(exc).__name__)
            if current and current.get("status") == JobStatus.STAGING:
                self.store.transition(
                    job["id"], JobStatus.FAILED, reason_code=source_error.code,
                    stage="validating_local_source", error_type="local_source",
                    error_message=source_error.code,
                )
            if isinstance(exc, SourceError):
                raise
            raise source_error from exc

        current = self.store.get_job(job["id"])
        if not current or current.get("status") == JobStatus.CANCELLED:
            return {"ok": False, "cancelled": True, "run_id": job["run_id"], "job_id": job["id"]}

        from local_folder_job import build_local_job_command, job_fields

        command = build_local_job_command(
            snapshot_ref=snapshot_ref,
            output=normalized["slug"],
            mode=normalized["mode"],
            logical_pages=True,
            use_cache=normalized["use_cache"],
            force=normalized["force"],
            use_context=normalized["use_context"],
            open_output=normalized["open_output"],
            python_executable=sys.executable,
        )
        selection = {
            "candidate_ids": candidate_ids,
            "automatic": True,
            "accepted_candidate_count": len(candidate_ids),
            "selected_candidate_count": len(candidate_ids),
            "manual_subset": False,
        }
        configuration = dict(current.get("configuration") or {})
        configuration.update({
            "local_source_summary": summary,
            "source_selection": selection,
        })
        self.store.update_fields(
            job["id"],
            command_json=json.dumps(command, ensure_ascii=False),
            configuration_json=json.dumps(configuration, ensure_ascii=False),
            source_analysis_json=json.dumps(summary, ensure_ascii=False),
            source_selection_json=json.dumps(selection, ensure_ascii=False),
            **job_fields(summary, snapshot_ref),
        )
        queued = self.store.transition(job["id"], JobStatus.QUEUED, stage="created")
        self.history_revision += 1
        return {
            "ok": True, "run_id": queued["run_id"], "job_id": queued["id"],
            "worker": self.ensure_worker(), "analysis": summary,
        }

    async def _snapshot_local_folder(
        self,
        raw_folder: str,
    ) -> tuple[str, dict[str, Any], list[str]]:
        """Run bounded local validation/copying off the UI loop, returning no path."""

        return await asyncio.to_thread(self._snapshot_local_folder_sync, raw_folder)

    @staticmethod
    def _snapshot_local_folder_sync(raw_folder: str) -> tuple[str, dict[str, Any], list[str]]:
        # Imports are intentionally delayed: opening the UI or importing this module neither
        # creates an input directory nor initialises the local image adapter.
        from local_folder_input import snapshot_workspace_root
        from local_folder_job import public_summary
        from local_folder_source import LocalFolderChapterAdapter

        snapshot = LocalFolderChapterAdapter().snapshot(raw_folder, snapshot_workspace_root())
        summary = public_summary(snapshot.analysis.source_folder, snapshot.analysis)
        manifest = snapshot.public()
        pages = manifest.get("pages") if isinstance(manifest, dict) else []
        candidate_ids = [
            str(page.get("id") or "")
            for page in pages if isinstance(page, dict) and str(page.get("id") or "")
        ]
        if not candidate_ids:
            from chapter_source import NO_CHAPTER_IMAGES, SourceError

            raise SourceError(NO_CHAPTER_IMAGES, "local_snapshot_without_pages")
        # ``workspace.name`` is generated by the adapter, never a user path/name.
        return snapshot.workspace.name, summary, candidate_ids

    def _create_local_folder_staging_job(
        self,
        normalized: dict[str, Any],
        *,
        principal: RequestPrincipal | None,
    ) -> dict[str, Any]:
        if principal is not None and not isinstance(principal, RequestPrincipal):
            raise TypeError("principal must be a RequestPrincipal")
        from local_folder_job import SOURCE_TYPE_LOCAL_FOLDER

        output_folder = (OUTPUT_ROOT / normalized["slug"]).resolve()
        configuration: dict[str, Any] = {
            "job_type": "translation",
            "source_type": SOURCE_TYPE_LOCAL_FOLDER,
            "mode": normalized["mode"],
            "full": True,
            "max_images": None,
            "force": normalized["force"],
            "use_cache": normalized["use_cache"],
            "use_context": normalized["use_context"],
            "chapter_name": normalized["chapter_name"],
            "open_output": normalized["open_output"],
            "create_source_profile": False,
            "source_analysis": {},
            "source_selection": {},
        }
        if principal is not None and principal.authenticated:
            configuration["community_owner_id"] = principal.user_id
        staging_owner_pid = os.getpid()
        staging_owner_create_time: float | None = None
        try:
            import process_tree

            snapshot = process_tree.snapshot(staging_owner_pid) or {}
            value = snapshot.get("create_time")
            staging_owner_create_time = float(value) if value is not None else None
        except (ImportError, OSError, TypeError, ValueError):
            pass
        job_id = self.store.create_job(
            # A local folder is never a URL and its original path is never persisted.
            source_url="",
            output_dir=str(output_folder),
            command=[],
            run_id=str(normalized["id"]),
            configuration=configuration,
            series_title=normalized["chapter_name"],
            series_slug=normalized["slug"],
            episode_number="",
            commit_hash=_current_commit(),
            branch=_current_branch(),
            initial_status=JobStatus.STAGING,
            staging_owner_pid=staging_owner_pid,
            staging_owner_create_time=staging_owner_create_time,
        )
        # Keep the universal source discriminator on the row from the moment the job is
        # visible.  Snapshot-specific provenance is intentionally unavailable until the
        # local adapter has accepted the folder, but a failed intake must still be
        # classifiable without inspecting its configuration blob.
        self.store.update_fields(job_id, source_type=SOURCE_TYPE_LOCAL_FOLDER)
        self.history_revision += 1
        return self.store.get_job(job_id)  # type: ignore[return-value]

    @staticmethod
    def _looks_like_local_path(value: str) -> bool:
        text = str(value or "").strip()
        return bool(re.search(
            r"(?:[A-Za-z]:[\\\\/]|\\\\\\\\|(?:^|\s)/|\bfile:)", text, re.I))

    def _normalize_local_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Normalise local-run settings without deriving any label from the folder path."""

        raw_folder = str(payload.get("local_folder") or "").strip()
        if not raw_folder:
            raise ValueError("local_folder_required")
        mode = str(payload.get("mode") or "fast")
        full = bool(payload.get("full", True))
        max_images = None if full else int(payload.get("max_images") or 0)
        force = bool(payload.get("force", False))
        use_cache = bool(payload.get("use_cache", not force))
        if mode not in {"fast", "quality"}:
            raise ValueError("O modo precisa ser fast ou quality.")
        if use_cache and force:
            raise ValueError("Cache e reprocessamento forçado são mutuamente exclusivos.")
        if not full or max_images is not None:
            raise ValueError("local_folder_requires_full_scope")
        chapter_name = str(payload.get("chapter_name") or "Capítulo local").strip()[:120]
        raw_slug = str(payload.get("slug") or "capitulo_local").strip()
        if (
            self._looks_like_local_path(chapter_name)
            or self._looks_like_local_path(raw_slug)
            or raw_folder.casefold() in chapter_name.casefold()
            or raw_folder.casefold() in raw_slug.casefold()
        ):
            raise ValueError("local_folder_name_must_not_be_path")
        return {
            "id": str(payload.get("id") or uuid.uuid4()),
            "chapter_name": chapter_name or "Capítulo local",
            "slug": sanitize_output_name(raw_slug),
            "mode": mode,
            "full": True,
            "max_images": None,
            "use_cache": use_cache,
            "force": force,
            "use_context": bool(payload.get("use_context", True)),
            "open_output": bool(payload.get("open_output", False)),
            "create_source_profile": False,
        }

    @staticmethod
    def _analyze_source(url: str, *, cancel_check=None):
        """Late import keeps UI bootstrap/import hermetic; only a user submit navigates."""
        from down import analyze_chapter_source

        return analyze_chapter_source(url, cancel_check=cancel_check)

    async def _run_source_analysis(self, url: str, *, cancel_check=None):
        """Run Selenium analysis off the UI loop while retaining cancellation visibility."""
        return await asyncio.wait_for(
            asyncio.to_thread(self._analyze_source, url, cancel_check=cancel_check),
            timeout=SOURCE_ANALYSIS_TIMEOUT_SECONDS,
        )

    def _pending_duplicate(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """An identical chapter already queued or in flight, if any."""
        try:
            url = clean_url(str(payload.get("url") or "")).strip()
            slug = sanitize_output_name(str(payload.get("slug") or ""))
        except Exception:  # noqa: BLE001 - normalization errors surface in _create_job
            return None
        if not url:
            return None
        pending = self.store.list_jobs(
            statuses=[JobStatus.STAGING, JobStatus.QUEUED, JobStatus.AWAITING_SOURCE_REVIEW,
                      *JobStatus.IN_FLIGHT], limit=None)
        for job in pending:
            if not self._is_translation_job(job):
                continue
            same_slug = slug and Path(str(job.get("output_dir") or "")).name == slug
            if job.get("source_url") == url or same_slug:
                return job
        return None

    def confirm_source_pages(self, job_id: str, candidate_ids: list[str]) -> dict[str, Any]:
        """Queue a medium-confidence reader only after an explicit, validated selection.

        The client sends opaque IDs, never remote URLs.  IDs must be a non-empty subset of the
        sanitised analysis stored with this job; the worker re-analyses at execution and rejects
        a changed reader instead of trusting stale source data.
        """
        if not isinstance(candidate_ids, list):
            raise ValueError("invalid_source_candidate_selection")
        job = self.store.get_job(str(job_id or ""))
        if not job or job.get("status") != JobStatus.AWAITING_SOURCE_REVIEW:
            raise ValueError("source_review_not_available")
        analysis = job.get("source_analysis") or {}
        accepted_ids = [
            str(item.get("id") or "")
            for item in (analysis.get("accepted") or [])
            if isinstance(item, dict) and str(item.get("id") or "")
        ]
        allowed = set(accepted_ids)
        selected = list(dict.fromkeys(str(value or "") for value in candidate_ids if value))
        if not selected or not set(selected).issubset(allowed):
            raise ValueError("invalid_source_candidate_selection")
        status = env_status()
        if not status["env_exists"] or not status["nvidia_configured"]:
            raise ValueError("Configure o arquivo .env e a NVIDIA_API_KEY antes de processar.")
        config = job.get("configuration") or {}
        command = build_run_command(
            url=str(job.get("source_url") or ""),
            mode=str(config.get("mode") or "fast"),
            output=Path(str(job.get("output_dir") or "chapter")).name,
            full=bool(config.get("full", True)),
            max_images=config.get("max_images"),
            use_cache=bool(config.get("use_cache")),
            force=bool(config.get("force")),
            use_context=bool(config.get("use_context", True)),
            source_candidate_ids=selected,
            open_output=bool(config.get("open_output", False)),
            python_executable=sys.executable,
        )
        selection = {
            "candidate_ids": selected,
            "automatic": False,
            "accepted_candidate_count": len(allowed),
            "selected_candidate_count": len(selected),
            # The selection itself is an explicit user acknowledgement that excluded pages
            # are not chapter pages. Persist it so a completed output is never mistaken for
            # an untouched automatic cluster.
            "manual_subset": len(selected) < len(allowed),
            # The UI submits opaque IDs in visible order. Preserve an intentional reordering
            # separately from an ordinary subset so the downloader can retain that sequence
            # and an audit can distinguish it from adapter order.
            "manual_reordered": selected != [item for item in accepted_ids if item in selected],
        }
        config["source_selection"] = selection
        self.store.update_fields(
            job["id"], command_json=json.dumps(command, ensure_ascii=False),
            source_selection_json=json.dumps(selection, ensure_ascii=False),
            configuration_json=json.dumps(config, ensure_ascii=False),
            reason_code="",
            stage="created",
        )
        job = self.store.transition(job["id"], JobStatus.QUEUED)
        self.history_revision += 1
        return {"ok": True, "job_id": job["id"], "worker": self.ensure_worker()}

    def _create_job(
        self,
        payload: dict[str, Any],
        *,
        require_environment: bool = True,
        principal: RequestPrincipal | None = None,
        initial_status: str = JobStatus.QUEUED,
        source_analysis: dict[str, Any] | None = None,
        source_selection: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if principal is not None and not isinstance(principal, RequestPrincipal):
            raise TypeError("principal must be a RequestPrincipal")
        normalized = self._normalize_payload(payload, require_environment=require_environment)
        command = build_run_command(
            url=normalized["url"],
            mode=normalized["mode"],
            output=normalized["slug"],
            full=normalized["full"],
            max_images=normalized.get("max_images"),
            use_cache=normalized["use_cache"],
            force=normalized["force"],
            use_context=normalized["use_context"],
            source_candidate_ids=list(payload.get("source_candidate_ids") or []),
            open_output=normalized["open_output"],
            python_executable=sys.executable,
        )
        output_folder = (OUTPUT_ROOT / normalized["slug"]).resolve()
        details = suggest_chapter_details(normalized["url"])
        configuration = {
            "job_type": "translation",
            "mode": normalized["mode"],
            "full": normalized["full"],
            "max_images": normalized["max_images"],
            "force": normalized["force"],
            "use_cache": normalized["use_cache"],
            "use_context": normalized["use_context"],
            "chapter_name": normalized["chapter_name"],
            "open_output": normalized["open_output"],
            "create_source_profile": normalized["create_source_profile"],
            "source_analysis": source_analysis or {},
            "source_selection": source_selection or {},
        }
        if principal is not None and principal.authenticated:
            configuration["community_owner_id"] = principal.user_id
        # Source analysis is an intentionally non-claimable STAGING job owned by this UI
        # process, not by the background worker.  Persist the process identity before the
        # browser thread starts so worker recovery can distinguish a live analysis from a
        # staging row abandoned by a crashed UI.
        staging_owner_pid: int | None = None
        staging_owner_create_time: float | None = None
        if initial_status == JobStatus.STAGING:
            staging_owner_pid = os.getpid()
            try:
                import process_tree

                snapshot = process_tree.snapshot(staging_owner_pid) or {}
                value = snapshot.get("create_time")
                staging_owner_create_time = float(value) if value is not None else None
            except (ImportError, OSError, TypeError, ValueError):
                # PID ownership without a creation time is weaker, but still lets the
                # recovery loop preserve an analysis whose creating UI is demonstrably live.
                staging_owner_create_time = None
        job_id = self.store.create_job(
            source_url=normalized["url"],
            output_dir=str(output_folder),
            command=command,
            run_id=str(normalized["id"]),
            configuration=configuration,
            series_title=normalized["chapter_name"],
            series_slug=str(details.get("slug") or ""),
            episode_number=str(details.get("episode") or ""),
            commit_hash=_current_commit(),
            branch=_current_branch(),
            initial_status=initial_status,
            staging_owner_pid=staging_owner_pid,
            staging_owner_create_time=staging_owner_create_time,
        )
        # Every URL job receives scalar adapter evidence before it can enter a queue. A later
        # fresh downloader diagnosis replaces preliminary values, but a specific-adapter batch
        # item never becomes an anonymous row while it waits for a worker.
        from chapter_source import select_adapter

        adapter = select_adapter(normalized["url"])
        provenance: dict[str, Any] = {
            "source_type": "url",
            "adapter_name": str(getattr(adapter, "name", "") or ""),
            "adapter_version": str(getattr(adapter, "adapter_version", "") or ""),
            "transport_name": "pending",
        }
        if source_analysis is not None:
            provenance.update(self._remote_source_provenance(source_analysis))
        if source_analysis is not None or source_selection is not None:
            provenance.update({
                "source_analysis_json": json.dumps(source_analysis or {}, ensure_ascii=False),
                "source_selection_json": json.dumps(source_selection or {}, ensure_ascii=False),
            })
        self.store.update_fields(job_id, **provenance)
        self.history_revision += 1
        return self.store.get_job(job_id)  # type: ignore[return-value]

    def ensure_worker(self) -> dict[str, Any]:
        """Make sure a worker exists to claim queued jobs.

        Without this the UI happily persists a job that nobody ever picks up: the queue is
        not in-flight, so the status stays "pronto" and the click looks like a no-op. Never
        starts a second worker — start_worker() checks the registered lease first.
        """
        healthy = self.store.healthy_worker(stale_seconds=15)
        if healthy:
            return {"online": True, "started": False}
        try:
            from start_tradutor import start_worker

            start_worker()
        except Exception as exc:  # noqa: BLE001 - reported to the caller, never swallowed
            return {"online": False, "started": False, "error": type(exc).__name__}
        healthy = self.store.healthy_worker(stale_seconds=15)
        return {"online": bool(healthy), "started": bool(healthy)}

    async def cancel(self, *, queue: bool = False, job_id: str = "") -> dict[str, Any]:
        requested_review_id = str(job_id or "").strip()
        if requested_review_id:
            displayed = self._displayed_source_review()
            # The source-review panel represents exactly the newest waiting job. Refuse a
            # stale/forged id rather than cancelling another pending chapter behind it.
            if not displayed or displayed.get("id") != requested_review_id:
                raise ValueError("source_review_not_available")
            self.store.transition(
                requested_review_id, JobStatus.CANCELLED,
                interrupted_reason="cancelled_source_review", reason_code="cancelled",
            )
            self.history_revision += 1
            return {"ok": True, "job_id": requested_review_id}
        active = self.store.active_job()
        if self._is_translation_job(active):
            self.store.request_cancel(active["id"])
            # The runner honours the flag and tears down only its own process tree. When the
            # runner is already gone nobody would ever act on the flag, so settle the job
            # here instead of leaving it in flight forever. Outputs are left untouched.
            if not _runner_still_alive(active):
                frozen = (_normalize_epoch(active.get("heartbeat_at"))
                          or _normalize_epoch(active.get("updated_at")) or time.time())
                for target in (JobStatus.CANCELLING, JobStatus.CANCELLED):
                    try:
                        self.store.transition(active["id"], target,
                                              interrupted_reason="cancelled_process_not_found",
                                              finished_at=frozen)
                    except Exception:  # noqa: BLE001 - already settled by another path
                        pass
        # A source review needs its explicit displayed job id above. For a source-analysis
        # request that has not yet returned a review id, cancel at most the newest staging row;
        # never fan one browser action out to unrelated submissions.
        staging = max(
            (candidate for candidate in self.store.list_jobs(statuses=[JobStatus.STAGING])
             if self._is_translation_job(candidate)),
            key=lambda candidate: float(candidate.get("updated_at") or 0), default=None)
        if staging:
            try:
                self.store.transition(staging["id"], JobStatus.CANCELLED,
                                      interrupted_reason="cancelled_source_analysis",
                                      reason_code="cancelled")
            except Exception:  # noqa: BLE001 - a completed analysis wins
                pass
        if queue:
            for job in self.store.list_jobs(statuses=[JobStatus.QUEUED]):
                if not self._is_translation_job(job):
                    continue
                try:
                    self.store.transition(job["id"], JobStatus.CANCELLED,
                                          interrupted_reason="queue_cleared")
                except Exception:  # noqa: BLE001 - best effort per queued job
                    pass
        self.history_revision += 1
        return {"ok": True}

    async def retry_source_review(self, job_id: str) -> dict[str, Any]:
        """Discard one displayed review hold and rerun only its safe source analysis."""
        job = self._displayed_source_review()
        if not job or job.get("id") != str(job_id or "").strip():
            raise ValueError("source_review_not_available")
        config = job.get("configuration") or {}
        source_url = str(job.get("source_url") or "")
        if not source_url:
            # Local snapshots are immutable inputs; retrying their source analysis would mean
            # rereading a path that is deliberately absent from the job row.
            raise ValueError("source_review_retry_not_available")
        self.store.transition(
            job["id"], JobStatus.CANCELLED,
            interrupted_reason="source_review_retry_requested", reason_code="cancelled",
        )
        self.history_revision += 1
        payload = {
            "source_type": "url",
            "url": source_url,
            "chapter_name": config.get("chapter_name") or job.get("series_title") or "",
            "slug": Path(str(job.get("output_dir") or "chapter")).name,
            "mode": config.get("mode") or "fast",
            "full": bool(config.get("full", True)),
            "max_images": config.get("max_images"),
            "use_cache": bool(config.get("use_cache")),
            "force": bool(config.get("force")),
            "use_context": bool(config.get("use_context", True)),
            "open_output": bool(config.get("open_output", False)),
            "create_source_profile": config.get("create_source_profile") is True,
        }
        return await self.start(payload)

    def resume(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise ValueError("Job não encontrado.")
        if not self._is_translation_job(job):
            raise ValueError("job_type_not_resumable_from_ui")
        if job["status"] not in {JobStatus.INTERRUPTED, JobStatus.RESUMABLE}:
            raise ValueError("Somente jobs interrompidos podem ser retomados.")
        # Mutual exclusion: never start a new attempt while the previous attempt's runner
        # is still alive, or two trees would process the same chapter at once.
        if _runner_still_alive(job):
            raise ValueError("previous_attempt_still_running")
        if job["status"] == JobStatus.INTERRUPTED:
            self.store.mark_resumable(job_id, resume_from_stage=job.get("resume_from_stage") or "")
        # A resume is a fresh attempt that reuses the same output dir and command; the
        # previous attempt is preserved for history.
        new_id = self.store.create_job(
            source_url=job["source_url"],
            output_dir=job["output_dir"],
            command=job.get("command") or [],
            configuration=job.get("configuration") or {},
            series_title=job.get("series_title") or "",
            series_slug=job.get("series_slug") or "",
            episode_number=job.get("episode_number") or "",
            commit_hash=_current_commit(),
            branch=_current_branch(),
            previous_job_id=job_id,
            attempt=int(job.get("attempt") or 1) + 1,
            resume_from_stage=job.get("resume_from_stage") or "",
        )
        # ``create_job`` starts an independent attempt. Preserve the path-free scalar
        # evidence and the sanitised analysis/selection for both local snapshots and URL
        # jobs so retries remain auditable even before the downloader refreshes its result.
        provenance = {
            key: job.get(key)
            for key in (
                "source_type", "adapter_name", "adapter_version", "transport_name",
                "source_score", "candidate_count", "input_root_fingerprint",
                "snapshot_ref", "logical_pages", "input_count", "accepted_count",
                "rejected_count", "duplicate_count", "total_size_bytes",
            )
            if job.get(key) is not None
        }
        provenance["source_analysis_json"] = json.dumps(
            job.get("source_analysis") or {}, ensure_ascii=False)
        provenance["source_selection_json"] = json.dumps(
            job.get("source_selection") or {}, ensure_ascii=False)
        self.store.update_fields(new_id, **provenance)
        self.store.transition(job_id, JobStatus.QUEUED) if job["status"] == JobStatus.RESUMABLE else None
        self.history_revision += 1
        return {"ok": True, "job_id": new_id}

    def add_queue_item(
        self,
        payload: dict[str, Any],
        *,
        principal: RequestPrincipal | None = None,
    ) -> dict[str, Any]:
        # A generic page cannot enter the background queue before its reader cluster is
        # confirmed.  Specific adapters retain the existing batch flow; a generic source is
        # intentionally directed through the interactive source-analysis screen.
        from chapter_source import select_adapter
        if self._requested_source_type(payload) == "local_folder":
            # The batch form is URL-only.  Folder intake needs the loopback-only API gate and
            # a snapshot before a job is queued, so it can only start from the main form.
            raise ValueError("local_folder_requires_primary_submit")
        url = clean_url(str(payload.get("url") or ""))
        if not select_adapter(url).is_specific:
            raise ValueError("generic_sources_require_interactive_review")
        job = self._create_job(
            {**payload, "full": True},
            require_environment=False,
            principal=principal,
        )
        return self._job_record(job)  # type: ignore[return-value]

    def remove_queue_item(self, item_id: str) -> None:
        job = self.store.get_job(item_id)
        if self._is_translation_job(job) and job["status"] == JobStatus.QUEUED:
            self.store.transition(item_id, JobStatus.CANCELLED, interrupted_reason="removed")

    def clear_queue(self) -> None:
        for job in self.store.list_jobs(statuses=[JobStatus.QUEUED]):
            if not self._is_translation_job(job):
                continue
            try:
                self.store.transition(job["id"], JobStatus.CANCELLED, interrupted_reason="queue_cleared")
            except Exception:  # noqa: BLE001
                pass

    async def start_queue(self) -> dict[str, Any]:
        # Jobs are already queued in the store; the independent worker drains them.
        if not any(
            self._is_translation_job(job)
            for job in self.store.list_jobs(statuses=[JobStatus.QUEUED])
        ):
            raise ValueError("A fila não tem itens aguardando.")
        status = env_status()
        if not status["env_exists"] or not status["nvidia_configured"]:
            raise ValueError("Configure o arquivo .env e a NVIDIA_API_KEY antes de processar.")
        return {"ok": True}

    def save_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed_status = {"online", "away", "busy", "offline"}
        profile = _profile_default()
        profile.update(self.profile)
        profile.update(
            {
                "display_name": str(payload.get("display_name") or "você")[:40],
                "title": str(payload.get("title") or "")[:40],
                "pronouns": str(payload.get("pronouns") or "")[:30],
                "status": str(payload.get("status") or "online")
                if str(payload.get("status") or "online") in allowed_status
                else "online",
                "status_text": str(payload.get("status_text") or "")[:60],
                "bio": str(payload.get("bio") or "")[:190],
                "avatar_mode": "image"
                if str(payload.get("avatar_mode") or "letter") == "image"
                else "letter",
                "avatar_color": str(payload.get("avatar_color") or "#c5372c")[:16],
                "banner": str(payload.get("banner") or "ink")[:20],
            }
        )
        self.profile = profile
        self._write_profile()
        return self._profile_payload()

    def save_profile_media(
        self,
        kind: str,
        *,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> dict[str, Any]:
        if kind not in {"avatar", "banner"}:
            raise ValueError("Tipo de mídia de perfil inválido.")
        suffix = Path(filename or "").suffix.casefold()
        expected_type = PROFILE_MEDIA_TYPES.get(suffix)
        if not expected_type or content_type.casefold().split(";", 1)[0] != expected_type:
            raise ValueError("Use PNG, JPG, JPEG, WEBP, GIF, MP4 ou WEBM.")
        if not content or len(content) > MAX_PROFILE_MEDIA_BYTES:
            raise ValueError("A mídia deve ter no máximo 12 MB.")

        PROFILE_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        target = (PROFILE_MEDIA_DIR / f"{kind}{suffix}").resolve()
        if PROFILE_MEDIA_DIR.resolve() not in target.parents:
            raise ValueError("Nome de mídia inválido.")
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(content)
        temporary.replace(target)
        for candidate in PROFILE_MEDIA_DIR.glob(f"{kind}.*"):
            if candidate.resolve() != target and candidate.suffix != ".tmp":
                candidate.unlink(missing_ok=True)

        self.profile[f"{kind}_media_path"] = str(target)
        self.profile[f"{kind}_media_type"] = expected_type
        self.profile[f"{kind}_media_name"] = Path(filename).name[:120]
        self.profile[f"{kind}_media_size"] = len(content)
        self.profile[f"{kind}_media_updated_at"] = utc_now()
        if kind == "avatar":
            self.profile["avatar_mode"] = "image"
        else:
            self.profile["banner"] = "custom"
        self._write_profile()
        return self._profile_payload()

    def remove_profile_media(self, kind: str) -> dict[str, Any]:
        if kind not in {"avatar", "banner"}:
            raise ValueError("Tipo de mídia de perfil inválido.")
        path = self.profile_media_path(kind)
        if path:
            path.unlink(missing_ok=True)
        for key in ("path", "type", "name", "size", "updated_at"):
            self.profile.pop(f"{kind}_media_{key}", None)
        self.profile[f"{kind}_media_path"] = ""
        self.profile[f"{kind}_media_type"] = ""
        self.profile[f"{kind}_media_name"] = ""
        self.profile[f"{kind}_media_size"] = 0
        if kind == "avatar":
            self.profile["avatar_mode"] = "letter"
        else:
            self.profile["banner"] = "ink"
        self._write_profile()
        return self._profile_payload()

    def profile_media_path(self, kind: str) -> Path | None:
        if kind not in {"avatar", "banner"}:
            return None
        raw_path = str(self.profile.get(f"{kind}_media_path") or "")
        if not raw_path:
            return None
        path = Path(raw_path).resolve()
        media_root = PROFILE_MEDIA_DIR.resolve()
        if media_root not in path.parents or not path.is_file():
            return None
        return path

    def open_artifact(self, path_value: str, *, select: bool = False) -> None:
        path = Path(str(path_value or "")).expanduser().resolve()
        output_root = OUTPUT_ROOT.resolve()
        if path != output_root and output_root not in path.parents:
            raise ValueError("A interface só abre artefatos dentro de output/.")
        if not path.exists():
            raise ValueError("Arquivo ou pasta não encontrado.")
        if os.name == "nt":
            if select and path.is_file():
                subprocess.Popen(["explorer.exe", "/select,", str(path)])
            else:
                os.startfile(path)  # type: ignore[attr-defined]
        else:
            import webbrowser

            webbrowser.open(path.as_uri())

    async def shutdown(self) -> None:
        # Closing the UI must never cancel a running job: the worker owns it and keeps
        # processing. Only release this process's database handle.
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass

    def _normalize_payload(
        self,
        payload: dict[str, Any],
        *,
        require_environment: bool = True,
    ) -> dict[str, Any]:
        if require_environment:
            status = env_status()
            if not status["env_exists"] or not status["nvidia_configured"]:
                raise ValueError("Configure o arquivo .env e a NVIDIA_API_KEY antes de processar.")
        url = clean_url(str(payload.get("url") or "")).strip()
        details = suggest_chapter_details(url)
        mode = str(payload.get("mode") or "fast")
        full = bool(payload.get("full", True))
        max_images = None if full else int(payload.get("max_images") or 0)
        force = bool(payload.get("force", False))
        use_cache = bool(payload.get("use_cache", not force))
        slug = sanitize_output_name(str(payload.get("slug") or details["slug"]))
        build_run_command(
            url=url,
            mode=mode,
            output=slug,
            full=full,
            max_images=max_images,
            use_cache=use_cache,
            force=force,
            use_context=bool(payload.get("use_context", True)),
        )
        return {
            "id": str(payload.get("id") or uuid.uuid4()),
            "url": url,
            "chapter_name": str(payload.get("chapter_name") or details["title"])[:120],
            "slug": slug,
            "mode": mode,
            "full": full,
            "max_images": max_images,
            "use_cache": use_cache,
            "force": force,
            "use_context": bool(payload.get("use_context", True)),
            "open_output": bool(payload.get("open_output", False)),
            "create_source_profile": payload.get("create_source_profile") is True,
        }

    def _refresh_history(self) -> None:
        self.history = self.history_store.discover_outputs()
        self.history_revision += 1

    def _load_profile(self) -> dict[str, Any]:
        profile = _profile_default()
        try:
            stored = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                profile.update(stored)
        except (OSError, ValueError, TypeError):
            pass
        profile.pop("avatar_emoji", None)
        profile.pop("avatar_media", None)
        profile.pop("banner_media", None)
        if profile.get("avatar_mode") not in {"letter", "image"}:
            profile["avatar_mode"] = "letter"
        return profile

    def _write_profile(self) -> None:
        PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = PROFILE_PATH.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.profile, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(PROFILE_PATH)

    def _profile_payload(self) -> dict[str, Any]:
        payload = dict(self.profile)
        for kind in ("avatar", "banner"):
            path = self.profile_media_path(kind)
            updated = str(payload.get(f"{kind}_media_updated_at") or "")
            payload[f"{kind}_media_url"] = (
                f"/api/ui/profile/media/{kind}?v={updated}"
                if path
                else ""
            )
        return payload
