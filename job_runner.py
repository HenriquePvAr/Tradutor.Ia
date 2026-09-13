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
import hashlib
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
from beta_license import LicenseState
from runner_start_gate import wait_for_start_gate
from runtime_paths import runtime_root
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


def _append_pipeline_trace(job: dict, event: str, **fields: object) -> None:
    """Write bounded, secret-free runner lifecycle evidence for diagnostics export."""
    try:
        # Resolve through the shared runtime-path policy so hermetic tests and
        # frozen runs never leak diagnostics into the developer's real profile.
        root = runtime_root() / "diagnostics"
        safe_job = hashlib.sha256(str(job.get("id") or "").encode()).hexdigest()[:12]
        payload = {"timestamp": time.time(), "event": str(event),
                   "trace_id": str((job.get("configuration") or {}).get("trace_id") or "")[:80],
                   "job_id": safe_job, **fields}
        root.mkdir(parents=True, exist_ok=True)
        with (root / f"pipeline_{safe_job}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
    except (OSError, TypeError, ValueError):
        pass


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


def _merge_terminal_source_analysis(
    fresh_analysis: object, previous_analysis: object,
) -> dict[str, object]:
    """Keep operation-level evidence when the downloader refreshes page evidence."""
    fresh = dict(fresh_analysis) if isinstance(fresh_analysis, dict) else {}
    previous = previous_analysis if isinstance(previous_analysis, dict) else {}
    for key in ("canonical_identity", "preflight"):
        if previous.get(key) and not fresh.get(key):
            fresh[key] = previous[key]
    return fresh


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


def _assert_beta_authorization_metadata(job: dict) -> str:
    """Defense-in-depth: protected Beta jobs must carry an allow decision."""

    configuration = job.get("configuration") if isinstance(job, dict) else {}
    authorization = (
        configuration.get("beta_license_authorization")
        if isinstance(configuration, dict)
        else None
    )
    if not isinstance(authorization, dict) or authorization.get("required") is not True:
        return ""
    if authorization.get("allowed") is not True:
        return "beta_license_not_authorized"
    if str(authorization.get("state") or "") != LicenseState.ACTIVE.value:
        return "beta_license_not_active"
    for field in ("user_id", "device_fingerprint_hash", "checked_at"):
        if not str(authorization.get(field) or "").strip():
            return f"beta_license_missing_{field}"
    return ""


def _pipeline_commit_mismatch(job: object, artifacts: object) -> dict[str, str]:
    """Return expected/actual commit hashes when a completed artifact is stale.

    A queued job records the commit visible to the UI at submission time, while
    the pipeline run manifest records the commit that physically produced the
    artifact.  If those differ, the PDF may be technically valid but it is not
    evidence for the requested code path.  Missing legacy values remain neutral
    so old fixtures without a run manifest are still readable.
    """

    expected = str((job or {}).get("commit_hash") or "").strip()
    manifest_path = str((artifacts or {}).get("manifest_path") or "").strip()
    if not expected or not manifest_path:
        return {}
    manifest = load_json(Path(manifest_path))
    actual = str(manifest.get("commit_hash") or "").strip()
    if not actual or actual == expected:
        return {}
    return {"expected": expected, "actual": actual}


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
        "translation_provider", "translation_request_id",
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
    except (OSError, ValueError, SystemError):
        # Windows can surface an already-closed process handle as
        # ``SystemError: kill returned a result with an exception set``
        # (WinError 6).  The poll-before-signal check above makes this an
        # idempotent cleanup outcome; do not replace the job's primary
        # cancellation/failure state with a teardown exception.
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
        _append_pipeline_trace(job or {"id": job_id}, "JOB_LOAD_STARTED")
        print(f"JOB_LOAD_RESULT found={job is not None}", flush=True)
        _append_pipeline_trace(job or {"id": job_id}, "JOB_LOAD_RESULT", found=job is not None)
        if job is None:
            return 2
        _append_pipeline_trace(job, "CHILD_BOOT", frozen=bool(getattr(sys, "frozen", False)))
        _append_pipeline_trace(
            job, "CHILD_POST_BOOT_BEGIN",
            status=str(job.get("status") or ""),
            worker_id_present=bool(job.get("worker_id")),
        )
        if worker_id and job.get("worker_id") not in (worker_id, None):
            _append_pipeline_trace(job, "CHILD_POST_BOOT_REJECTED", reason_code="worker_ownership_mismatch")
            print(f"job {job_id} not owned by {worker_id}", file=sys.stderr)
            return 2
        if job["status"] not in {JobStatus.CLAIMING, JobStatus.STARTING}:
            _append_pipeline_trace(
                job, "CHILD_POST_BOOT_REJECTED",
                reason_code="job_not_startable", status=str(job.get("status") or ""),
            )
            print(f"job {job_id} not startable from {job['status']}", file=sys.stderr)
            return 2
        _append_pipeline_trace(job, "JOB_STATE_BEFORE_PIPELINE", status=str(job.get("status") or ""))
        if store.cancel_requested(job_id):
            store.transition(job_id, JobStatus.CANCELLING, expected_worker=job.get("worker_id"))
            store.transition(job_id, JobStatus.CANCELLED, expected_worker=job.get("worker_id"),
                             reason_code="user_cancelled")
            return 0

        output_dir = Path(job["output_dir"]).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        _append_pipeline_trace(job, "RUNTIME_CONTEXT_READY", output_dir=str(output_dir))
        command = list(job.get("command") or [])
        if not command:
            _append_pipeline_trace(job, "CHILD_POST_BOOT_REJECTED", reason_code="invalid_job_command")
            store.transition(job_id, JobStatus.FAILED, error_type="config",
                             error_message="invalid_job_command",
                             reason_code="invalid_job_command")
            return 2
        beta_error = _assert_beta_authorization_metadata(job)
        if beta_error:
            _append_pipeline_trace(job, "CHILD_POST_BOOT_REJECTED", reason_code=str(beta_error))
            store.transition(job_id, JobStatus.FAILED, error_type="authorization",
                             error_message=beta_error, reason_code=beta_error)
            return 2

        # The FINAL argv, after every rebuild, is the only command that can execute. A job
        # that explicitly requested a provider never reaches the provider with a different
        # one — or with none, which silently selects the runtime default.
        from ui_helpers import assert_command_provider

        try:
            _append_pipeline_trace(job, "PIPELINE_IMPORT_BEGIN")
            assert_command_provider(command, job.get("configuration"))
            _append_pipeline_trace(job, "PIPELINE_IMPORT_RESULT", status="success")
        except ValueError as exc:
            _append_pipeline_trace(job, "PIPELINE_IMPORT_RESULT", status="failed", reason_code=str(exc))
            reason_code = str(exc)
            store.transition(job_id, JobStatus.FAILED, error_type="config",
                             error_message=reason_code, reason_code=reason_code)
            return 2

        # Ensure the job is STARTING then RUNNING, and write the initial manifest. The
        # worker owns runner_pid/runner_create_time: it records the top of the runner tree
        # (the process it spawned), so the recovery termination catches the whole tree
        # including the venv launcher shim. The runner does not set them, to avoid racing
        # the worker with its own (child) PID.
        if job["status"] == JobStatus.CLAIMING:
            _append_pipeline_trace(job, "FIRST_DB_WRITE", transition="starting")
            job = store.transition(job_id, JobStatus.STARTING, expected_worker=job.get("worker_id"))
        _append_pipeline_trace(job, "FIRST_DB_WRITE", transition="running")
        job = store.transition(
            job_id, JobStatus.RUNNING, expected_worker=job.get("worker_id"),
            log_path=log_path, stage="created",
        )
        _write_manifest(output_dir, job)

        log_file = Path(log_path)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["TRADUTOR_JOB_ID"] = str(job.get("id") or "")
        env["TRADUTOR_JOB_RUN_ID"] = str(job.get("run_id") or "")
        # Stable logical translation request identity; credentials remain in the
        # encrypted job envelope and never cross this process boundary.
        configuration = job.get("configuration") if isinstance(job.get("configuration"), dict) else {}
        env["TRADUTOR_REQUEST_ID"] = str(
            configuration.get("translation_request_id") or f"translation:{job.get('id') or ''}"
        )[:220]
        # Persisted control-plane authority crosses the process boundary explicitly.
        # Missing/legacy metadata is fail-closed and cannot re-enable a provider.
        env["TRANSLATION_ENABLED"] = (
            "1" if configuration.get("translation_enabled") is True else "0"
        )
        # The wallet RPC expects the verified license_devices row UUID, never
        # the commercial install identifier.  It is read from the persisted
        # authorization snapshot at this process boundary.
        env["TRADUTOR_DEVICE_UUID"] = str(configuration.get("device_uuid") or "")

        with log_file.open("a", encoding="utf-8") as handle:
            # The command contains the submitted URL and can contain signed query values.
            # Keep an auditable event without persisting protected process arguments.
            handle.write(f"{time.strftime('%H:%M:%S')} pipeline iniciado (argumentos protegidos)\n")
            handle.flush()
            _append_pipeline_trace(job, "PIPELINE_SPAWN_BEGIN")
            try:
                proc = subprocess.Popen(command, **build_background_process_options(
                    cwd=str(Path.cwd()), stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, env=env,
                ))
            except OSError:
                _append_pipeline_trace(job, "PIPELINE_SPAWN_FAILED", reason_code="runner_start_failed")
                failed = store.transition(
                    job_id, JobStatus.FAILED, expected_worker=job.get("worker_id"),
                    exit_code=127, error_type="runner", error_message="runner_start_failed",
                    reason_code="runner_start_failed",
                )
                _write_manifest(output_dir, failed, status=JobStatus.FAILED,
                                exit_code=127, reason_code="runner_start_failed")
                return 2
            store.update_fields(job_id, runner_pid=os.getpid())
            _append_pipeline_trace(job, "PROCESS_CREATED", pid=proc.pid)
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
    commit_mismatch = (
        {}
        if cancelled or interrupted
        else _pipeline_commit_mismatch(job, artifacts)
    )
    technical_ok = (
        return_code == 0 and bool(artifacts.get("pdf_path"))
        and not cancelled and not interrupted and not commit_mismatch
    )
    if commit_mismatch:
        target = JobStatus.FAILED
    elif cancelled:
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
    if commit_mismatch:
        reason_code = "pipeline_commit_mismatch"
    elif cancelled:
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
    if isinstance(fresh_analysis, dict):
        fresh_analysis = _merge_terminal_source_analysis(
            fresh_analysis, job.get("source_analysis"))
    fresh_selection = download_report.get("source_selection") if isinstance(download_report, dict) else None
    if isinstance(fresh_analysis, dict):
        fields["source_analysis_json"] = json.dumps(fresh_analysis, ensure_ascii=False)
    if isinstance(fresh_selection, dict):
        fields["source_selection_json"] = json.dumps(fresh_selection, ensure_ascii=False)
    fields.update(_fresh_source_provenance(download_report, fresh_analysis))
    if target == JobStatus.FAILED:
        fields["error_type"] = (
            "runtime"
            if commit_mismatch
            else "source"
            if source_reason != "pipeline_failed"
            else "pipeline"
        )
        fields["error_message"] = reason_code
    if target == JobStatus.INTERRUPTED:
        fields["interrupted_reason"] = "worker_stop"
        fields["recoverable"] = 1
    try:
        job = store.transition(job_id, target, expected_worker=job.get("worker_id"), **fields)
    except TransitionError as exc:
        print(f"finalize transition failed: {exc}", file=sys.stderr)
        return 1
    manifest_updates = {}
    if commit_mismatch:
        manifest_updates["runtime_commit_mismatch"] = {
            "expected_commit_hash": commit_mismatch["expected"],
            "actual_commit_hash": commit_mismatch["actual"],
        }
    _write_manifest(output_dir, job, status=target,
                    pdf_path=artifacts.get("pdf_path") or "", exit_code=effective_return_code,
                    reason_code=reason_code, **manifest_updates)
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
    try:
        return run_job(args.job_id, args.db, args.worker_id, args.log)
    except BaseException as exc:  # noqa: BLE001 - preserve exit code and diagnostics
        _append_pipeline_trace(
            {"id": args.job_id, "configuration": {}}, "PIPELINE_MAIN_EXCEPTION",
            exception_class=type(exc).__name__, reason_code="runner_unhandled_exception",
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
