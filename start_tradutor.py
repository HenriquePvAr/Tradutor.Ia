"""Launcher for the local Tradutor.Ia system: independent worker + UI.

The UI does not own the worker. The worker is started as a detached process so it keeps
draining the queue when the UI (or the shell that launched it) is closed.

``all`` also supervises the worker it started: if that process dies unexpectedly it is
replaced under a bounded restart policy (see ``worker_supervisor``), and after the budget is
spent the launcher stays degraded instead of respawning forever. Supervision lives and dies
with this launcher process, and never adopts a worker this launcher did not start - so
``worker``, which exits immediately, spawns without supervising.

``all`` also checks for a signed update *before* anything starts, because Windows locks the
files of a running application: see ``update_bootstrap``. In a plain repository clone (the
current developer setup) there is no install layout, so the check reports "not configured" and
the launcher proceeds exactly as it always did.

    python start_tradutor.py            # check updates, then start the worker (if none) and UI
    python start_tradutor.py worker     # start only the worker (detached)
    python start_tradutor.py ui         # start only the UI (foreground)
    python start_tradutor.py status     # print worker/queue health
    python start_tradutor.py selftest   # prove this payload can start (used by the updater)
    python start_tradutor.py stop       # ask the running worker to stop gracefully
"""

from __future__ import annotations

import os
import json
import subprocess
import sys
import time
from pathlib import Path

import app_version
import update_bootstrap
import worker_supervisor
from local_environment import load_local_environment_for_entrypoint
from process_options import background_python_executable, build_background_process_options
from runtime_paths import runtime_root

REPO_ROOT = Path(__file__).resolve().parent
RUNTIME_ROOT = runtime_root()
DB_PATH = RUNTIME_ROOT / "jobs.sqlite3"

#: Supervision belongs to the launcher instance that started the worker, so it lives for
#: exactly as long as this process. A launcher that finds a healthy worker it did not start
#: never becomes its supervisor.
_SUPERVISOR: worker_supervisor.WorkerSupervisor | None = None


def _detached_flags() -> int:
    if os.name != "nt":
        return 0
    # Independence from the launching console so the worker survives the UI closing.
    return (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )


def build_child_command(role: str, *, frozen: bool | None = None) -> list[str]:
    """Build the worker/UI child command for Python and frozen runtimes."""
    if role not in {"worker", "ui"}:
        raise ValueError(f"unknown child role: {role}")
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        return [sys.executable, "--internal-child", role]
    script = REPO_ROOT / ("worker_service.py" if role == "worker" else "app_ui.py")
    return [background_python_executable(), "-u", str(script)]


