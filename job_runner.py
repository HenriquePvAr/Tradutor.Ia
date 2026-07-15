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
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from job_store import JobStatus, JobStore, TransitionError
from ui_helpers import (
    ProgressSnapshot,
    derive_final_run_status,
    find_output_artifacts,
    load_json,
    mask_secrets,
    parse_progress_line,
)

HEARTBEAT_SECONDS = 3.0
CANCEL_GRACE_SECONDS = 8.0


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _write_manifest(output_dir: Path, job: dict, **updates) -> None:
    manifest = {
        "job_manifest_version": 1,
        "job_id": job["id"],
        "run_id": job["run_id"],
        "status": job["status"],
        "stage": job.get("stage"),
        "source_url": job.get("source_url"),
        "output_dir": str(output_dir),
        "commit_hash": job.get("commit_hash"),
        "branch": job.get("branch"),
        "attempt": job.get("attempt"),
        "configuration": job.get("configuration", {}),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "updated_at": time.time(),
    }
    manifest.update(updates)
    _atomic_write_json(output_dir / "job_manifest.json", manifest)


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
            masked = mask_secrets(line)
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
    """Stop the pipeline child cooperatively, then forcibly, by its verified handle."""
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
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
        proc.wait(timeout=CANCEL_GRACE_SECONDS)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass


def run_job(job_id: str, db_path: str, worker_id: str, log_path: str) -> int:
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
            store.transition(job_id, JobStatus.CANCELLED, expected_worker=job.get("worker_id"))
            return 0

        output_dir = Path(job["output_dir"]).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        command = list(job.get("command") or [])
        if not command:
            store.transition(job_id, JobStatus.FAILED, error_type="config",
                             error_message="empty command")
            return 2

        # Ensure the job is STARTING then RUNNING, and write the initial manifest.
        if job["status"] == JobStatus.CLAIMING:
            job = store.transition(job_id, JobStatus.STARTING, expected_worker=job.get("worker_id"))
        job = store.transition(
            job_id, JobStatus.RUNNING, expected_worker=job.get("worker_id"),
            runner_pid=os.getpid(), log_path=log_path, stage="created",
        )
        _write_manifest(output_dir, job)

        log_file = Path(log_path)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%H:%M:%S')} $ {' '.join(command)}\n")
            handle.flush()
            proc = subprocess.Popen(
                command, cwd=str(Path.cwd()), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, creationflags=creationflags, env=env,
            )
            store.update_fields(job_id, runner_pid=os.getpid())
            pump = _OutputPump(proc.stdout, handle, store, job_id)
            pump.start()

            cancelled = False
            while proc.poll() is None:
                time.sleep(min(HEARTBEAT_SECONDS, 1.0))
                snap = pump.drain_progress()
                if snap is not None:
                    store.update_progress(
                        job_id, stage=snap.stage, current=int(snap.current or 0),
                        total=int(snap.total or 0), message=snap.last_message,
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
            pump.join(timeout=2)
            return_code = proc.wait()

        return _finalize(store, job_id, job, output_dir, return_code, cancelled, log_path)
    finally:
        store.close()


def _finalize(store, job_id, job, output_dir, return_code, cancelled, log_path) -> int:
    artifacts = find_output_artifacts(output_dir)
    report = load_json(output_dir / "timing_report.json")
    quality = report.get("quality_validation") or {}
    technical_ok = return_code == 0 and bool(artifacts.get("pdf_path")) and not cancelled
    if cancelled:
        target = JobStatus.CANCELLED
    else:
        status = derive_final_run_status(
            technical_success=technical_ok, cancelled=False, quality_validation=quality,
        )
        target = {
            "finished": JobStatus.FINISHED,
            "review_required": JobStatus.REVIEW_REQUIRED,
            "error": JobStatus.FAILED,
        }.get(status, JobStatus.FAILED)

    fields = {
        "exit_code": int(return_code),
        "pdf_path": artifacts.get("pdf_path") or "",
        "quality_report_path": artifacts.get("quality_report_path") or "",
        "manifest_path": artifacts.get("manifest_path") or "",
        "progress_path": str(output_dir / "progress.json"),
    }
    if target == JobStatus.FAILED:
        fields["error_type"] = "pipeline"
        fields["error_message"] = f"exit_code={return_code}, pdf={'yes' if artifacts.get('pdf_path') else 'no'}"
    try:
        job = store.transition(job_id, target, expected_worker=job.get("worker_id"), **fields)
    except TransitionError as exc:
        print(f"finalize transition failed: {exc}", file=sys.stderr)
        return 1
    _write_manifest(output_dir, job, status=target,
                    pdf_path=artifacts.get("pdf_path") or "", exit_code=int(return_code))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--worker-id", default="")
    parser.add_argument("--log", required=True)
    args = parser.parse_args(argv)
    return run_job(args.job_id, args.db, args.worker_id, args.log)


if __name__ == "__main__":
    raise SystemExit(main())
