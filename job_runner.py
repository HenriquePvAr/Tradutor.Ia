"""Runner for exactly one job, spawned by the worker as an isolated subprocess.

Owns a single chapter's lifecycle: it writes an initial manifest immediately (so an
interrupted run still leaves a record), marks the job running, launches the actual
pipeline command with output streamed to a per-job log, updates progress and heartbeat
in the store, honours cooperative cancellation, and derives the terminal status from the
artifacts the pipeline produced. A crash here is contained to one job - the worker keeps
running.

Invoked as::

    python -u job_runner.py --job-id <id> --db <path> --worker-id <id> --log <path>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import process_tree
from job_store import JobStatus, JobStore, TransitionError
from process_options import build_background_process_options
from output_manifest import sanitize_source_url
from runner_start_gate import wait_for_start_gate
from ui_helpers import (
    ProgressSnapshot,
    derive_final_run_status,
    find_output_artifacts,
    load_json,
    parse_progress_line,
    sanitize_diagnostic_text,
)

HEARTBEAT_SECONDS = 3.0
CANCEL_GRACE_SECONDS = 8.0
CANCELLED_EXIT_CODE = 130
_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_PROVENANCE_TEXT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")

# Set when the runner is signalled to stop (by the worker or the OS); the poll loop
# observes it, stops the pipeline tree and interrupts the job instead of finishing.
_STOP_REQUESTED = False


def _install_stop_handlers() -> None:
    def _handler(signum, _frame):
        global _STOP_REQUESTED
        _STOP_REQUESTED = True

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _safe_reason_code(value: object, fallback: str) -> str:
    """Return only a bounded machine reason, never provider text or a request URL."""
    candidate = str(value or "").strip().casefold()
    return candidate if _REASON_CODE_RE.fullmatch(candidate) else fallback


def _safe_provenance_text(value: object) -> str:
    text = str(value or "").strip()
    return text if _PROVENANCE_TEXT_RE.fullmatch(text) else ""


def _safe_provenance_count(value: object) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 10_000 else None


def _safe_provenance_score(value: object) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return score if 0.0 <= score <= 1.0 else None


def _fresh_source_provenance(
    download_report: object,
    source_analysis: object,
) -> dict[str, object]:
    """Project a fresh, sanitized downloader diagnosis into indexed job fields."""

    report = download_report if isinstance(download_report, dict) else {}
    analysis = source_analysis if isinstance(source_analysis, dict) else {}
    fields: dict[str, object] = {}
    source_type = str(report.get("source_type") or "").strip()
    if source_type in {"url", "local_folder"}:
        fields["source_type"] = source_type
    for field, value in (
        ("adapter_name", report.get("adapter_name") or analysis.get("adapter")),
        ("adapter_version", report.get("adapter_version") or analysis.get("adapter_version")),
        ("transport_name", report.get("transport_name")),
    ):
        safe = _safe_provenance_text(value)
        if safe:
            fields[field] = safe
    score = _safe_provenance_score(analysis.get("confidence"))
    if score is not None:
        fields["source_score"] = score
    candidate_count = _safe_provenance_count(analysis.get("candidate_count"))
    if candidate_count is not None:
        fields["candidate_count"] = candidate_count
        fields["input_count"] = candidate_count
    accepted_count = _safe_provenance_count(analysis.get("accepted_count"))
    if accepted_count is not None:
        fields["accepted_count"] = accepted_count
    rejected_count = _safe_provenance_count(analysis.get("discarded_count"))
    if rejected_count is not None:
        fields["rejected_count"] = rejected_count
    return fields


def _safe_job_provenance(job: object) -> dict[str, object]:
    """Return the output-safe scalar source record, never URLs/cookies/selectors."""

    row = job if isinstance(job, dict) else {}
    return {
        "source_type": "local_folder" if row.get("source_type") == "local_folder" else "url",
        "adapter_name": _safe_provenance_text(row.get("adapter_name")),
        "adapter_version": _safe_provenance_text(row.get("adapter_version")),
        "transport_name": _safe_provenance_text(row.get("transport_name")),
        "score": _safe_provenance_score(row.get("source_score")),
        "candidate_count": _safe_provenance_count(row.get("candidate_count")) or 0,
        "accepted_page_count": _safe_provenance_count(row.get("accepted_count")) or 0,
        "rejected_page_count": _safe_provenance_count(row.get("rejected_count")) or 0,
    }


def _write_manifest(output_dir: Path, job: dict, **updates) -> None:
    manifest = {
        "job_manifest_version": 1,
        "job_id": job["id"],
        "run_id": job["run_id"],
        "status": job["status"],
        "stage": job.get("stage"),
        "source_url": sanitize_source_url(str(job.get("source_url") or "")),
        "output_dir": str(output_dir),
        "commit_hash": job.get("commit_hash"),
        "branch": job.get("branch"),
        "attempt": job.get("attempt"),
        "source_provenance": _safe_job_provenance(job),
        "configuration": _safe_manifest_configuration(job.get("configuration") or {}),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "updated_at": time.time(),
    }
    manifest.update(updates)
    _atomic_write_json(output_dir / "job_manifest.json", manifest)


def _safe_manifest_configuration(configuration: object) -> dict:
    """Whitelist harmless run settings before copying them to an output artifact."""
    if not isinstance(configuration, dict):
        return {}
    allowed = {
        "job_type", "mode", "full", "max_images", "use_cache", "force",
        "use_context", "chapter_name", "open_output", "create_source_profile",
    }
    safe = {key: configuration[key] for key in allowed if key in configuration}
    if "create_source_profile" in safe:
        safe["create_source_profile"] = configuration.get("create_source_profile") is True
    if "chapter_name" in safe:
        # A title is user-controlled and can itself be an accidental signed URL. It remains
        # readable when ordinary text, but receives the same diagnostic redaction as logs.
        safe["chapter_name"] = sanitize_diagnostic_text(str(safe["chapter_name"]))[:120]
    return safe


def _profile_creation_is_authorized(
    configuration: object,
    source_selection: object,
) -> bool:
    """Return whether a completed job may create a reusable source profile.

    An automatic source selection can be used for the current run, but is not
    reusable evidence.  Creating a profile requires a strict, explicit opt-in
    and a confirmed manual selection from the completed download report.
    """
    if not isinstance(configuration, dict):
        return False
    if configuration.get("create_source_profile") is not True:
        return False
    if not isinstance(source_selection, dict):
        return False
    if source_selection.get("automatic") is not False:
        return False

    candidate_ids = source_selection.get("candidate_ids")
    return isinstance(candidate_ids, list) and any(
        isinstance(candidate_id, str) and candidate_id.strip()
        for candidate_id in candidate_ids
    )


class _OutputPump(threading.Thread):
    """Read the pipeline's output on a thread so cancellation never waits on it."""

    def __init__(self, stream, log_handle, store: JobStore, job_id: str):
        super().__init__(daemon=True)
        self.stream = stream
        self.log_handle = log_handle
        self.store = store
        self.job_id = job_id
        self.snapshot = ProgressSnapshot()
        self._lock = threading.Lock()
        self.dirty = False

    def run(self) -> None:
        for raw in iter(self.stream.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            masked = sanitize_diagnostic_text(line)
            timestamp = time.strftime("%H:%M:%S")
            self.log_handle.write(f"{timestamp} {masked}\n")
            self.log_handle.flush()
            with self._lock:
                self.snapshot = parse_progress_line(masked, self.snapshot)
                self.dirty = True

    def drain_progress(self) -> ProgressSnapshot | None:
        with self._lock:
            if not self.dirty:
                return None
            self.dirty = False
            return ProgressSnapshot(**vars(self.snapshot))


def _terminate(proc: subprocess.Popen) -> None:
    """Stop the pipeline and its whole tree: cooperative signal, then a validated kill.

    The pipeline runs in its own process group and may itself spawn children (a launcher
    shim, a browser driver). A cooperative CTRL_BREAK reaches its group first; anything
    still alive - including descendants the direct handle does not cover - is then stopped
    by the process-tree helper, which we hold the live handle for so ownership is certain.
    """
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
    except (OSError, ValueError):
        pass
    try:
        proc.wait(timeout=CANCEL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    if proc.poll() is None:
        # Recursively terminate the pipeline tree by its verified live handle.
        process_tree.terminate_tree(proc.pid, timeout=CANCEL_GRACE_SECONDS)
    try:
        proc.wait(timeout=2)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass


def run_job(job_id: str, db_path: str, worker_id: str, log_path: str) -> int:
    _install_stop_handlers()
    store = JobStore(db_path)
    try:
        job = store.get_job(job_id)
        if job is None:
            return 2
        if worker_id and job.get("worker_id") not in (worker_id, None):
            print(f"job {job_id} not owned by {worker_id}", file=sys.stderr)
            return 2
        if job["status"] not in {JobStatus.CLAIMING, JobStatus.STARTING}:
            print(f"job {job_id} not startable from {job['status']}", file=sys.stderr)
            return 2
        if store.cancel_requested(job_id):
            store.transition(job_id, JobStatus.CANCELLING, expected_worker=job.get("worker_id"))
            store.transition(job_id, JobStatus.CANCELLED, expected_worker=job.get("worker_id"),
                             reason_code="user_cancelled")
            return 0

        output_dir = Path(job["output_dir"]).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        command = list(job.get("command") or [])
        if not command:
            store.transition(job_id, JobStatus.FAILED, error_type="config",
                             error_message="invalid_job_command",
                             reason_code="invalid_job_command")
            return 2

        # Ensure the job is STARTING then RUNNING, and write the initial manifest. The
        # worker owns runner_pid/runner_create_time: it records the top of the runner tree
        # (the process it spawned), so the recovery termination catches the whole tree
        # including the venv launcher shim. The runner does not set them, to avoid racing
        # the worker with its own (child) PID.
        if job["status"] == JobStatus.CLAIMING:
            job = store.transition(job_id, JobStatus.STARTING, expected_worker=job.get("worker_id"))
        job = store.transition(
            job_id, JobStatus.RUNNING, expected_worker=job.get("worker_id"),
            log_path=log_path, stage="created",
        )
        _write_manifest(output_dir, job)

        log_file = Path(log_path)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        with log_file.open("a", encoding="utf-8") as handle:
            # The command contains the submitted URL and can contain signed query values.
            # Keep an auditable event without persisting protected process arguments.
            handle.write(f"{time.strftime('%H:%M:%S')} pipeline iniciado (argumentos protegidos)\n")
            handle.flush()
            try:
                proc = subprocess.Popen(command, **build_background_process_options(
                    cwd=str(Path.cwd()), stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, env=env,
                ))
            except OSError:
                failed = store.transition(
                    job_id, JobStatus.FAILED, expected_worker=job.get("worker_id"),
                    exit_code=127, error_type="runner", error_message="runner_start_failed",
                    reason_code="runner_start_failed",
                )
                _write_manifest(output_dir, failed, status=JobStatus.FAILED,
                                exit_code=127, reason_code="runner_start_failed")
                return 2
            store.update_fields(job_id, runner_pid=os.getpid())
            pump = _OutputPump(proc.stdout, handle, store, job_id)
            pump.start()

            cancelled = False
            interrupted = False
            while proc.poll() is None:
                time.sleep(min(HEARTBEAT_SECONDS, 1.0))
                snap = pump.drain_progress()
                if snap is not None:
                    store.update_progress(
                        job_id, stage=snap.stage, current=int(snap.current or 0),
                        total=int(snap.total or 0), message=snap.last_message,
                        counter_stage=snap.counter_stage,
                    )
                else:
                    store.heartbeat(job_id)
                if not cancelled and store.cancel_requested(job_id):
                    cancelled = True
                    handle.write(f"{time.strftime('%H:%M:%S')} cancelamento solicitado\n")
                    handle.flush()
                    try:
                        store.transition(job_id, JobStatus.CANCELLING,
                                         expected_worker=job.get("worker_id"))
                    except TransitionError:
                        pass
                    _terminate(proc)
                elif not cancelled and _STOP_REQUESTED:
                    # The worker or the OS asked this runner to stop. Preserve checkpoints,
                    # stop the pipeline tree, and let finalize mark the job interrupted.
                    interrupted = True
                    handle.write(f"{time.strftime('%H:%M:%S')} parada solicitada; encerrando pipeline\n")
                    handle.flush()
                    _terminate(proc)
                    break
            pump.join(timeout=2)
            return_code = proc.wait()

        return _finalize(store, job_id, job, output_dir, return_code, cancelled,
                         log_path, interrupted=interrupted)
    finally:
        store.close()


def _finalize(store, job_id, job, output_dir, return_code, cancelled, log_path,
              *, interrupted: bool = False) -> int:
    effective_return_code = CANCELLED_EXIT_CODE if cancelled else int(return_code)
    artifacts = find_output_artifacts(output_dir)
    report = load_json(output_dir / "timing_report.json")
    download_report = load_json(output_dir / "downloaded_images.json")
    quality = report.get("quality_validation") or {}
    technical_ok = (
        return_code == 0 and bool(artifacts.get("pdf_path"))
        and not cancelled and not interrupted
    )
    if cancelled:
        target = JobStatus.CANCELLED
    elif interrupted:
        # An operational stop is not a failure: the chapter can be resumed. RUNNING
        # transitions straight to INTERRUPTED (a permitted, recoverable outcome).
        target = JobStatus.INTERRUPTED
    else:
        status = derive_final_run_status(
            technical_success=technical_ok, cancelled=False, quality_validation=quality,
        )
        target = {
            "finished": JobStatus.FINISHED,
            "review_required": JobStatus.REVIEW_REQUIRED,
            "error": JobStatus.FAILED,
        }.get(status, JobStatus.FAILED)

    failure = download_report.get("failure") if isinstance(download_report, dict) else {}
    source_reason = _safe_reason_code(
        failure.get("code") if isinstance(failure, dict) else "", "pipeline_failed")
    if cancelled:
        reason_code = "user_cancelled"
    elif interrupted:
        reason_code = "worker_stop"
    elif target == JobStatus.FINISHED:
        reason_code = "completed"
    elif target == JobStatus.REVIEW_REQUIRED:
        reason_code = "quality_review_required"
    else:
        reason_code = source_reason

    fields = {
        "exit_code": effective_return_code,
        "pdf_path": artifacts.get("pdf_path") or "",
        "quality_report_path": artifacts.get("quality_report_path") or "",
        "manifest_path": artifacts.get("manifest_path") or "",
        "progress_path": str(output_dir / "progress.json"),
        "reason_code": reason_code,
    }
    fresh_analysis = download_report.get("source_analysis") if isinstance(download_report, dict) else None
    fresh_selection = download_report.get("source_selection") if isinstance(download_report, dict) else None
    if isinstance(fresh_analysis, dict):
        fields["source_analysis_json"] = json.dumps(fresh_analysis, ensure_ascii=False)
    if isinstance(fresh_selection, dict):
        fields["source_selection_json"] = json.dumps(fresh_selection, ensure_ascii=False)
    fields.update(_fresh_source_provenance(download_report, fresh_analysis))
    if target == JobStatus.FAILED:
        fields["error_type"] = "source" if source_reason != "pipeline_failed" else "pipeline"
        fields["error_message"] = reason_code
    if target == JobStatus.INTERRUPTED:
        fields["interrupted_reason"] = "worker_stop"
        fields["recoverable"] = 1
    try:
        job = store.transition(job_id, target, expected_worker=job.get("worker_id"), **fields)
    except TransitionError as exc:
        print(f"finalize transition failed: {exc}", file=sys.stderr)
        return 1
    _write_manifest(output_dir, job, status=target,
                    pdf_path=artifacts.get("pdf_path") or "", exit_code=effective_return_code,
                    reason_code=reason_code)
    if (
        target == JobStatus.FINISHED
        and isinstance(fresh_analysis, dict)
        and isinstance(fresh_selection, dict)
        and _profile_creation_is_authorized(job.get("configuration"), fresh_selection)
    ):
        # A profile is created only after a technically complete, quality-approved generic
        # run.  It stores fresh evidence, never a URL/cookie/query or an access grant.
        try:
            from source_profile import SourceProfileStore

            SourceProfileStore().record_success(
                fresh_analysis,
                fresh_selection,
            )
        except Exception:
            pass
    # The runner process completed its terminalization successfully. The job's
    # persisted exit_code carries the normalized cancellation contract; the
    # supervisor itself keeps the historical zero return for handled outcomes.
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--worker-id", default="")
    parser.add_argument("--log", required=True)
    parser.add_argument("--start-gate", default="")
    args = parser.parse_args(argv)
    if not wait_for_start_gate(args.start_gate):
        return 3
    return run_job(args.job_id, args.db, args.worker_id, args.log)


if __name__ == "__main__":
    raise SystemExit(main())
