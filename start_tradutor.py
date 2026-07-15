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

REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = REPO_ROOT / ".cache" / "runtime" / "jobs.sqlite3"


def _detached_flags() -> int:
    if os.name != "nt":
        return 0
    # Independence from the launching console so the worker survives the UI closing.
    return (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
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
    kwargs: dict = {"cwd": str(REPO_ROOT)}
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


def stop_worker() -> None:
    """Ask a running worker to stop by terminating its verified PID (not by name)."""
    import signal

    from job_store import JobStore

    store = JobStore(DB_PATH)
    try:
        healthy = store.healthy_worker(stale_seconds=60)
    finally:
        store.close()
    if not healthy:
        print("no healthy worker to stop")
        return
    pid = int(healthy["pid"])
    try:
        if os.name == "nt":
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        else:
            os.kill(pid, signal.SIGTERM)
        print(f"stop signal sent to worker pid {pid}")
    except (OSError, ValueError) as exc:
        print(f"could not signal worker pid {pid}: {exc}")


def main(argv: list[str] | None = None) -> int:
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
    if command == "stop":
        stop_worker()
        return 0
    if command == "all":
        start_worker()
        return start_ui()
    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
