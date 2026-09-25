"""Independent worker that drains the persistent job queue.

Runs as its own OS process, launched separately from the UI, so a chapter keeps
processing when the browser tab closes, the page reloads, or app_ui.py is restarted.
It claims one job at a time (concurrency 1), spawns an isolated runner subprocess for
it, keeps its own lease heartbeat, and recovers jobs left stale by a previous crash.

Commands::

    python worker_service.py            # run until stopped
    python worker_service.py --once     # process at most one job, then exit
    python worker_service.py --status   # print worker/queue health and exit
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import process_tree
import app_version
from job_store import JobStatus, JobStore
from local_environment import load_local_environment_for_entrypoint
from process_options import build_background_process_options
from runtime_paths import runtime_root
from runtime_contract import current_runtime_contract
from ui_helpers import sanitize_diagnostic_text

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_RUNTIME_ROOT = runtime_root()
DEFAULT_DB = DEFAULT_RUNTIME_ROOT / "jobs.sqlite3"
LOG_DIR = DEFAULT_RUNTIME_ROOT / "logs"
DIAGNOSTICS_DIR = DEFAULT_RUNTIME_ROOT / "diagnostics"
POLL_SECONDS = 1.5
WORKER_HEARTBEAT_SECONDS = 3.0
SOURCE_VALIDATION_TIMEOUT_SECONDS = 180.0
SOURCE_ANALYSIS_REUSE_TTL_SECONDS = 15 * 60
STALE_SECONDS = 30.0
STAGING_GRACE_SECONDS = 5.0
COMMUNITY_RUNNER_MAX_ATTEMPTS = 3
COMMUNITY_RUNNER_RETRY_BACKOFF_SECONDS = 2.0


class Worker:
    def __init__(self, db_path: Path, *, poll_seconds: float = POLL_SECONDS,
                 stale_seconds: float = STALE_SECONDS, log_dir: Path | None = None):
        # Include the artifact build in the lease identity.  A detached worker can outlive
        # an in-place installer upgrade; reusing that lease would run old worker code
        # against a new UI/database and can silently stop after source validation.
        build_tag = str(getattr(app_version, "BUILD_VERSION", "dev") or "dev")
        build_tag = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in build_tag)
        self.worker_contract = current_runtime_contract()
        contract_tag = self.worker_contract.rsplit(":", 1)[-1]
        self.worker_id = f"{build_tag}:{contract_tag}:{uuid.uuid4().hex}"
        self.pid = os.getpid()
        self.db_path = Path(db_path)
        # Production keeps its established runtime log directory. A worker pointed at a
        # separate database (tests, maintenance, isolated smoke) writes beside that database
        # instead of silently creating logs in the project's production cache.
        try:
            is_default_db = self.db_path.resolve() == DEFAULT_DB.resolve()
        except OSError:
            is_default_db = self.db_path == DEFAULT_DB
        self.log_dir = Path(log_dir) if log_dir is not None else (
            LOG_DIR if is_default_db else self.db_path.parent / "logs"
        )
        self.poll_seconds = poll_seconds
        self.stale_seconds = stale_seconds
        self.store = JobStore(self.db_path)
        self._stop_requested = False
        self._active: dict | None = None  # {proc, job_id, worker_id fingerprint}

    def close(self) -> None:
        self.store.close()

    def request_stop(self) -> None:
        self._stop_requested = True

    def _install_signal_handlers(self) -> None:
        def _handler(signum, _frame):
            self._stop_requested = True

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    signal.signal(sig, _handler)
                except (ValueError, OSError):
                    pass

    def another_worker_alive(self) -> bool:
        healthy = self.store.healthy_worker(stale_seconds=self.stale_seconds / 2)
        return bool(healthy and healthy["worker_id"] != self.worker_id)

    def _append_job_log(self, job_id: str, stage: str, message: str = "") -> None:
        """Persist sanitized source-phase events before a runner exists."""
        safe_stage = str(stage or "source_analysis").strip().casefold()
        safe_stage = "".join(
            character for character in safe_stage
            if character.isascii() and (character.isalnum() or character in "_-")
        )[:64] or "source_analysis"
        safe_message = sanitize_diagnostic_text(str(message or "")).strip()
        log_dir = Path(getattr(
            self, "log_dir", Path(self.store.db_path).parent / "logs"))
        path = log_dir / f"{job_id}.log"
        line = f"{time.strftime('%H:%M:%S')} [{safe_stage}]"
        if safe_message:
            line += f" {safe_message[:500]}"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            # An unwritable log location is a diagnostics problem, never a reason to
            # crash the worker loop out from under a job it still owns.
            return
        self.store.update_fields(job_id, log_path=str(path))
        try:
            row = self.store.get_job(job_id) or {}
            trace_id = str((row.get("configuration") or {}).get("trace_id") or "")[:80]
            safe_job = hashlib.sha256(str(job_id).encode("utf-8")).hexdigest()[:12]
            trace_path = DIAGNOSTICS_DIR / f"job_{safe_job}.jsonl"
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            with trace_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "timestamp": time.time(), "event": safe_stage.upper(),
                    "trace_id": trace_id, "job_id": safe_job,
                    "message": safe_message[:500]}, separators=(",", ":")) + "\n")
        except (OSError, TypeError, ValueError):
            pass

    # Runner script per job type. Only translation is the default; more handlers register
    # here without spreading job-type ifs through the worker loop.
    _RUNNERS = {
        "translation": "job_runner.py",
        "community_publish": "community_publish_runner.py",
        "review_rerun": "review_rerun_runner.py",
    }

    def _runner_fingerprint(self, job_id: str, job: dict | None = None) -> list[str]:
        # Match the actual runner selected for this job type.  Using the translation
        # filename for every job would misclassify a live community upload as a reused
        # PID and could permit a second concurrent attempt.
        current = job or self.store.get_job(job_id) or {}
        if getattr(sys, "frozen", False):
            return ["YomuSekai.exe", str(job_id)]
        job_type = (current.get("configuration") or {}).get("job_type", "translation")
        runner = self._RUNNERS.get(job_type, "job_runner.py")
        return [runner, job_id]

    def _spawn_runner(self, job: dict) -> subprocess.Popen:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"{job['id']}.log"
        job_type = (job.get("configuration") or {}).get("job_type", "translation")
        runner = self._RUNNERS.get(job_type, "job_runner.py")
        gate_path = self.log_dir / f".{job['id']}.{uuid.uuid4().hex}.start"
        if bool(getattr(sys, "frozen", False)):
            role = {
                "job_runner.py": "job-runner",
                "community_publish_runner.py": "community-publish-runner",
                "review_rerun_runner.py": "review-rerun-runner",
            }.get(runner, "job-runner")
            command = [sys.executable, "--internal-child", role]
        else:
            command = [sys.executable, "-u", str(REPO_ROOT / runner)]
        command.extend([
            "--job-id", job["id"],
            "--db", str(self.db_path),
            "--worker-id", self.worker_id,
            "--log", str(log_path),
            "--start-gate", str(gate_path),
        ])
        self._append_job_log(
            job["id"], "runner", "RUNNER_SPAWN_REQUEST "
            f"frozen={bool(getattr(sys, 'frozen', False))} "
            f"hide_window={os.name == 'nt'} shell=false"
        )
        # The runner writes everything worth keeping to its own per-job log file, so its
        # stdout/stderr are silenced. Inheriting them would hand the runner (and the
        # pipeline it spawns) a handle to whatever console launched the worker, and a
        # tool capturing that console would then block on EOF until every descendant
        # exits - the cause of an earlier multi-hour hang.
        proc = subprocess.Popen(command, **build_background_process_options(
            cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ))
        self._append_job_log(job["id"], "runner", f"PROCESS_CREATED pid={proc.pid}")
        proc._tradutor_start_gate = gate_path  # type: ignore[attr-defined]
        return proc

    def _should_stop(self) -> bool:
        if self._stop_requested:
            return True
        try:
            return self.store.worker_stop_requested(self.worker_id)
        except Exception:  # noqa: BLE001 - a transient read must not crash the loop
            return False

    @staticmethod
    def _staging_owner_alive(job: dict) -> bool:
        """Whether the process that created a non-claimable staging job still owns it.

        Translation source analysis runs in the UI process before a worker may claim the
        job.  ``worker_pid``/``worker_create_time`` therefore hold the staging owner during
        that short phase; the creation time prevents a reused PID from keeping an abandoned
        row alive.
        """
        return process_tree.is_alive(
            job.get("worker_pid"), create_time=job.get("worker_create_time"))

    def _analyze_source(self, url: str, *, cancel_check=None, on_progress=None):
        """Injection seam: tests replace this instead of driving a real browser.

        Tries HTTP-only discovery first (no Chrome) and only falls back to the browser-based
        analysis when the adapter/page does not support it -- see ``discover_chapter_source``.
        """
        from down import discover_chapter_source

        return discover_chapter_source(url, cancel_check=cancel_check, on_progress=on_progress)

    def _reuse_persisted_source_analysis(self, job: dict):
        """Rehydrate a recent UI preflight without reopening the remote reader."""
        config = job.get("configuration") if isinstance(job.get("configuration"), dict) else {}
        analysis_id = str(config.get("preflight_source_analysis_result_id")
                          or config.get("source_analysis_result_id")
                          or config.get("analysis_result_id") or "").strip()
        if not analysis_id:
            return None
        from source_readiness import SourceReadinessStore
        readiness = SourceReadinessStore(self.store.db_path)
        try:
            result = readiness.get_analysis("local", analysis_id)
        finally:
            readiness.close()
        if result is None or result.status != "source_analysis_ready":
            self._append_job_log(job["id"], "source_validation", "SOURCE_REUSE_SKIPPED reason=missing_or_not_ready")
            return None
        age = max(0.0, time.time() - float(result.created_at or 0.0))
        if age > SOURCE_ANALYSIS_REUSE_TTL_SECONDS:
            self._append_job_log(job["id"], "source_validation", "SOURCE_REUSE_SKIPPED reason=expired")
            return None
        expected_hash = hashlib.sha256(str(job.get("source_url") or "").encode("utf-8")).hexdigest()
        if result.normalized_url_hash != expected_hash:
            self._append_job_log(job["id"], "source_validation", "SOURCE_REUSE_SKIPPED reason=lineage_mismatch")
            return None
        try:
            public = json.loads(job.get("source_analysis_json") or "{}")
            selection = json.loads(job.get("source_selection_json") or "{}")
        except (TypeError, ValueError):
            public, selection = {}, {}
        # A persisted preflight intentionally stores only URL-free public metadata and
        # opaque candidate IDs.  That is sufficient for review/history, but it is not a
        # materializable page manifest for the dynamic Comix reader: the ten logical pages
        # rendered through canvas/network fallback lose their reader-owned resource payload
        # when rehydrated from this sanitized snapshot.  Reopen the bounded public reader
        # analysis in the worker so the downloader receives the real 105-page manifest.
        if str(public.get("adapter") or public.get("adapter_name") or "").casefold() == "comix":
            self._append_job_log(
                job["id"], "source_validation",
                "SOURCE_REUSE_SKIPPED reason=dynamic_adapter_requires_materializable_manifest")
            return None
        ids = [str(v) for v in (getattr(result, "resolved_selection", ()) or []) if str(v)]
        if not ids:
            ids = [str(v) for v in (selection.get("candidate_ids") or []) if str(v)]
        if not ids:
            ids = [str(item.get("id")) for item in (public.get("accepted") or [])
                   if isinstance(item, dict) and str(item.get("id") or "")]
        if not ids:
            self._append_job_log(job["id"], "source_validation", "SOURCE_REUSE_SKIPPED reason=missing_selection")
            return None
        from universal_chapter_adapter import ImageCandidate, SourceAnalysis
        accepted_public = {str(item.get("id")): item for item in (public.get("accepted") or [])
                           if isinstance(item, dict)}
        accepted = []
        for order, candidate_id in enumerate(ids):
            item = accepted_public.get(candidate_id, {})
            accepted.append(ImageCandidate(
                id=candidate_id, url=str(job.get("source_url") or ""),
                source=str(item.get("source") or "persisted_preflight"), order=order,
                width=int(item.get("width") or 0), height=int(item.get("height") or 0),
                visible=bool(item.get("visible", True))))
        # The preflight JSON on the job is a UI-facing snapshot and may retain the
        # provisional ``source_analysis_pending`` outcome even after the durable
        # SourceAnalysisResult has been persisted as ready.  Once the durable row has
        # passed the status/TTL/lineage checks above and contains a resolved selection,
        # its ready status is authoritative for this worker rehydration.  Do not let the
        # stale presentation field send a validated job down the unsupported/pending
        # branch in apply_source_analysis; genuinely pending rows never reach here.
        from universal_chapter_adapter import (
            SUPPORTED_GENERIC_HIGH_CONFIDENCE,
            SUPPORTED_SPECIFIC_ADAPTER,
        )
        outcome = str(public.get("outcome") or "")
        if outcome not in {SUPPORTED_SPECIFIC_ADAPTER, SUPPORTED_GENERIC_HIGH_CONFIDENCE}:
            outcome = SUPPORTED_SPECIFIC_ADAPTER
        analysis = SourceAnalysis(
            adapter=result.adapter, final_host="", outcome=str(
                outcome),
            confidence=1.0, adapter_version=str(public.get("adapter_version") or ""),
            accepted=accepted, warnings=list(public.get("warnings") or []),
            collection_strategy="persisted_preflight", coverage_strategy="persisted_preflight")
        self._append_job_log(job["id"], "source_validation",
                             f"SOURCE_REUSE_RESULT status=success age_ms={int(age * 1000)} candidates={len(accepted)}")
        self._append_job_log(job["id"], "source_validation",
                             f"SOURCE_REUSE_USED analysis_result_id_present=true candidates={len(accepted)}")
        return analysis

    def _prepare_source(self, job: dict) -> dict | None:
        """Run the source phase when needed. Returns None when no runner may start.

        Returning None is the whole point of doing this before ``_spawn_runner``: a review,
        a failed analysis or a cancellation must not produce a child process that outlives
        the decision.
        """
        from source_analysis_phase import (
            has_usable_selection, should_spawn_runner,
        )

        current = self.store.get_job(job["id"]) or job
        job_type = str((current.get("configuration") or {}).get("job_type") or "translation")
        if job_type != "translation":
            # Child operations consume already-persisted artifacts. Sending them through
            # URL source readiness would either reopen a source or fail them for not
            # carrying a chapter selection, both of which violate their contract.
            return current
        if not should_spawn_runner(current):
            return None
        if str(current.get("source_type") or "") != "url":
            return current                      # local folder keeps its existing flow
        if has_usable_selection(current):
            from source_analysis_phase import ensure_command_has_source_selection

            # A previous attempt already produced a selection; re-analysing would reopen a
            # browser and could contradict the order already persisted.
            return ensure_command_has_source_selection(self.store, current)

        def cancelled() -> bool:
            row = self.store.get_job(job["id"])
            return not row or bool(row.get("cancel_requested")) or row.get("status") in (
                JobStatus.CANCELLED, JobStatus.FAILED)

        validation_started = time.monotonic()
        self._append_job_log(job["id"], "source_validation", "SOURCE_VALIDATION_STARTED")
        self.store.update_progress(
            job["id"], stage="source_validation", message="Validando fonte…",
            counter_stage="source_validation")
        self._append_job_log(job["id"], "source_validation", "Validando fonte")
        self.store.heartbeat(job["id"])
        try:
            analysis = self._reuse_persisted_source_analysis(current)
            if analysis is not None:
                self._append_job_log(job["id"], "source_validation", "SOURCE_VALIDATION_PATH=persisted_preflight")
            else:
                self._append_job_log(job["id"], "source_validation", "SOURCE_VALIDATION_PATH=full_analysis")

            def progress(event: dict) -> None:
                payload = event if isinstance(event, dict) else {}
                stage = str(payload.get("stage") or "source_lazy_resolution")
                self.store.update_progress(
                    job["id"],
                    stage=stage,
                    current=int(payload.get("current") or 0),
                    total=int(payload.get("total") or 0),
                    message=str(payload.get("message") or ""),
                    counter_stage=str(payload.get("counter_stage") or stage),
                )
                self._append_job_log(
                    job["id"], stage, str(payload.get("message") or ""))
                self.store.worker_heartbeat(self.worker_id)

            result: dict[str, object] = {"analysis": analysis} if analysis is not None else {}

            def run_analysis() -> None:
                try:
                    result["analysis"] = self._analyze_source(
                        str(current.get("source_url") or ""),
                        cancel_check=cancelled,
                        on_progress=progress)
                except BaseException as exc:  # noqa: BLE001 - re-raised on worker thread
                    result["error"] = exc

            if analysis is None:
                thread = threading.Thread(target=run_analysis, daemon=True)
                thread.start()
                deadline = time.monotonic() + SOURCE_VALIDATION_TIMEOUT_SECONDS
                while thread.is_alive():
                    self.store.worker_heartbeat(self.worker_id)
                    self.store.heartbeat(job["id"])
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("source_validation_timeout")
                    thread.join(timeout=min(WORKER_HEARTBEAT_SECONDS, 1.0, remaining))
                if "error" in result:
                    raise result["error"]  # type: ignore[misc]
                analysis = result.get("analysis")
            if analysis is None:
                raise RuntimeError("source_validation_no_result")
        except Exception as exc:  # noqa: BLE001 - coded, sanitized terminal outcome
            self._append_job_log(
                job["id"], "source_validation",
                f"SOURCE_VALIDATION_FAILED reason_code={self._source_failure_code(exc)} "
                f"duration_ms={int((time.monotonic() - validation_started) * 1000)}")
            return self._fail_source(job["id"], exc)
        # The analysis runs in a helper thread while the worker owns a CLAIMING row.
        # A shutdown/crash reconciliation can mark that row INTERRUPTED while the
        # helper is finishing.  Never apply a late result to that new lifecycle state:
        # doing so used to attempt the illegal ``interrupted -> source_analysis_ready``
        # transition.  A resumed attempt will use its durable selection/checkpoint (or
        # safely re-run analysis) through the normal claim path.
        latest = self.store.get_job(job["id"])
        if (
            latest is None
            or latest.get("status") != JobStatus.CLAIMING
            or latest.get("worker_id") != self.worker_id
            or cancelled()
        ):
            return None
        canonical_url = str(getattr(analysis, "canonical_url", "") or "")
        if canonical_url and canonical_url != str(current.get("source_url") or ""):
            # The official metadata decision becomes the sole URL used by the runner.
            # The public analysis persists only the sanitized identity fields.
            self.store.update_fields(job["id"], source_url=canonical_url)
        self._append_job_log(
            job["id"], "source_validation",
            f"SOURCE_VALIDATION_RESULT status=success duration_ms={int((time.monotonic() - validation_started) * 1000)}")
        self._append_job_log(job["id"], "runner", "POST_VALIDATION_DECISION branch=apply_source_analysis")
        try:
            prepared = self._apply_phase(job["id"])(analysis)
            current_after = self.store.get_job(job["id"])
            self._append_job_log(
                job["id"], "runner",
                f"POST_VALIDATION_RETURN prepared={bool(prepared)} "
                f"status={str((prepared or current_after or {}).get('status') or '')} "
                f"stage={str((prepared or current_after or {}).get('stage') or '')} "
                f"reason_code={str((prepared or current_after or {}).get('reason_code') or '')}")
            return prepared
        except Exception as exc:  # noqa: BLE001 - phase boundary must be explicit
            self._append_job_log(
                job["id"], "runner",
                f"POST_VALIDATION_EXCEPTION type={type(exc).__name__} "
                f"reason_code={self._source_failure_code(exc)}")
            self._append_job_log(
                job["id"], "source_validation",
                f"SOURCE_VALIDATION_FAILED reason_code={self._source_failure_code(exc)} "
                f"duration_ms={int((time.monotonic() - validation_started) * 1000)}")
            return self._fail_source(job["id"], exc)

    @staticmethod
    def _source_failure_code(exc: BaseException) -> str:
        if isinstance(exc, TimeoutError):
            return "source_validation_timeout"
        try:
            from down import _pipeline_exception_code
            return str(_pipeline_exception_code(exc) or "source_validation_failed")[:80]
        except Exception:  # noqa: BLE001
            return "source_validation_failed"

    def _apply_phase(self, job_id: str):
        """Apply the shared decision and translate it into a runner/no-runner answer."""
        from source_analysis_phase import apply_source_analysis

        def apply(analysis):
            row = self.store.get_job(job_id)
            # Source analysis is only valid for the worker's active claim.  In
            # particular, an interrupted row may be reconciled while an analysis
            # callback is still unwinding; applying it would violate the state machine.
            if row is None or row.get("status") != JobStatus.CLAIMING:
                return None
            result = apply_source_analysis(self.store, row, analysis)
            if result.outcome == "source_analysis_ready":
                from source_readiness import SourceReadinessStore

                readiness = SourceReadinessStore(self.store.db_path)
                try:
                    resolution = readiness.resolve_ready_pipeline(
                        job_id, handoff_worker_id=self.worker_id)
                finally:
                    readiness.close()
                self._append_job_log(
                    job_id, "runner",
                    f"POST_VALIDATION_JOB_STATE resolution_status={str(resolution.get('status') or '')} "
                    f"ok={bool(resolution.get('ok'))} reason_code={str(resolution.get('reason_code') or '')}")
                # The current claim ends here. A successful atomic resolution requeues
                # the same job, which the normal claim loop will acquire exactly once.
                # A disabled/revoked policy leaves it visibly fail-closed at readiness.
                if resolution.get("status") in {JobStatus.QUEUED, JobStatus.CLAIMING}:
                    # The worker-owned path keeps the row CLAIMING, preventing another
                    # worker from winning a second claim during runner creation.  The
                    # queued case remains accepted for compatibility with older stores.
                    self._append_job_log(job_id, "runner", "RUNNER_HANDOFF_BEGIN")
                    return self.store.get_job(job_id)
                self._append_job_log(
                    job_id, "runner",
                    f"POST_VALIDATION_RESOLUTION_FAILED reason_code="
                    f"{str(resolution.get('reason_code') or 'workspace_pipeline_resolution_failed')}")
                return None
            if not result.should_spawn_runner:
                return None
            return self.store.get_job(job_id)

        return apply

    def _fail_source(self, job_id: str, exc: BaseException) -> None:
        """Record a sanitized terminal source failure; never start a runner after one."""
        from down import _pipeline_exception_code

        code = _pipeline_exception_code(exc)
        try:
            if code == "cancelled":
                current = self.store.get_job(job_id)
                if current and current.get("status") in {
                    JobStatus.CLAIMING, JobStatus.STARTING, JobStatus.RUNNING,
                }:
                    self.store.transition(
                        job_id, JobStatus.CANCELLING,
                        reason_code="user_cancelled", stage="cancelling",
                        interrupted_reason="cancelled_source_analysis")
                self.store.transition(
                    job_id, JobStatus.CANCELLED, reason_code="user_cancelled", stage="cancelled",
                    interrupted_reason="cancelled_source_analysis")
                return
            preflight = getattr(exc, "preflight_result", None)
            if isinstance(preflight, dict):
                allowed = {
                    "schema_version", "normalized_url_hash", "adapter", "status",
                    "reason_code", "http_method", "http_status", "content_type",
                    "redirect_count", "final_host", "response_size_class",
                    "html_shell_detected", "javascript_required_possible",
                    "browser_inspection_allowed", "browser_inspection_reason",
                    "authentication_required", "access_restricted",
                    "captcha_detected", "security_blocked", "transport_error",
                    "elapsed_ms", "policy_hash",
                }
                sanitized = {
                    key: value for key, value in preflight.items() if key in allowed
                }
                current = self.store.get_job(job_id) or {}
                analysis = dict(current.get("source_analysis") or {})
                analysis["preflight"] = sanitized
                self.store.update_fields(
                    job_id,
                    source_analysis_json=json.dumps(
                        analysis, ensure_ascii=False, sort_keys=True),
                )
            self.store.transition(
                job_id, JobStatus.FAILED, reason_code=code, stage="source_analysis",
                error_type="source_analysis", error_message=str(code),
                **self._record_failure_diagnostic(job_id, exc, code))
        except Exception:  # noqa: BLE001 - a concurrent owner already settled it
            pass

    def _record_failure_diagnostic(
            self, job_id: str, exc: BaseException, code: str) -> dict[str, str]:
        """Persist the exception evidence and return the job field pointing at it.

        The worker's stdout/stderr are DEVNULL in production; without this the exception
        class, its chain and its origin are lost and only the coded reason survives.
        Recording evidence must never itself fail a job differently, so every error here
        degrades to "no diagnostic".
        """
        from job_failure_diagnostic import build_failure_diagnostic, write_failure_diagnostic

        try:
            row = self.store.get_job(job_id) or {}
            payload = build_failure_diagnostic(
                exc, job_id=job_id, run_id=str(row.get("run_id") or ""),
                reason_code=code, worker_pid=self.pid)
            path = write_failure_diagnostic(
                Path(self.log_dir) / f"{job_id}.failure.json", payload)
            self._append_job_log(
                job_id, "source_analysis",
                f"{code} {payload['exception_class']}: {payload['safe_message']}")
            return {"error_trace_path": str(path)}
        except Exception:  # noqa: BLE001 - evidence is best effort, the failure is not
            return {}

    def _run_one(self, job: dict) -> None:
        # A URL job whose source has not been analysed yet is analysed here, in the worker,
        # before any child process exists. The submit request must not own a browser, and a
        # review or terminal outcome must not leave a runner behind.
        job = self._prepare_source(job)
        if job is None:
            return
        if not self._runner_input_ready(job):
            return
        proc = self._spawn_runner(job)
        # Own the unambiguously spawned child immediately.  There is a small but real
        # window before runner_pid reaches SQLite; if that write fails, this handle is
        # the only safe way to prevent an untracked runner from surviving.
        self._active = {
            "proc": proc,
            "job_id": job["id"],
            "fingerprint": self._runner_fingerprint(job["id"], job),
        }
        # Persist the top of the runner tree (the process we spawned) so a later stop or
        # reconcile terminates the whole tree - including the launcher shim the venv
        # inserts as the parent of the real interpreter - not just a subtree of it.
        try:
            snap = process_tree.snapshot(proc.pid)
            self.store.update_fields(
                job["id"], runner_pid=proc.pid,
                runner_create_time=(snap or {}).get("create_time"),
            )
            gate_path = getattr(proc, "_tradutor_start_gate", None)
            if gate_path is not None:
                fd = os.open(str(gate_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
        except Exception:  # noqa: BLE001 - the child must not outlive PID persistence
            try:
                self._stop_active_runner(job["id"])
            finally:
                self._active = None
            return
        self._append_job_log(job["id"], "runner", "RUNNER_HANDOFF_RESULT status=success")
        try:
            # Keep the worker's lease alive while the runner owns the job's heartbeat.
            while proc.poll() is None:
                self.store.worker_heartbeat(self.worker_id)
                if self._should_stop():
                    self._stop_requested = True
                    self._stop_active_runner(job["id"])
                    break
                time.sleep(min(WORKER_HEARTBEAT_SECONDS, 1.0))
            self.store.worker_heartbeat(self.worker_id)
        finally:
            try:
                # Any exception in worker-side heartbeat/accounting must not abandon a
                # live child.  Conversely, a child that exited before the runner could
                # transition its row must not leave a job owned by this healthy worker
                # in CLAIMING/STARTING/RUNNING forever.
                if proc.poll() is None:
                    try:
                        self._stop_active_runner(job["id"])
                    except Exception:  # noqa: BLE001 - Popen handle is authoritative
                        process_tree.terminate_tree(proc.pid, timeout=8.0)
                        try:
                            proc.wait(timeout=5)
                        except (subprocess.TimeoutExpired, OSError, ValueError):
                            pass
                if proc.poll() is not None:
                    self._reconcile_runner_exit(job["id"])
            finally:
                gate_path = getattr(proc, "_tradutor_start_gate", None)
                if gate_path is not None:
                    try:
                        Path(gate_path).unlink(missing_ok=True)
                    except OSError:
                        pass
                self._active = None

    def _runner_input_ready(self, job: dict) -> bool:
        """Fail closed before spawning a runner with incomplete URL source evidence."""
        from source_analysis_phase import has_usable_selection

        if str((job or {}).get("source_type") or "") != "url":
            return True
        if has_usable_selection(job):
            return True
        try:
            self.store.transition(
                job["id"], JobStatus.FAILED, expected_worker=self.worker_id,
                stage="source_selection", reason_code="missing_source_selection",
                error_type="source_analysis", error_message="missing_source_selection")
        except Exception:  # noqa: BLE001 - a concurrent owner may already have settled it
            pass
        return False

    def _reconcile_runner_exit(self, job_id: str) -> None:
        """Close worker-owned accounting when a runner exits before terminalizing."""
        fresh = self.store.get_job(job_id)
        if not fresh or fresh["status"] not in JobStatus.IN_FLIGHT:
            return
        if not fresh.get("cancel_requested"):
            # CLAIMING means the child never crossed its own trusted start boundary
            # (for example import/start-gate failure). Retrying the identical binary in
            # a tight loop cannot make progress. Later-stage crashes get bounded,
            # delayed recovery in _recover_interrupted_community_publishes.
            recoverable = int(fresh["status"] != JobStatus.CLAIMING)
            self.store.transition(
                job_id,
                JobStatus.INTERRUPTED,
                expected_worker=self.worker_id,
                interrupted_reason="runner_exited_before_terminal",
                recoverable=recoverable,
                writer_component="worker_service._reconcile_runner_exit",
            )
            return

        current = fresh["status"]
        if current in {JobStatus.STARTING, JobStatus.RUNNING}:
            self.store.transition(
                job_id,
                JobStatus.CANCELLING,
                expected_worker=self.worker_id,
            )
        elif current == JobStatus.CLAIMING:
            self.store.transition(
                job_id,
                JobStatus.INTERRUPTED,
                expected_worker=self.worker_id,
                interrupted_reason="cancel_requested",
                recoverable=0,
            )
        self.store.transition(
            job_id,
            JobStatus.CANCELLED,
            expected_worker=self.worker_id,
            interrupted_reason="cancel_requested",
        )

    def _stop_active_runner(self, job_id: str) -> None:
        """On worker stop, take the active runner's whole tree down and interrupt the job.

        The runner runs in its own process group, so a signal to the worker never reaches
        it; the worker must stop it explicitly. The tree is validated by the runner's
        command fingerprint before anything is terminated, so no unrelated process dies.
        """
        active = self._active
        proc = active["proc"] if active else None
        try:
            job = self.store.get_job(job_id)
        except Exception:  # noqa: BLE001 - the owned Popen handle still permits cleanup
            job = None
        runner_pid = (job or {}).get("runner_pid") or (proc.pid if proc else None)
        fingerprint = (active or {}).get("fingerprint")
        if not fingerprint:
            fingerprint = self._runner_fingerprint(job_id, job) if job else []
        report = process_tree.terminate_tree(
            runner_pid,
            create_time=(job or {}).get("runner_create_time"),
            substrings=fingerprint,
            timeout=8.0,
        )
        # If the store did not yet know the runner pid (very early), fall back to the
        # handle we hold, which is unambiguously our child.
        if report["reason"] in {"ownership_mismatch", "not_running", "no_pid"} and proc and proc.poll() is None:
            process_tree.terminate_tree(proc.pid, timeout=8.0)
        if proc:
            try:
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass
        # The runner itself moves the job to interrupted on its stop signal; if it died
        # before doing so, close the accounting here.
        try:
            fresh = self.store.get_job(job_id)
        except Exception:  # noqa: BLE001 - cleanup succeeded; accounting retries later
            fresh = None
        if fresh and fresh["status"] in JobStatus.IN_FLIGHT:
            reason = "cancel_requested" if fresh.get("cancel_requested") else "worker_stop"
            target = JobStatus.CANCELLING if fresh.get("cancel_requested") else JobStatus.INTERRUPTED
            try:
                if target == JobStatus.CANCELLING:
                    self.store.transition(job_id, JobStatus.CANCELLING, expected_worker=self.worker_id)
                    self.store.transition(job_id, JobStatus.CANCELLED, expected_worker=self.worker_id,
                                          interrupted_reason=reason)
                else:
                    self.store.transition(job_id, JobStatus.INTERRUPTED, expected_worker=self.worker_id,
                                          interrupted_reason=reason, recoverable=1)
            except Exception:  # noqa: BLE001 - the runner may have finalized concurrently
                pass

    def _reconcile_stale(self) -> bool:
        """Recover jobs whose owning worker died, taking down any orphaned runner tree.

        A crashed worker can leave its runner alive - even heartbeating a stuck pipeline -
        so the signal is the owning worker's lease being gone, not the job heartbeat. A
        live, validated runner is stopped first so it cannot keep processing or later
        finalize the job; a reused or foreign PID is never touched and the job is flagged
        ownership_mismatch. This guarantees no orphan and never a second live attempt.
        """
        orphans = self.store.orphaned_in_flight_jobs(
            exclude_worker=self.worker_id, worker_stale_seconds=self.stale_seconds / 2,
            worker_contract=self.worker_contract,
        )
        safe_to_continue = True
        for job in orphans:
            job_id = job["id"]
            runner_pid = job.get("runner_pid")
            fingerprint = self._runner_fingerprint(job_id, job)
            alive = process_tree.is_alive(
                runner_pid, create_time=job.get("runner_create_time"), substrings=fingerprint
            )
            reused = bool(runner_pid) and process_tree.snapshot(runner_pid) is not None and not alive
            reason = "orphaned_worker_gone"
            if alive:
                report = process_tree.terminate_tree(
                    runner_pid, create_time=job.get("runner_create_time"),
                    substrings=fingerprint, timeout=8.0,
                )
                if report["reason"] != "stopped":
                    # Never claim/requeue more work while a validated old runner may
                    # still be alive.  The next poll retries reconciliation.
                    safe_to_continue = False
                    continue
                reason = "reconciled_live_runner"
            elif reused:
                # PID belongs to some other process now: fail closed, do not terminate it.
                reason = "ownership_mismatch"
            try:
                if job.get("cancel_requested"):
                    current = self.store.get_job(job_id)
                    if current and current.get("status") in {
                        JobStatus.CLAIMING, JobStatus.STARTING, JobStatus.RUNNING,
                    }:
                        self.store.transition(
                            job_id, JobStatus.CANCELLING,
                            interrupted_reason="cancelled_worker_gone",
                            reason_code="user_cancelled",
                            stage="cancelling",
                        )
                    self.store.transition(
                        job_id, JobStatus.CANCELLED,
                        interrupted_reason="cancelled_worker_gone",
                        reason_code="user_cancelled",
                        stage="cancelled",
                        recoverable=0,
                    )
                else:
                    self.store.transition(job_id, JobStatus.INTERRUPTED,
                                          interrupted_reason=reason, recoverable=1)
            except Exception:  # noqa: BLE001 - another worker may have won the reconcile
                pass
        return safe_to_continue

    def _recover_interrupted_community_publishes(self) -> bool:
        """Requeue only a still-current publish after its old runner is confirmed gone.

        Generic UI resume deliberately refuses community jobs.  Recovery therefore
        lives in the trusted worker and validates the post/file/job linkage before a
        new runner can be claimed.  Invalidated or cancelled attempts are terminalized
        without constructing a storage provider.
        """
        from community_store import CommunityStore

        safe_to_continue = True
        candidates = self.store.list_jobs(
            statuses=[JobStatus.INTERRUPTED, JobStatus.RESUMABLE],
            limit=None,
        )
        for listed in candidates:
            config = listed.get("configuration") or {}
            if config.get("job_type") != "community_publish":
                continue
            job_id = listed["id"]
            current = self.store.get_job(job_id)
            if not current or current["status"] not in {
                JobStatus.INTERRUPTED,
                JobStatus.RESUMABLE,
            }:
                continue

            runner_pid = current.get("runner_pid")
            fingerprint = self._runner_fingerprint(job_id, current)
            if process_tree.is_alive(
                runner_pid,
                create_time=current.get("runner_create_time"),
                substrings=fingerprint,
            ):
                report = process_tree.terminate_tree(
                    runner_pid,
                    create_time=current.get("runner_create_time"),
                    substrings=fingerprint,
                    timeout=8.0,
                )
                if report["reason"] != "stopped":
                    safe_to_continue = False
                    continue

            community = None
            post_id = str(config.get("post_id") or "")
            file_id = str(config.get("file_id") or "")
            valid = False
            cancelled = bool(current.get("cancel_requested"))
            recovery_allowed = bool(current.get("recoverable")) or (
                current["status"] == JobStatus.RESUMABLE
            )
            runner_exit = (
                current.get("interrupted_reason") == "runner_exited_before_terminal"
            )
            attempt = max(1, int(current.get("attempt") or 1))
            state = "invalid"
            validation_completed = False
            try:
                if post_id and file_id and config.get("community_db"):
                    community = CommunityStore(config["community_db"])
                    state = community.publish_attempt_state(post_id, file_id, job_id)
                    validation_completed = True
                    if state == "completed":
                        self.store.reconcile_community_publish_terminal(
                            job_id,
                            JobStatus.FINISHED,
                        )
                        continue
                    if state == "failed":
                        self.store.reconcile_community_publish_terminal(
                            job_id,
                            JobStatus.FAILED,
                        )
                        continue
                    valid = state == "active"
                    if cancelled and valid:
                        community.fail_publish_attempt(
                            post_id=post_id,
                            file_id=file_id,
                            upload_job_id=job_id,
                            actor_id=str(config.get("user_id") or ""),
                            reason="recovery_cancelled",
                        )
                    elif not recovery_allowed and valid:
                        community.fail_publish_attempt(
                            post_id=post_id,
                            file_id=file_id,
                            upload_job_id=job_id,
                            actor_id=str(config.get("user_id") or ""),
                            reason="publish_not_recoverable",
                        )
                        valid = False
                    elif (
                        runner_exit
                        and recovery_allowed
                        and valid
                        and attempt >= COMMUNITY_RUNNER_MAX_ATTEMPTS
                    ):
                        community.fail_publish_attempt(
                            post_id=post_id,
                            file_id=file_id,
                            upload_job_id=job_id,
                            actor_id=str(config.get("user_id") or ""),
                            reason="runner_retry_exhausted",
                        )
                        recovery_allowed = False
                        valid = False
                    elif not valid:
                        community.invalidate_publish_file(file_id, job_id)
                else:
                    validation_completed = True
            except Exception:  # noqa: BLE001 - unavailable cross-DB state must be retried
                safe_to_continue = False
                continue
            finally:
                if community is not None:
                    community.close()

            if not validation_completed:
                safe_to_continue = False
                continue

            current = self.store.get_job(job_id)
            if not current or current["status"] not in {
                JobStatus.INTERRUPTED,
                JobStatus.RESUMABLE,
            }:
                continue
            try:
                if cancelled or not valid:
                    target = (
                        JobStatus.FAILED
                        if not recovery_allowed
                        and current["status"] == JobStatus.INTERRUPTED
                        else JobStatus.CANCELLED
                    )
                    self.store.transition(
                        job_id,
                        target,
                        expected_worker=current.get("worker_id"),
                        error_type="community_publish_recovery",
                        error_message=(
                            "cancelled_before_recovery" if cancelled
                            else "publish_not_recoverable" if not recovery_allowed
                            else "invalid_publish_attempt"
                        ),
                    )
                    continue
                if current["status"] == JobStatus.INTERRUPTED:
                    current = self.store.mark_resumable(
                        job_id,
                        resume_from_stage=current.get("stage") or "uploading",
                    )
                self.store.transition(
                    job_id,
                    JobStatus.QUEUED,
                    expected_worker=current.get("worker_id"),
                    stage="requeued",
                    queued_at=(
                        time.time()
                        + COMMUNITY_RUNNER_RETRY_BACKOFF_SECONDS
                        * (2 ** max(0, attempt - 1))
                        if runner_exit
                        else time.time()
                    ),
                    attempt=attempt + 1 if runner_exit else attempt,
                    worker_id=None,
                    worker_pid=None,
                    worker_create_time=None,
                    runner_pid=None,
                    runner_create_time=None,
                    interrupted_reason="",
                )
            except Exception:  # noqa: BLE001 - another worker/state change won the CAS
                pass
        return safe_to_continue

    def _recover_staged_community_publishes(
        self,
        *,
        grace_seconds: float = STAGING_GRACE_SECONDS,
    ) -> bool:
        """Finish or discard stale non-claimable jobs left by a hard-killed API."""
        from community_store import CommunityStore

        safe_to_continue = True
        cutoff = time.time() - max(0.0, float(grace_seconds))
        for current in self.store.list_jobs(statuses=[JobStatus.STAGING], limit=None):
            if float(current.get("created_at") or 0) > cutoff:
                continue
            owner_alive = self._staging_owner_alive(current)
            config = current.get("configuration") or {}
            if config.get("job_type") != "community_publish":
                # A live source-analysis owner is still deciding whether this row should
                # become QUEUED.  Its wall time may legitimately exceed the short staging
                # recovery grace, so only an absent/dead owner makes it recoverable here.
                if owner_alive:
                    continue
                try:
                    self.store.transition(current["id"], JobStatus.CANCELLED)
                except Exception:
                    pass
                continue
            job_id = current["id"]
            post_id = str(config.get("post_id") or "")
            file_id = str(config.get("file_id") or "")
            community = None
            try:
                if not (post_id and file_id and config.get("community_db")):
                    state = "invalid"
                else:
                    community = CommunityStore(config["community_db"])
                    state = community.publish_attempt_state(post_id, file_id, job_id)
                # A live API PID protects only the short unlinked creation window.  A
                # linked active/terminal community state is authoritative and must be
                # reconciled even if both the queue transition and lease-clear writes
                # failed while the long-lived API process stayed up.
                if state == "invalid" and owner_alive:
                    continue
                if state == "active":
                    self.store.transition(
                        job_id,
                        JobStatus.QUEUED,
                        queued_at=time.time(),
                        stage="recovered_staging",
                        worker_pid=None,
                        worker_create_time=None,
                    )
                elif state == "completed":
                    self.store.reconcile_community_publish_terminal(
                        job_id, JobStatus.FINISHED)
                elif state == "failed":
                    self.store.reconcile_community_publish_terminal(
                        job_id, JobStatus.FAILED)
                else:
                    if community is not None:
                        community.invalidate_publish_file(file_id, job_id)
                    self.store.transition(
                        job_id,
                        JobStatus.CANCELLED,
                        error_type="community_publish_staging",
                        error_message="staging_link_missing",
                    )
            except Exception:  # noqa: BLE001 - leave STAGING for a later safe retry
                safe_to_continue = False
            finally:
                if community is not None:
                    community.close()
        return safe_to_continue

    def run(self, *, once: bool = False, max_idle_cycles: int | None = None) -> None:
        if self.another_worker_alive():
            print("another healthy worker is running; exiting cleanly")
            return
        self._install_signal_handlers()
        self.store.register_worker(
            self.worker_id, self.pid,
            create_time=process_tree.snapshot(self.pid)["create_time"] if process_tree.snapshot(self.pid) else None,
            worker_contract=self.worker_contract,
        )
        idle = 0
        try:
            while not self._stop_requested:
                self.store.worker_heartbeat(self.worker_id)
                if self.store.worker_stop_requested(self.worker_id):
                    self._stop_requested = True
                    break
                if not self._reconcile_stale():
                    if once:
                        return
                    time.sleep(self.poll_seconds)
                    continue
                if not self._recover_staged_community_publishes():
                    if once:
                        return
                    time.sleep(self.poll_seconds)
                    continue
                if not self._recover_interrupted_community_publishes():
                    if once:
                        return
                    time.sleep(self.poll_seconds)
                    continue
                snap = process_tree.snapshot(self.pid) or {}
                job = self.store.claim_next_job(
                    self.worker_id, self.pid, worker_create_time=snap.get("create_time"),
                    worker_contract=self.worker_contract)
                if job is None:
                    if once:
                        return
                    idle += 1
                    if max_idle_cycles is not None and idle >= max_idle_cycles:
                        return
                    time.sleep(self.poll_seconds)
                    continue
                idle = 0
                self._append_job_log(job["id"], "worker_job_discovered", "WORKER_JOB_DISCOVERED")
                self._append_job_log(job["id"], "worker_job_claim_result", "WORKER_JOB_CLAIM_RESULT status=success")
                self._run_one(job)
                if once:
                    return
        finally:
            self.store.unregister_worker(self.worker_id)


def print_status(db_path: Path) -> None:
    store = JobStore(db_path)
    try:
        healthy = store.healthy_worker(stale_seconds=STALE_SECONDS / 2)
        active = store.active_job()
        queued = store.list_jobs(statuses=[JobStatus.QUEUED])
        resumable = store.list_jobs(statuses=[JobStatus.RESUMABLE, JobStatus.INTERRUPTED])
        print(f"worker: {'online ' + healthy['worker_id'] if healthy else 'offline'}")
        print(f"active job: {active['id'] if active else 'none'} "
              f"({active['status'] if active else '-'})")
        print(f"queued: {len(queued)} | resumable/interrupted: {len(resumable)}")
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    if not load_local_environment_for_entrypoint():
        return 2
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=POLL_SECONDS)
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if args.status:
        print_status(db_path)
        return 0

    worker = Worker(db_path, poll_seconds=args.poll_interval)
    try:
        worker.run(once=args.once)
    finally:
        worker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