def _run_internal_child(role: str, argv: list[str]) -> int:
    print(f"CHILD_BOOT role={role}", flush=True)
    if role == "worker":
        import worker_service
        return worker_service.main(argv)
    if role == "ui":
        import app_ui
        return app_ui.main()
    if role == "pipeline":
        print("CHILD_POST_BOOT_BEGIN role=pipeline", flush=True)
        try:
            print("PIPELINE_MODULE_IMPORT_BEGIN", flush=True)
            import run_webtoon
            print("PIPELINE_MODULE_IMPORT_RESULT status=success", flush=True)
            print("PIPELINE_MAIN_CALL_BEGIN", flush=True)
            result = run_webtoon.main(argv)
            print(
                f"PIPELINE_MAIN_CALL_RETURN result_type={type(result).__name__}",
                flush=True,
            )
            # run_webtoon returns its structured report on successful completion;
            # only numeric returns represent an explicit process exit code.  The
            # frozen child must not crash while converting the report dict to int.
            code = 0 if isinstance(result, dict) else int(result or 0)
            print(f"CHILD_EXIT exit_code={code}", flush=True)
            return code
        except BaseException as exc:  # noqa: BLE001 - preserve real child failure
            print(
                f"CHILD_STARTUP_EXCEPTION exception_class={type(exc).__name__} "
                "stage=pipeline_startup exit_code=1",
                flush=True,
            )
            print("CHILD_EXIT exit_code=1", flush=True)
            raise
    if role == "performance-validation":
        # Private CLI-only harness.  It is deliberately not reachable from the
        # normal desktop/UI command surface and owns no production job state.
        try:
            # PyInstaller's windowed bootloader may expose a console stream
            # whose flush raises EINVAL.  Keep diagnostics file-backed while
            # making routine pipeline logging non-fatal in that environment.
            class _FrozenSafeStream:
                def __init__(self, stream): self.stream = stream
                def write(self, value):
                    try: return self.stream.write(value) if self.stream is not None else len(value)
                    except OSError: return len(value)
                def flush(self):
                    try:
                        if self.stream is not None: self.stream.flush()
                    except OSError:
                        pass
            if getattr(sys, "frozen", False):
                sys.stdout = _FrozenSafeStream(getattr(sys, "stdout", None))
                sys.stderr = _FrozenSafeStream(getattr(sys, "stderr", None))
            from performance_validation_harness import main as performance_validation_main
            return int(performance_validation_main(argv) or 0)
        except BaseException as exc:  # noqa: BLE001 - preserve technical failure
            # Frozen GUI builds have no console, so preserve a sanitized failure
            # artifact at the requested output boundary instead of silently
            # terminating after snapshot creation.
            output_value = ""
            for index, value in enumerate(argv):
                if value == "--output-folder" and index + 1 < len(argv):
                    output_value = str(argv[index + 1])
                    break
            if output_value:
                try:
                    from pathlib import Path
                    import json
                    import traceback
                    target = Path(output_value).expanduser().resolve()
                    target.mkdir(parents=True, exist_ok=True)
                    frames = []
                    for frame in traceback.extract_tb(exc.__traceback__)[-12:]:
                        frames.append({"file": str(frame.filename), "function": str(frame.name),
                                       "line": int(frame.lineno), "operation": str(frame.line or "")[:240]})
                    (target / "frozen_failure.json").write_text(
                        json.dumps({
                            "status": "technical_failure",
                            "exception_type": type(exc).__name__,
                            "message": str(exc).splitlines()[0][:300],
                            "boundary": "performance_validation_child",
                            "traceback": frames,
                        }, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except Exception:
                    pass
            print(
                f"PERFORMANCE_VALIDATION_EXCEPTION type={type(exc).__name__} "
                f"message={str(exc).splitlines()[0][:200]}",
                flush=True,
            )
            return 1
    if role == "job-runner":
        import job_runner
        return int(job_runner.main(argv) or 0)
    if role == "community-publish-runner":
        import community_publish_runner
        return int(community_publish_runner.main(argv) or 0)
    if role == "review-rerun-runner":
        import review_rerun_runner
        return int(review_rerun_runner.main(argv) or 0)
    print(f"unknown internal child role: {role}", file=sys.stderr)
    return 2


def spawn_worker_process() -> subprocess.Popen:
    """Start one detached worker child and return its handle.

    DEVNULL on every stream (no unread PIPE can ever block the launcher) and the same
    detached/console-free flags as before: those keep the worker independent of the UI and
    of the launching console, and none of them prevent the parent from waiting on the
    handle, so supervision costs the child nothing.
    """

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    kwargs: dict = build_background_process_options(
        cwd=str(REPO_ROOT), env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if os.name == "nt":
        kwargs["creationflags"] = _detached_flags() | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(
        build_child_command("worker") + ["--db", str(DB_PATH)],
        **kwargs,
    )


def start_worker(*, force: bool = False) -> subprocess.Popen | None:
    """Start the worker detached unless a healthy one is already registered.

    Returns the process handle when *this* launcher created the worker, ``None`` when a
    healthy worker already existed. Only the former may be supervised.
    """
    from job_store import JobStore

    store = JobStore(DB_PATH)
    try:
        healthy = store.healthy_worker(stale_seconds=15)
    finally:
        store.close()
    if healthy and not force:
        print(f"worker already online: {healthy['worker_id']} (pid {healthy['pid']})")
        return None
    process = spawn_worker_process()
    # Give it a moment to register its lease so status is accurate.
    time.sleep(1.5)
    print("worker started (detached)")
    return process


def supervise_worker(process: subprocess.Popen) -> worker_supervisor.WorkerSupervisor:
    """Keep replacing this launcher's worker, within the bounded restart policy."""

    global _SUPERVISOR
    _SUPERVISOR = worker_supervisor.supervise(spawn_worker_process, process)
    return _SUPERVISOR


def start_ui() -> int:
    """Run the UI without inheriting a visible Windows console window."""

    log_path = RUNTIME_ROOT / "ui.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_handle:
        kwargs = build_background_process_options(
            cwd=str(REPO_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            new_session=True,
        )
        proc = subprocess.Popen(
            build_child_command("ui"),
            **kwargs,
        )
        return proc.wait()


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

    # Intent first: a worker that disappears after this point is an expected stop, never a
    # crash, whatever exit status it ends up with.
    if _SUPERVISOR is not None:
        _SUPERVISOR.request_stop()

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


def selftest() -> int:
    """Prove this payload is runnable: import the runtime and open the job store, nothing else.

    This is the evidence ``update_bootstrap`` requires from a freshly activated version before
    it is allowed to stay current. Importing the UI, the worker service and the job store pulls
    in essentially the whole runtime dependency graph, so a payload that is incomplete, built
    against a missing dependency or syntactically broken fails here — while nothing is started:
    no worker, no UI, no translation job, and no database is opened.
    """
    import app_ui  # noqa: F401  - the UI entrypoint must at least import
    import job_store  # noqa: F401
    import worker_service  # noqa: F401

    print(f"selftest ok: {app_version.PRODUCT_VERSION}")
    return 0


def internal_selftest(name: str) -> int:
    if name == "request-shape-writer":
        import tempfile
        from yomu_backend_provider import _persist_translation_execute_shape, _translation_execute_shape
        previous = os.environ.get("TRADUTOR_RUNTIME_ROOT")
        root = tempfile.mkdtemp(prefix="yomu-request-shape-selftest-")
        try:
            os.environ["TRADUTOR_RUNTIME_ROOT"] = root
            payload = {
                "request_id": "selftest-frozen-request", "job_id": "selftest-frozen-job",
                "source_lang": "JA", "target_lang": "PT-BR",
                "device_id": "550e8400-e29b-41d4-a716-446655440000",
                "reservation_id": "550e8400-e29b-41d4-a716-446655440001",
                "items": [{"item_id": f"item-{i}", "text": "x" * (254 if i == 0 else 1)} for i in range(9)],
            }
            shape = _translation_execute_shape(payload)
            _persist_translation_execute_shape(shape)
            path = Path(root) / "diagnostics" / "translation_execute_request_shape.jsonl"
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) < 1 or json.loads(lines[-1]).get("event") != "TRANSLATION_EXECUTE_REQUEST_SHAPE":
                return 1
            if any(secret in lines[-1].lower() for secret in ("authorization", "bearer", "jwt", "access_token", "refresh_token", "deepl_api_key", '"items"')):
                return 1
            print(json.dumps({"event": "FROZEN_REQUEST_SHAPE_WRITER_SELFTEST", "jsonl": str(path), "items_count": 9, "total_chars": 262}, separators=(",", ":")))
            return 0
        except Exception as exc:
            print(f"request-shape writer selftest failed: {type(exc).__name__}", file=sys.stderr)
            return 1
        finally:
            if previous is None:
                os.environ.pop("TRADUTOR_RUNTIME_ROOT", None)
            else:
                os.environ["TRADUTOR_RUNTIME_ROOT"] = previous
    if name != "rapidocr":
        print(f"unknown internal selftest: {name}", file=sys.stderr)
        return 2
    try:
        from ocr_engine import rapidocr_runtime_smoke
        print(json.dumps(rapidocr_runtime_smoke(), ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"rapidocr selftest failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def run_update_check() -> update_bootstrap.UpdateStatus:
    """Check for a signed update before any part of the application is running."""
    status = update_bootstrap.check_before_start(REPO_ROOT)
    if status.user_message:
        print(status.user_message)
    return status


def handoff(payload_dir: Path) -> int:
    """Start the newly activated release and wait for it, so it owns the worker supervisor.

    Nothing is overwritten: the updated payload is a different immutable directory, and this
    launcher keeps running only as the parent that waits for it.
    """
    env = os.environ.copy()
    env[update_bootstrap.HANDOFF_ENV] = "1"
    return subprocess.run(
        [sys.executable, str(Path(payload_dir) / "start_tradutor.py"), "all"],
        cwd=str(payload_dir), env=env,
    ).returncode


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--internal-child":
        if len(args) < 2:
            print("missing internal child role", file=sys.stderr)
            return 2
        return _run_internal_child(args[1], args[2:])
    if args and args[0] == "--internal-selftest":
        if len(args) < 2:
            print("missing internal selftest name", file=sys.stderr)
            return 2
        return internal_selftest(args[1])
    if not load_local_environment_for_entrypoint():
        return 2
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
    if command == "selftest":
        return selftest()
    if command == "all":
        status = run_update_check()
        if not status.can_launch:
            print(f"update_required: {status.state}", file=sys.stderr)
            return 3
        if status.payload_dir is not None:
            return handoff(status.payload_dir)
        started = start_worker()
        if started is not None:
            supervise_worker(started)
        return start_ui()
    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
