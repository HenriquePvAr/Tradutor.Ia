"""Run one child process and persist its real exit code reliably."""

import argparse
import ctypes
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from process_options import build_background_process_options


LAUNCH_FAILURE_EXIT_CODE = 251
CLEANUP_FAILURE_EXIT_CODE = 252
CANCELLED_EXIT_CODE = 130
_TREE_CLEANUP_TIMEOUT_SECONDS = 10.0
_INTERRUPTIBLE_WAIT_SECONDS = 0.2


class ProcessTreeError(RuntimeError):
    """Raised when a launched process tree cannot be controlled safely."""


def _interruptible_wait(process):
    while True:
        try:
            return process.wait(timeout=_INTERRUPTIBLE_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            continue


if os.name == "nt":
    from ctypes import wintypes

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
    _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS = 1
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _THREAD_SUSPEND_RESUME = 0x0002
    _TH32CS_SNAPTHREAD = 0x00000004
    _CREATE_SUSPENDED = 0x00000004
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _ERROR_NO_MORE_FILES = 18

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    class _THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _KERNEL32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _KERNEL32.CreateJobObjectW.restype = wintypes.HANDLE
    _KERNEL32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _KERNEL32.SetInformationJobObject.restype = wintypes.BOOL
    _KERNEL32.AssignProcessToJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
    ]
    _KERNEL32.AssignProcessToJobObject.restype = wintypes.BOOL
    _KERNEL32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _KERNEL32.TerminateJobObject.restype = wintypes.BOOL
    _KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    _KERNEL32.CloseHandle.restype = wintypes.BOOL
    _KERNEL32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    _KERNEL32.OpenProcess.restype = wintypes.HANDLE
    _KERNEL32.IsProcessInJob.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.BOOL),
    ]
    _KERNEL32.IsProcessInJob.restype = wintypes.BOOL
    _KERNEL32.GetCurrentProcess.argtypes = []
    _KERNEL32.GetCurrentProcess.restype = wintypes.HANDLE
    _KERNEL32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _KERNEL32.QueryInformationJobObject.restype = wintypes.BOOL
    _KERNEL32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _KERNEL32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _KERNEL32.Thread32First.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_THREADENTRY32),
    ]
    _KERNEL32.Thread32First.restype = wintypes.BOOL
    _KERNEL32.Thread32Next.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_THREADENTRY32),
    ]
    _KERNEL32.Thread32Next.restype = wintypes.BOOL
    _KERNEL32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _KERNEL32.OpenThread.restype = wintypes.HANDLE
    _KERNEL32.ResumeThread.argtypes = [wintypes.HANDLE]
    _KERNEL32.ResumeThread.restype = wintypes.DWORD


def _windows_error(operation):
    error = ctypes.get_last_error()
    return OSError(error, f"{operation} failed: {ctypes.FormatError(error)}")


def _open_suspended_primary_thread(pid):
    snapshot = _KERNEL32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
        raise _windows_error("CreateToolhelp32Snapshot")
    thread_ids = []
    enumeration_error = None
    try:
        entry = _THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        if not _KERNEL32.Thread32First(snapshot, ctypes.byref(entry)):
            enumeration_error = _windows_error("Thread32First")
        else:
            while True:
                if int(entry.th32OwnerProcessID) == int(pid):
                    thread_ids.append(int(entry.th32ThreadID))
                entry.dwSize = ctypes.sizeof(entry)
                ctypes.set_last_error(0)
                if not _KERNEL32.Thread32Next(snapshot, ctypes.byref(entry)):
                    error = ctypes.get_last_error()
                    if error not in (0, _ERROR_NO_MORE_FILES):
                        enumeration_error = _windows_error("Thread32Next")
                    break
    finally:
        if not _KERNEL32.CloseHandle(snapshot) and enumeration_error is None:
            enumeration_error = _windows_error("CloseHandle(thread snapshot)")
    if enumeration_error is not None:
        raise enumeration_error
    if len(thread_ids) != 1:
        raise ProcessTreeError(
            f"Expected one suspended primary thread for PID {pid}, found {len(thread_ids)}"
        )
    thread_handle = _KERNEL32.OpenThread(
        _THREAD_SUSPEND_RESUME,
        False,
        thread_ids[0],
    )
    if not thread_handle:
        raise _windows_error("OpenThread")
    return thread_handle, thread_ids[0]


