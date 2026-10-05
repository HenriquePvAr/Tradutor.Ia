"""Regression tests for trace dbc11d9a root cause.

Root cause: discover_chapter_source triggered CF capture on
dynamic_resolver_isolation_unavailable (local subprocess failure) — a code path
where CF clearance is irrelevant because the child process never starts.

Two fixes:
  1. scrapling_reader_resolver.py: CREATE_BREAKAWAY_FROM_JOB in Popen creationflags
  2. down.py: isolation detail codes excluded from CF capture trigger
"""
import importlib
import json
import os
from pathlib import Path
from unittest import mock

import pytest

from yomu.download import down
from yomu.download.sources.chapter_source import SourceError


COMIX = "https://comix.to/title/example/1-chapter-1"

_ISOLATION_CODES = [
    "dynamic_resolver_isolation_unavailable",
    "dynamic_resolver_child_cleanup_failed",
]


# ---------------------------------------------------------------------------
# Fix 2: isolation detail codes do NOT trigger CF capture
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("detail", _ISOLATION_CODES)
def test_isolation_failure_does_not_trigger_cf_capture(detail):
    """Subprocess isolation failures are not CF-fixable; capture must be skipped."""
    with mock.patch.object(down, "_discover_chapter_source_once",
                           side_effect=SourceError("dynamic_source_unavailable", detail)), \
            mock.patch.object(down, "_comix_cf_autocapture_enabled", return_value=True), \
            mock.patch.object(down, "_run_comix_clearance_capture") as capture:
        with pytest.raises(SourceError) as exc_info:
            down.discover_chapter_source(COMIX)
    capture.assert_not_called()
    assert exc_info.value.detail == detail


def test_non_isolation_failure_still_triggers_cf_capture():
    """Network-type dynamic_source_unavailable must still fire CF capture."""
    calls = {"n": 0}

    def side(*_a, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SourceError("dynamic_source_unavailable", "no_reader_images")
        return "ANALYSIS"

    with mock.patch.object(down, "_discover_chapter_source_once", side_effect=side), \
            mock.patch.object(down, "_comix_cf_autocapture_enabled", return_value=True), \
            mock.patch.object(down, "_run_comix_clearance_capture", return_value=True) as cap:
        result = down.discover_chapter_source(COMIX)
    assert result == "ANALYSIS"
    cap.assert_called_once()


def test_isolation_failure_propagates_correct_source_error_code():
    """After both attempts fail with isolation, the raised SourceError is dynamic_source_unavailable."""
    with mock.patch.object(down, "_discover_chapter_source_once",
                           side_effect=SourceError("dynamic_source_unavailable",
                                                   "dynamic_resolver_isolation_unavailable")), \
            mock.patch.object(down, "_comix_cf_autocapture_enabled", return_value=True), \
            mock.patch.object(down, "_run_comix_clearance_capture", return_value=True):
        with pytest.raises(SourceError) as exc_info:
            down.discover_chapter_source(COMIX)
    assert exc_info.value.code == "dynamic_source_unavailable"
    assert exc_info.value.detail == "dynamic_resolver_isolation_unavailable"


def test_capture_does_not_fire_for_non_comix_url_with_isolation_detail():
    """Non-comix URLs must not attempt capture even with a non-isolation detail."""
    with mock.patch.object(down, "_discover_chapter_source_once",
                           side_effect=SourceError("dynamic_source_unavailable", "no_reader_images")), \
            mock.patch.object(down, "_comix_cf_autocapture_enabled", return_value=True), \
            mock.patch.object(down, "_run_comix_clearance_capture") as cap:
        with pytest.raises(SourceError):
            down.discover_chapter_source("https://example.test/chapter/1")
    cap.assert_not_called()


# ---------------------------------------------------------------------------
# _load_comix_cf_override disk behaviour
# ---------------------------------------------------------------------------

def _resolver():
    from yomu.download.readers import scrapling_reader_resolver
    return scrapling_reader_resolver


def test_clearance_override_reads_cookie_and_ua_from_disk(tmp_path):
    r = _resolver()
    cookie_file = tmp_path / "comix_cf_cookie.json"
    cookie_file.write_text(
        json.dumps({"cf_clearance": "abc123", "user_agent": "TestAgent/1.0"}),
        encoding="utf-8",
    )
    with mock.patch.object(r, "_comix_cf_cookie_path", return_value=cookie_file), \
            mock.patch.object(r, "COMIX_CF_REUSE_ENABLED", True):
        result = r._load_comix_cf_override()
    assert result == ("abc123", "TestAgent/1.0")


def test_clearance_override_missing_file_returns_none(tmp_path):
    r = _resolver()
    absent = tmp_path / "no_such_file.json"
    with mock.patch.object(r, "_comix_cf_cookie_path", return_value=absent), \
            mock.patch.object(r, "COMIX_CF_REUSE_ENABLED", True):
        assert r._load_comix_cf_override() is None


def test_clearance_override_invalid_json_returns_none(tmp_path):
    r = _resolver()
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    with mock.patch.object(r, "_comix_cf_cookie_path", return_value=bad), \
            mock.patch.object(r, "COMIX_CF_REUSE_ENABLED", True):
        assert r._load_comix_cf_override() is None


def test_clearance_override_empty_cookie_returns_none(tmp_path):
    r = _resolver()
    cookie_file = tmp_path / "empty_cookie.json"
    cookie_file.write_text(
        json.dumps({"cf_clearance": "", "user_agent": "Agent/1"}),
        encoding="utf-8",
    )
    with mock.patch.object(r, "_comix_cf_cookie_path", return_value=cookie_file), \
            mock.patch.object(r, "COMIX_CF_REUSE_ENABLED", True):
        assert r._load_comix_cf_override() is None


def test_clearance_override_reuse_disabled_by_env(tmp_path):
    r = _resolver()
    cookie_file = tmp_path / "ok.json"
    cookie_file.write_text(
        json.dumps({"cf_clearance": "abc", "user_agent": "UA"}),
        encoding="utf-8",
    )
    with mock.patch.object(r, "_comix_cf_cookie_path", return_value=cookie_file), \
            mock.patch.object(r, "COMIX_CF_REUSE_ENABLED", False):
        assert r._load_comix_cf_override() is None


# ---------------------------------------------------------------------------
# Isolation failure telemetry
# ---------------------------------------------------------------------------

def test_isolation_failure_emits_telemetry_with_exception_type():
    """_resolve_in_child_process must emit telemetry before raising isolation error."""
    r = _resolver()
    emitted = []

    def _fake_run_child(*_a, **_kw):
        raise OSError(5, "fake access denied")

    def _fake_telemetry(event, **fields):
        emitted.append({"event": event, **fields})

    with mock.patch.object(r, "_run_child_command", side_effect=_fake_run_child), \
            mock.patch.object(r, "_append_telemetry", side_effect=_fake_telemetry):
        with pytest.raises(r.DynamicReaderError) as exc_info:
            r._resolve_in_child_process(
                "https://comix.to/title/x/1",
                cancel_check=None, timeout=30.0)

    assert exc_info.value.code == "dynamic_resolver_isolation_unavailable"
    iso_events = [e for e in emitted if e.get("error_code") == "dynamic_resolver_isolation_unavailable"]
    assert iso_events, "telemetry event for isolation_unavailable must be emitted"
    assert iso_events[0].get("exception_type") == "OSError"
