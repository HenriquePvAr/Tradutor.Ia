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
import subprocess
import sys
import time
import uuid
from pathlib import Path

from job_store import JobStatus, JobStore

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

    def close(self) -> None:
        self.store.close()

    def another_worker_alive(self) -> bool:
        healthy = self.store.healthy_worker(stale_seconds=self.stale_seconds / 2)
        return bool(healthy and healthy["worker_id"] != self.worker_id)

    def _spawn_runner(self, job: dict) -> subprocess.Popen:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / f"{job['id']}.log"
        command = [
            sys.executable, "-u", str(REPO_ROOT / "job_runner.py"),
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

    def _run_one(self, job: dict) -> None:
        proc = self._spawn_runner(job)
        # Keep the worker's own lease alive while the runner owns the job's heartbeat.
        while proc.poll() is None:
            self.store.worker_heartbeat(self.worker_id)
            time.sleep(WORKER_HEARTBEAT_SECONDS)
        self.store.worker_heartbeat(self.worker_id)

    def run(self, *, once: bool = False, max_idle_cycles: int | None = None) -> None:
        if self.another_worker_alive():
            print("another healthy worker is running; exiting cleanly")
            return
        self.store.register_worker(self.worker_id, self.pid)
        idle = 0
        try:
            while True:
                self.store.worker_heartbeat(self.worker_id)
                self.store.recover_stale(stale_seconds=self.stale_seconds)
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