class _WindowsJob:
    """Own a kill-on-close Windows Job Object and its process membership."""

    def __init__(self, events_path):
        self.events_path = Path(events_path)
        self.handle = _KERNEL32.CreateJobObjectW(None, None)
        self.assigned = False
        if not self.handle:
            raise _windows_error("CreateJobObjectW")
        try:
            information = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            information.BasicLimitInformation.LimitFlags = (
                _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            if not _KERNEL32.SetInformationJobObject(
                self.handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(information),
                ctypes.sizeof(information),
            ):
                raise _windows_error("SetInformationJobObject")
            parent_in_job = wintypes.BOOL()
            if not _KERNEL32.IsProcessInJob(
                _KERNEL32.GetCurrentProcess(),
                None,
                ctypes.byref(parent_in_job),
            ):
                raise _windows_error("IsProcessInJob")
            _append_event(
                self.events_path,
                "job_created",
                mechanism="windows_job",
                kill_on_close=True,
                parent_in_job=bool(parent_in_job.value),
            )
        except BaseException:
            self._close_unchecked()
            raise

    def assign(self, pid):
        access = (
            _PROCESS_TERMINATE
            | _PROCESS_SET_QUOTA
            | _PROCESS_QUERY_LIMITED_INFORMATION
        )
        process_handle = _KERNEL32.OpenProcess(access, False, int(pid))
        if not process_handle:
            raise _windows_error("OpenProcess")
        assignment_error = None
        try:
            if not _KERNEL32.AssignProcessToJobObject(self.handle, process_handle):
                assignment_error = _windows_error("AssignProcessToJobObject")
            else:
                self.assigned = True
        finally:
            if not _KERNEL32.CloseHandle(process_handle) and assignment_error is None:
                assignment_error = _windows_error("CloseHandle(process)")
        if assignment_error is not None:
            raise assignment_error
        _append_event(
            self.events_path,
            "child_assigned",
            mechanism="windows_job",
            child_pid=int(pid),
        )

    def terminate(self, exit_code):
        if not self.handle:
            return
        if not _KERNEL32.TerminateJobObject(self.handle, int(exit_code)):
            raise _windows_error("TerminateJobObject")

    def active_process_count(self):
        information = _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        returned = wintypes.DWORD()
        if not _KERNEL32.QueryInformationJobObject(
            self.handle,
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS,
            ctypes.byref(information),
            ctypes.sizeof(information),
            ctypes.byref(returned),
        ):
            raise _windows_error("QueryInformationJobObject")
        return int(information.ActiveProcesses)

    def wait_empty(self, timeout=_TREE_CLEANUP_TIMEOUT_SECONDS):
        deadline = time.monotonic() + timeout
        while self.active_process_count() != 0:
            if time.monotonic() >= deadline:
                raise ProcessTreeError("Windows Job Object did not become empty")
            time.sleep(0.01)

    def close(self):
        if not self.handle:
            return
        handle = self.handle
        if not _KERNEL32.CloseHandle(handle):
            raise _windows_error("CloseHandle(job)")
        self.handle = None

    def _close_unchecked(self):
        if self.handle:
            _KERNEL32.CloseHandle(self.handle)
            self.handle = None

    def __del__(self):
        try:
            self._close_unchecked()
        except Exception:
            pass


class _WindowsManagedProcess:
    mechanism = "windows_job"

    def __init__(self, events_path):
        self.events_path = Path(events_path)
        self.job = _WindowsJob(events_path)
        self.process = None

    def start(
        self,
        command,
        *,
        cwd,
        stdout_handle,
        stderr_handle,
        environment,
        runtime,
    ):
        # No child instruction can run before Job assignment and ResumeThread.
        options = build_background_process_options(
            cwd=cwd, stdout=stdout_handle, stderr=stderr_handle, env=environment,
        )
        options["creationflags"] = int(options.get("creationflags", 0)) | _CREATE_SUSPENDED
        self.process = subprocess.Popen(command, **options)
        atomic_write_text(runtime / "child_pid.txt", f"{self.process.pid}\n")
        _append_event(
            self.events_path,
            "child_created_suspended",
            child_pid=self.process.pid,
            mechanism=self.mechanism,
        )
        thread_handle, thread_id = _open_suspended_primary_thread(self.process.pid)
        resume_error = None
        try:
            self.job.assign(self.process.pid)
            previous_suspend_count = _KERNEL32.ResumeThread(thread_handle)
            if previous_suspend_count == 0xFFFFFFFF:
                resume_error = _windows_error("ResumeThread")
            elif previous_suspend_count != 1:
                resume_error = ProcessTreeError(
                    "Suspended primary thread had unexpected suspend count "
                    f"{previous_suspend_count}"
                )
        finally:
            if not _KERNEL32.CloseHandle(thread_handle) and resume_error is None:
                resume_error = _windows_error("CloseHandle(primary thread)")
        if resume_error is not None:
            raise resume_error
        _append_event(
            self.events_path,
            "child_started",
            child_pid=self.process.pid,
            primary_thread_id=thread_id,
            mechanism=self.mechanism,
        )

    def wait(self):
        return _interruptible_wait(self.process)

    def terminate_tree(self, exit_code, reason):
        errors = []
        if self.job.assigned:
            try:
                self.job.terminate(exit_code)
            except Exception as exc:
                errors.append(exc)
            else:
                try:
                    self.job.wait_empty()
                except Exception as exc:
                    errors.append(exc)
        elif self.process is not None and self.process.poll() is None:
            # The primary thread is still suspended, so child code never ran.
            try:
                self.process.terminate()
            except OSError as exc:
                errors.append(exc)
        try:
            self.job.close()
        except Exception as exc:
            errors.append(exc)
        if self.process is not None:
            try:
                self.process.wait(timeout=_TREE_CLEANUP_TIMEOUT_SECONDS)
            except Exception as exc:
                errors.append(exc)
        _append_event(
            self.events_path,
            "process_tree_terminated",
            mechanism=self.mechanism,
            reason=reason,
            success=not errors,
        )
        if errors:
            raise ProcessTreeError(
                "; ".join(f"{type(error).__name__}: {error}" for error in errors)
            )


class _PosixManagedProcess:
    mechanism = "posix_process_group"

    def __init__(self, events_path):
        self.events_path = Path(events_path)
        self.process = None

    def start(
        self,
        command,
        *,
        cwd,
        stdout_handle,
        stderr_handle,
        environment,
        runtime,
    ):
        self.process = subprocess.Popen(command, **build_background_process_options(
            cwd=cwd, stdout=stdout_handle, stderr=stderr_handle, env=environment,
            new_session=True,
        ))
        atomic_write_text(runtime / "child_pid.txt", f"{self.process.pid}\n")
        _append_event(
            self.events_path,
            "process_group_created",
            mechanism=self.mechanism,
            child_pid=self.process.pid,
        )
        _append_event(
            self.events_path,
            "child_started",
            child_pid=self.process.pid,
        )

    def wait(self):
        return _interruptible_wait(self.process)

    def terminate_tree(self, exit_code, reason):
        del exit_code
        errors = []
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            errors.append(exc)
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=_TREE_CLEANUP_TIMEOUT_SECONDS)
            except Exception as exc:
                errors.append(exc)
        deadline = time.monotonic() + _TREE_CLEANUP_TIMEOUT_SECONDS
        while True:
            try:
                os.killpg(self.process.pid, 0)
            except ProcessLookupError:
                break
            except OSError as exc:
                errors.append(exc)
                break
            if time.monotonic() >= deadline:
                errors.append(ProcessTreeError("POSIX process group did not become empty"))
                break
            time.sleep(0.01)
        _append_event(
            self.events_path,
            "process_tree_terminated",
            mechanism=self.mechanism,
            reason=reason,
            success=not errors,
        )
        if errors:
            raise ProcessTreeError(
                "; ".join(f"{type(error).__name__}: {error}" for error in errors)
            )


