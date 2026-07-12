"""Run one child process and persist its real exit code reliably."""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


LAUNCH_FAILURE_EXIT_CODE = 251


def _timestamp():
    return datetime.now().astimezone().isoformat()


def atomic_write_text(path, value):
    """Replace a UTF-8 text file only after its complete content is durable."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(str(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def read_exit_code(path):
    """Return an integer exit code, or None for missing/legacy empty files."""
    try:
        value = Path(path).read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        return None
    if not re.fullmatch(r"[+-]?\d+", value):
        return None
    return int(value)


def _append_event(events_path, event, **details):
    payload = {
        "timestamp": _timestamp(),
        "event": event,
        **details,
    }
    target = Path(events_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()


def _prepare_runtime(runtime):
    runtime.mkdir(parents=True, exist_ok=True)
    for name in (
        "child_pid.txt",
        "end_time.txt",
        "exit_code.txt",
        "launcher_error.txt",
    ):
        runtime.joinpath(name).unlink(missing_ok=True)
    atomic_write_text(runtime / "launcher_events.jsonl", "")
    atomic_write_text(runtime / "launcher_pid.txt", f"{os.getpid()}\n")
    atomic_write_text(runtime / "start_time.txt", _timestamp() + "\n")


def _persist_completion(runtime, events_path, returncode):
    atomic_write_text(runtime / "end_time.txt", _timestamp() + "\n")
    exit_path = runtime / "exit_code.txt"
    try:
        atomic_write_text(exit_path, f"{int(returncode)}\n")
    except Exception as exc:
        _append_event(
            events_path,
            "exit_code_persist_failed",
            success=False,
            error_type=type(exc).__name__,
        )
        print(
            f"Unable to persist child exit code {returncode}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        raise
    _append_event(
        events_path,
        "exit_code_persisted",
        success=True,
        exit_code=int(returncode),
        path=str(exit_path.resolve()),
    )


def run_process(
    command,
    runtime_directory,
    *,
    cwd=None,
    stdout_path=None,
    stderr_path=None,
    environment=None,
):
    """Run a child, wait for it, then atomically persist its real return code."""
    runtime = Path(runtime_directory).resolve()
    _prepare_runtime(runtime)
    events_path = runtime / "launcher_events.jsonl"
    stdout = Path(stdout_path).resolve() if stdout_path else runtime / "stdout.log"
    stderr = Path(stderr_path).resolve() if stderr_path else runtime / "stderr.log"
    stdout.parent.mkdir(parents=True, exist_ok=True)
    stderr.parent.mkdir(parents=True, exist_ok=True)
    normalized_command = [os.fspath(part) for part in command]
    working_directory = str(Path(cwd).resolve()) if cwd else None
    _append_event(events_path, "launcher_started", launcher_pid=os.getpid())

    process = None
    try:
        with stdout.open("wb") as stdout_handle, stderr.open("wb") as stderr_handle:
            if not normalized_command:
                raise ValueError("A child command is required")
            process = subprocess.Popen(
                normalized_command,
                cwd=working_directory,
                stdout=stdout_handle,
                stderr=stderr_handle,
                env=environment,
            )
            atomic_write_text(runtime / "child_pid.txt", f"{process.pid}\n")
            _append_event(
                events_path,
                "child_started",
                child_pid=process.pid,
            )
            try:
                returncode = process.wait()
            except KeyboardInterrupt:
                process.terminate()
                returncode = process.wait()
                _append_event(
                    events_path,
                    "child_interrupted",
                    child_pid=process.pid,
                    exit_code=int(returncode),
                )
    except (OSError, ValueError) as exc:
        if process is not None:
            raise
        error_text = f"{type(exc).__name__}: {exc}"
        atomic_write_text(runtime / "launcher_error.txt", error_text + "\n")
        _append_event(
            events_path,
            "launch_failed",
            error_type=type(exc).__name__,
            error_number=getattr(exc, "errno", None),
        )
        _persist_completion(runtime, events_path, LAUNCH_FAILURE_EXIT_CODE)
        return LAUNCH_FAILURE_EXIT_CODE

    returncode = int(returncode)
    _append_event(
        events_path,
        "child_exited",
        child_pid=process.pid,
        exit_code=returncode,
    )
    _persist_completion(runtime, events_path, returncode)
    return returncode


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run a child process and persist its real exit code.",
    )
    parser.add_argument("--runtime-directory", required=True)
    parser.add_argument("--cwd")
    parser.add_argument("--stdout-path")
    parser.add_argument("--stderr-path")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    command = list(arguments.command)
    if command[:1] == ["--"]:
        command = command[1:]
    return run_process(
        command,
        arguments.runtime_directory,
        cwd=arguments.cwd,
        stdout_path=arguments.stdout_path,
        stderr_path=arguments.stderr_path,
    )


if __name__ == "__main__":
    raise SystemExit(main())
