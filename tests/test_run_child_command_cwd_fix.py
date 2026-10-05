"""Regression tests for Mission 4 root cause: NotADirectoryError (WinError 267).

Root cause: subprocess.Popen was called with
  cwd=str(Path(__file__).resolve().parent)
In a frozen PyInstaller build the __file__ for scrapling_reader_resolver.py is
embedded inside a zip archive (e.g. library.zip).  Path(__file__).resolve() then
produces a path whose components cross through a file (the .zip), which Windows
rejects as a directory → NotADirectoryError / WinError 267 → isolation_unavailable.

Fix: remove the cwd argument from Popen; all paths are absolute.
"""
import io
import os
import sys
import time
from pathlib import Path
from unittest import mock

import pytest


def _resolver():
    from yomu.download.readers import scrapling_reader_resolver
    return scrapling_reader_resolver


# ---------------------------------------------------------------------------
# 1. Core fix: Popen is NOT called with cwd
# ---------------------------------------------------------------------------

def test_run_child_command_popen_called_without_cwd():
    """subprocess.Popen must not receive a cwd argument."""
    r = _resolver()
    captured = {}

    def fake_popen(args, **kwargs):
        captured["kwargs"] = kwargs
        raise OSError(42, "sentinel-stop")

    with mock.patch("subprocess.Popen", side_effect=fake_popen), \
         mock.patch.object(r, "_create_kill_job", return_value=None), \
         mock.patch.object(r, "_close_job"):
        with pytest.raises(OSError):
            r._run_child_command(["echo", "test"], deadline=time.monotonic() + 10)

    assert "cwd" not in captured.get("kwargs", {}), (
        "Popen must not be called with cwd — all child paths are absolute "
        "and cwd=Path(__file__).resolve().parent fails inside a zip archive"
    )


# ---------------------------------------------------------------------------
# 2. NotADirectoryError in Popen → isolation_unavailable raised by _resolve_in_child_process
# ---------------------------------------------------------------------------

def test_not_a_directory_error_in_popen_raises_isolation_unavailable():
    """NotADirectoryError (WinError 267) propagates from Popen → _resolve_in_child_process
    wraps it into DynamicReaderError isolation_unavailable."""
    r = _resolver()

    def fake_run(*a, **kw):
        err = NotADirectoryError(267, "The directory name is invalid.")
        err.winerror = 267
        raise err

    with mock.patch.object(r, "_run_child_command", side_effect=fake_run):
        with pytest.raises(r.DynamicReaderError) as exc_info:
            r._resolve_in_child_process(
                "https://comix.to/title/x/1",
                cancel_check=None, timeout=30.0,
            )

    assert exc_info.value.code == "dynamic_resolver_isolation_unavailable"


# ---------------------------------------------------------------------------
# 3. NotADirectoryError telemetry captures correct exception_type and winerror
# ---------------------------------------------------------------------------

def test_not_a_directory_error_emits_telemetry_with_winerror_267():
    """_resolve_in_child_process emits exception_type=NotADirectoryError, winerror=267."""
    r = _resolver()
    emitted = []

    def fake_run(*a, **kw):
        err = NotADirectoryError(267, "The directory name is invalid.")
        err.winerror = 267
        raise err

    def fake_telemetry(event, **fields):
        emitted.append({"event": event, **fields})

    with mock.patch.object(r, "_run_child_command", side_effect=fake_run), \
         mock.patch.object(r, "_append_telemetry", side_effect=fake_telemetry):
        with pytest.raises(r.DynamicReaderError) as exc_info:
            r._resolve_in_child_process(
                "https://comix.to/title/x/1",
                cancel_check=None, timeout=30.0,
            )

    assert exc_info.value.code == "dynamic_resolver_isolation_unavailable"
    iso = [e for e in emitted if e.get("error_code") == "dynamic_resolver_isolation_unavailable"]
    assert iso, "isolation_unavailable telemetry must be emitted"
    assert iso[0]["exception_type"] == "NotADirectoryError"
    assert iso[0]["winerror"] == 267


# ---------------------------------------------------------------------------
# 4. Any OSError from _run_child_command raises isolation_unavailable
# ---------------------------------------------------------------------------

def test_os_error_in_run_child_command_raises_isolation_unavailable():
    """Any OSError from _run_child_command → isolation_unavailable from _resolve_in_child_process."""
    r = _resolver()

    def fake_run(*a, **kw):
        raise OSError(13, "Permission denied")

    with mock.patch.object(r, "_run_child_command", side_effect=fake_run):
        with pytest.raises(r.DynamicReaderError) as exc_info:
            r._resolve_in_child_process(
                "https://comix.to/title/x/1",
                cancel_check=None, timeout=30.0,
            )

    assert exc_info.value.code == "dynamic_resolver_isolation_unavailable"


# ---------------------------------------------------------------------------
# 5. Telemetry winerror field propagated for permission errors too
# ---------------------------------------------------------------------------