def _timestamp():
    return datetime.now().astimezone().isoformat()


def _raise_keyboard_interrupt(signum, frame):
    del signum, frame
    raise KeyboardInterrupt


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


def _record_launcher_error(runtime, events_path, event, exc, **details):
    error_text = f"{type(exc).__name__}: {exc}"
    atomic_write_text(runtime / "launcher_error.txt", error_text + "\n")
    _append_event(
        events_path,
        event,
        error_type=type(exc).__name__,
        error_number=getattr(exc, "errno", None),
        **details,
    )


def _read_child_pid(runtime):
    try:
        return int((runtime / "child_pid.txt").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _new_managed_process(events_path):
    if os.name == "nt":
        return _WindowsManagedProcess(events_path)
    if os.name == "posix":
        return _PosixManagedProcess(events_path)
    raise OSError(f"Unsupported process-tree platform: {os.name}")


def _terminate_managed_process(manager, events_path, exit_code, reason):
    if manager is None:
        return None
    try:
        manager.terminate_tree(exit_code, reason)
    except Exception as exc:
        _append_event(
            events_path,
            "cleanup_error",
            mechanism=getattr(manager, "mechanism", "unknown"),
            reason=reason,
            error_type=type(exc).__name__,
        )
        return exc
    return None


def run_process(
    command,
    runtime_directory,
    *,
    cwd=None,
    stdout_path=None,
    stderr_path=None,
    environment=None,
):
    """Run a controlled process tree and persist its real return code."""
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

    manager = None
    returncode = None
    child_completed = False
    try:
        with stdout.open("wb") as stdout_handle, stderr.open("wb") as stderr_handle:
            if not normalized_command:
                raise ValueError("A child command is required")
            manager = _new_managed_process(events_path)
            manager.start(
                normalized_command,
                cwd=working_directory,
                stdout_handle=stdout_handle,
                stderr_handle=stderr_handle,
                environment=environment,
                runtime=runtime,
            )
            returncode = int(manager.wait())
            child_completed = True
            cleanup_error = _terminate_managed_process(
                manager,
                events_path,
                0,
                "normal_completion",
            )
            if cleanup_error is not None:
                _record_launcher_error(
                    runtime,
                    events_path,
                    "launcher_error",
                    cleanup_error,
                    stage="normal_cleanup",
                )
                returncode = CLEANUP_FAILURE_EXIT_CODE
    except KeyboardInterrupt:
        _append_event(
            events_path,
            "cancellation_requested",
            mechanism=getattr(manager, "mechanism", "not_started"),
        )
        cleanup_error = _terminate_managed_process(
            manager,
            events_path,
            CANCELLED_EXIT_CODE,
            "keyboard_interrupt",
        )
        if cleanup_error is None:
            returncode = CANCELLED_EXIT_CODE
        else:
            _record_launcher_error(
                runtime,
                events_path,
                "launcher_error",
                cleanup_error,
                stage="cancellation_cleanup",
            )
            returncode = CLEANUP_FAILURE_EXIT_CODE
    except (OSError, ValueError, ProcessTreeError) as exc:
        cleanup_error = _terminate_managed_process(
            manager,
            events_path,
            LAUNCH_FAILURE_EXIT_CODE,
            "launch_failure",
        )
        _record_launcher_error(runtime, events_path, "launch_failed", exc)
        if cleanup_error is None:
            returncode = LAUNCH_FAILURE_EXIT_CODE
        else:
            _record_launcher_error(
                runtime,
                events_path,
                "cleanup_error",
                cleanup_error,
                stage="launch_failure_cleanup",
            )
            returncode = CLEANUP_FAILURE_EXIT_CODE
    except Exception as exc:
        cleanup_error = _terminate_managed_process(
            manager,
            events_path,
            CLEANUP_FAILURE_EXIT_CODE,
            "monitoring_failure",
        )
        _record_launcher_error(
            runtime,
            events_path,
            "launcher_error",
            exc,
            stage="monitoring",
        )
        if cleanup_error is None:
            returncode = CLEANUP_FAILURE_EXIT_CODE
        else:
            _record_launcher_error(
                runtime,
                events_path,
                "cleanup_error",
                cleanup_error,
                stage="monitoring_cleanup",
            )
            returncode = CLEANUP_FAILURE_EXIT_CODE

    returncode = int(returncode)
    child_pid = _read_child_pid(runtime)
    if child_pid is not None:
        _append_event(
            events_path,
            "child_exited",
            child_pid=child_pid,
            exit_code=returncode,
            completed_normally=child_completed,
        )
    _persist_completion(runtime, events_path, returncode)
    return returncode


def main(argv=None):
    arguments_list = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        description="Run a child process and persist its real exit code.",
    )
    parser.add_argument("--runtime-directory", required=True)
    parser.add_argument("--cwd")
    parser.add_argument("--stdout-path")
    parser.add_argument("--stderr-path")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(arguments_list)
    command = list(arguments.command)
    if command[:1] == ["--"]:
        command = command[1:]
    previous_sigbreak_handler = None
    if os.name == "nt" and hasattr(signal, "SIGBREAK"):
        previous_sigbreak_handler = signal.getsignal(signal.SIGBREAK)
        signal.signal(signal.SIGBREAK, _raise_keyboard_interrupt)
    try:
        return run_process(
            command,
            arguments.runtime_directory,
            cwd=arguments.cwd,
            stdout_path=arguments.stdout_path,
            stderr_path=arguments.stderr_path,
        )
    finally:
        if previous_sigbreak_handler is not None:
            signal.signal(signal.SIGBREAK, previous_sigbreak_handler)


if __name__ == "__main__":
    raise SystemExit(main())
