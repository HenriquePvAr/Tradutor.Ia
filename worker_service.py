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
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import process_tree
from job_store import JobStatus, JobStore
from local_environment import load_local_environment_for_entrypoint

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = REPO_ROOT / ".cache" / "runtime" / "jobs.sqlite3"
LOG_DIR = REPO_ROOT / ".cache" / "runtime" / "logs"
POLL_SECONDS = 1.5
WORKER_HEARTBEAT_SECONDS = 3.0
STALE_SECONDS = 30.0


class Worker:
    def __init__(self, db_path: Path, *, poll_seconds: float = POLL_SECONDS,
                 stale_seconds: float = STALE_SECONDS):
        self.worker_id = uuid.uuid4().hex
        self.pid = os.getpid()
        self.db_path = Path(db_path)
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

    @staticmethod
    def _runner_fingerprint(job_id: str) -> list[str]:
        # A runner's command always carries "job_runner.py" and its job id, so a live
        # process can be matched to this exact job and never to a reused PID.
        return ["job_runner.py", job_id]

    def another_worker_alive(self) -> bool:
        healthy = self.store.healthy_worker(stale_seconds=self.stale_seconds / 2)
        return bool(healthy and healthy["worker_id"] != self.worker_id)

    # Runner script per job type. Only translation is the default; more handlers register
    # here without spreading job-type ifs through the worker loop.
    _RUNNERS = {
        "translation": "job_runner.py",
        "community_publish": "community_publish_runner.py",
    }

    def _spawn_runner(self, job: dict) -> subprocess.Popen:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / f"{job['id']}.log"
        job_type = (job.get("configuration") or {}).get("job_type", "translation")
        runner = self._RUNNERS.get(job_type, "job_runner.py")
        command = [
            sys.executable, "-u", str(REPO_ROOT / runner),
            "--job-id", job["id"],
            "--db", str(self.db_path),
            "--worker-id", self.worker_id,
            "--log", str(log_path),
        ]
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        # The runner writes everything worth keeping to its own per-job log file, so its
        # stdout/stderr are silenced. Inheriting them would hand the runner (and the
        # pipeline it spawns) a handle to whatever console launched the worker, and a
        # tool capturing that console would then block on EOF until every descendant
        # exits - the cause of an earlier multi-hour hang.
        return subprocess.Popen(
            command, cwd=str(REPO_ROOT), creationflags=creationflags,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def _should_stop(self) -> bool:
        if self._stop_requested:
            return True
        try:
            return self.store.worker_stop_requested(self.worker_id)
        except Exception:  # noqa: BLE001 - a transient read must not crash the loop
            return False

    def _run_one(self, job: dict) -> None:
        proc = self._spawn_runner(job)
        # Persist the top of the runner tree (the process we spawned) so a later stop or
        # reconcile terminates the whole tree - including the launcher shim the venv
        # inserts as the parent of the real interpreter - not just a subtree of it.
        snap = process_tree.snapshot(proc.pid)
        self.store.update_fields(
            job["id"], runner_pid=proc.pid,
            runner_create_time=(snap or {}).get("create_time"),
        )
        self._active = {"proc": proc, "job_id": job["id"]}
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
            self._active = None

    def _stop_active_runner(self, job_id: str) -> None:
        """On worker stop, take the active runner's whole tree down and interrupt the job.

        The runner runs in its own process group, so a signal to the worker never reaches
        it; the worker must stop it explicitly. The tree is validated by the runner's
        command fingerprint before anything is terminated, so no unrelated process dies.
        """
        active = self._active
        proc = active["proc"] if active else None
        job = self.store.get_job(job_id)
        runner_pid = (job or {}).get("runner_pid") or (proc.pid if proc else None)
        report = process_tree.terminate_tree(
            runner_pid,
            create_time=(job or {}).get("runner_create_time"),
            substrings=self._runner_fingerprint(job_id),
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
        fresh = self.store.get_job(job_id)
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

    def _reconcile_stale(self) -> None:
        """Recover jobs whose owning worker died, taking down any orphaned runner tree.

        A crashed worker can leave its runner alive - even heartbeating a stuck pipeline -
        so the signal is the owning worker's lease being gone, not the job heartbeat. A
        live, validated runner is stopped first so it cannot keep processing or later
        finalize the job; a reused or foreign PID is never touched and the job is flagged
        ownership_mismatch. This guarantees no orphan and never a second live attempt.
        """
        orphans = self.store.orphaned_in_flight_jobs(
            exclude_worker=self.worker_id, worker_stale_seconds=self.stale_seconds / 2
        )
        for job in orphans:
            job_id = job["id"]
            runner_pid = job.get("runner_pid")
            fingerprint = self._runner_fingerprint(job_id)
            alive = process_tree.is_alive(
                runner_pid, create_time=job.get("runner_create_time"), substrings=fingerprint
            )
            reused = bool(runner_pid) and process_tree.snapshot(runner_pid) is not None and not alive
            reason = "orphaned_worker_gone"
            if alive:
                process_tree.terminate_tree(
                    runner_pid, create_time=job.get("runner_create_time"),
                    substrings=fingerprint, timeout=8.0,
                )
                reason = "reconciled_live_runner"
            elif reused:
                # PID belongs to some other process now: fail closed, do not terminate it.
                reason = "ownership_mismatch"
            try:
                self.store.transition(job_id, JobStatus.INTERRUPTED,
                                      interrupted_reason=reason, recoverable=1)
            except Exception:  # noqa: BLE001 - another worker may have won the reconcile
                pass

    def run(self, *, once: bool = False, max_idle_cycles: int | None = None) -> None:
        if self.another_worker_alive():
            print("another healthy worker is running; exiting cleanly")
            return
        self._install_signal_handlers()
        self.store.register_worker(self.worker_id, self.pid, create_time=process_tree.snapshot(self.pid)["create_time"] if process_tree.snapshot(self.pid) else None)
        idle = 0
        try:
            while not self._stop_requested:
                self.store.worker_heartbeat(self.worker_id)
                if self.store.worker_stop_requested(self.worker_id):
                    self._stop_requested = True
                    break
                self._reconcile_stale()
                job = self.store.claim_next_job(self.worker_id, self.pid)
                if job is None:
                    if once:
                        return
                    idle += 1
                    if max_idle_cycles is not None and idle >= max_idle_cycles:
                        return
                    time.sleep(self.poll_seconds)
                    continue
                idle = 0
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