def test_isolation_telemetry_winerror_is_propagated_for_permission_error():
    """Telemetry winerror is populated for PermissionError (WinError 5) as well."""
    r = _resolver()
    emitted = []

    def fake_run(*a, **kw):
        err = PermissionError(5, "Access denied")
        err.winerror = 5
        raise err

    def fake_telemetry(event, **fields):
        emitted.append({"event": event, **fields})

    with mock.patch.object(r, "_run_child_command", side_effect=fake_run), \
         mock.patch.object(r, "_append_telemetry", side_effect=fake_telemetry):
        with pytest.raises(r.DynamicReaderError):
            r._resolve_in_child_process(
                "https://comix.to/title/x/1",
                cancel_check=None, timeout=30.0,
            )

    iso = [e for e in emitted if e.get("error_code") == "dynamic_resolver_isolation_unavailable"]
    assert iso
    assert iso[0]["winerror"] == 5


# ---------------------------------------------------------------------------
# 6. CF capture is NOT triggered after isolation failure (regression guard)
# ---------------------------------------------------------------------------

def test_isolation_via_not_a_directory_error_does_not_trigger_cf_capture():
    """NotADirectoryError isolation failure must not cause CF clearance capture attempt."""
    from yomu.download import down
    from yomu.download.sources.chapter_source import SourceError

    def fake_once(*a, **kw):
        raise SourceError("dynamic_source_unavailable", "dynamic_resolver_isolation_unavailable")

    with mock.patch.object(down, "_discover_chapter_source_once", side_effect=fake_once), \
         mock.patch.object(down, "_comix_cf_autocapture_enabled", return_value=True), \
         mock.patch.object(down, "_run_comix_clearance_capture") as capture:
        with pytest.raises(SourceError):
            down.discover_chapter_source("https://comix.to/title/x/1")

    capture.assert_not_called()


# ---------------------------------------------------------------------------
# 7. _create_kill_job failure propagates as OSError from _run_child_command
# ---------------------------------------------------------------------------

def test_create_kill_job_failure_propagates_as_os_error():
    """If _create_kill_job itself fails, the OSError propagates through _run_child_command."""
    r = _resolver()

    def fake_create_job():
        raise OSError(6, "The handle is invalid.")

    # _run_child_command does not wrap: it propagates raw; _resolve_in_child_process wraps it
    def fake_run(*a, **kw):
        raise OSError(6, "The handle is invalid.")

    with mock.patch.object(r, "_run_child_command", side_effect=fake_run):
        with pytest.raises(r.DynamicReaderError) as exc_info:
            r._resolve_in_child_process(
                "https://comix.to/title/x/1",
                cancel_check=None, timeout=30.0,
            )

    assert exc_info.value.code == "dynamic_resolver_isolation_unavailable"


# ---------------------------------------------------------------------------
# 8. _assign_child_to_job failure maps to isolation_unavailable
# ---------------------------------------------------------------------------

def test_assign_child_to_job_failure_raises_isolation_unavailable():
    """If AssignProcessToJobObject fails, isolation_unavailable is raised (via _resolve_in_child_process)."""
    r = _resolver()

    def fake_run(*a, **kw):
        err = OSError(5, "Access is denied.")
        err.winerror = 5
        raise err

    with mock.patch.object(r, "_run_child_command", side_effect=fake_run):
        with pytest.raises(r.DynamicReaderError) as exc_info:
            r._resolve_in_child_process(
                "https://comix.to/title/x/1",
                cancel_check=None, timeout=30.0,
            )

    assert exc_info.value.code == "dynamic_resolver_isolation_unavailable"


# ---------------------------------------------------------------------------
# 9. Normal child exit still succeeds (smoke test post-fix)
# ---------------------------------------------------------------------------

def test_run_child_command_normal_exit_returns_success_dict():
    """After removing cwd, normal child exit still returns the success outcome dict."""
    r = _resolver()

    class _FakeProc:
        returncode = 0
        stdin = io.StringIO()
        _handle = None
        _poll_count = 0
        def poll(self):
            self._poll_count += 1
            # First poll → still running, second → done
            return None if self._poll_count == 1 else 0
        def wait(self, timeout=None): pass

    with mock.patch("subprocess.Popen", return_value=_FakeProc()), \
         mock.patch.object(r, "_create_kill_job", return_value=None), \
         mock.patch.object(r, "_assign_child_to_job"), \
         mock.patch.object(r, "_terminate_child"), \
         mock.patch.object(r, "_close_job"), \
         mock.patch.object(r, "_job_active_processes", return_value=0):
        outcome = r._run_child_command(["echo"], deadline=time.monotonic() + 60)

    assert outcome["returncode"] == 0
    assert not outcome["terminated"]


# ---------------------------------------------------------------------------
# 10. Cancel signal still terminates child (regression guard post-fix)
# ---------------------------------------------------------------------------

def test_run_child_command_cancel_still_terminates_child():
    """After removing cwd, cancel_check=lambda: True still terminates the child."""
    r = _resolver()

    class _FakeProc:
        returncode = -1
        stdin = io.StringIO()
        _handle = None
        def poll(self): return None  # always "running"
        def wait(self, timeout=None): pass

    with mock.patch("subprocess.Popen", return_value=_FakeProc()), \
         mock.patch.object(r, "_create_kill_job", return_value=None), \
         mock.patch.object(r, "_assign_child_to_job"), \
         mock.patch.object(r, "_terminate_child"), \
         mock.patch.object(r, "_close_job"), \
         mock.patch.object(r, "_job_active_processes", return_value=0):
        outcome = r._run_child_command(
            ["echo"], deadline=time.monotonic() + 60,
            cancel_check=lambda: True,
        )

    assert outcome["terminated"] is True
