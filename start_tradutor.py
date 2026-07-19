"""Launcher for the local Tradutor.Ia system: independent worker + UI.

The UI does not own the worker. The worker is started as a detached process so it keeps
draining the queue when the UI (or the shell that launched it) is closed.

    python start_tradutor.py            # start the worker (if none) and the UI
    python start_tradutor.py worker     # start only the worker (detached)
    python start_tradutor.py ui         # start only the UI (foreground)
    python start_tradutor.py status     # print worker/queue health
    python start_tradutor.py stop       # ask the running worker to stop gracefully
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from local_environment import load_local_environment_for_entrypoint

REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = REPO_ROOT / ".cache" / "runtime" / "jobs.sqlite3"


def _detached_flags() -> int:
    if os.name != "nt":
        return 0
    # Independence from the launching console so the worker survives the UI closing.
    return (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )


def start_worker(*, force: bool = False) -> bool:
    """Start the worker detached unless a healthy one is already registered."""
    from job_store import JobStore

    store = JobStore(DB_PATH)
    try:
        healthy = store.healthy_worker(stale_seconds=15)
    finally:
        store.close()
    if healthy and not force:
        print(f"worker already online: {healthy['worker_id']} (pid {healthy['pid']})")
        return False
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    kwargs: dict = {"cwd": str(REPO_ROOT), "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = _detached_flags()
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(
        [sys.executable, "-u", str(REPO_ROOT / "worker_service.py"), "--db", str(DB_PATH)],
        **kwargs,
    )
    # Give it a moment to register its lease so status is accurate.
    time.sleep(1.5)
    print("worker started (detached)")
    return True


def start_ui() -> int:
    proc = subprocess.run([sys.executable, str(REPO_ROOT / "app_ui.py")], cwd=str(REPO_ROOT))
    return proc.returncode


def print_status() -> None:
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "worker_service.py"), "--status", "--db", str(DB_PATH)],
        cwd=str(REPO_ROOT),
    )


def stop_worker(*, force: bool = False, timeout: float = 30.0) -> int:
    """Gracefully stop a running worker (and its active runner tree), verified by PID.

    The stop is requested through the database so it reaches even a detached worker with
    no shared console. The worker's own loop then takes its active runner tree down and
    exits. ``--force`` is a fallback that terminates the worker's validated process tree
    directly; it never touches a process whose command line is not the worker's.
    """
    import process_tree
    from job_store import JobStore

    store = JobStore(DB_PATH)
    try:
        healthy = store.healthy_worker(stale_seconds=60)
        if not healthy:
            print("no healthy worker to stop")
            return 0
        worker_id = healthy["worker_id"]
        pid = int(healthy["pid"])
        create_time = healthy.get("create_time")
        active = store.active_job()
        if active:
            print(f"active job: {active['id']} ({active['status']})")
        if not process_tree.matches(pid, create_time=create_time, substrings=["worker_service.py"]):
            print(f"worker pid {pid} no longer matches a worker process; not signalling")
            return 0
        store.request_worker_stop(worker_id)
        print(f"stop requested for worker {worker_id} (pid {pid})")
    finally:
        store.close()

    # Wait for the worker to unregister its lease (it exits after stopping its runner).
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process_tree.snapshot(pid) is None:
            break
        time.sleep(0.5)

    if process_tree.snapshot(pid) is not None:
        if not force:
            print("worker still running; re-run with --force to terminate its tree")
            return 1
        report = process_tree.terminate_tree(
            pid, create_time=create_time, substrings=["worker_service.py"], timeout=10.0
        )
        print(f"force stop: {report['reason']} (terminated {len(report['terminated'])}, "
              f"killed {len(report['killed'])}, survivors {report['survivors']})")
        if report["survivors"]:
            return 1

    print("worker stopped; verified gone:", process_tree.snapshot(pid) is None)
    return 0


def main(argv: list[str] | None = None) -> int:
    if not load_local_environment_for_entrypoint():
        return 2
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else "all"
    if command == "worker":
        start_worker(force="--force" in args)
        return 0
    if command == "ui":
        return start_ui()
    if command == "status":
        print_status()
        return 0
    if command in {"stop", "stop-worker"}:
        return stop_worker(force="--force" in args)
    if command == "all":
        start_worker()
        return start_ui()
    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
