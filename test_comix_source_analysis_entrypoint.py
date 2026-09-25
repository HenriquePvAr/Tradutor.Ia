"""Hermetic integration coverage for the real UI source-analysis entrypoint."""

import _test_bootstrap  # noqa: F401

import asyncio
from types import SimpleNamespace
from unittest import mock

import pytest
import app_ui
import down
from chapter_source import SourceError
from universal_chapter_adapter import SourceAnalysis, SUPPORTED_SPECIFIC_ADAPTER
from ui_bridge import UiBridge


URL = "https://comix.to/title/k72ge-home/1536897-chapter-1"


@pytest.mark.parametrize("code,detail,status", [
    ("challenge_required", "navigation_preflight", 200),
    ("source_access_denied", "reader_api_status_403", 403),
    ("source_unavailable", "navigation_preflight_http", 522),
])
def test_ui_source_analysis_entrypoint_routes_recoverable_comix_failures(
        tmp_path, monkeypatch, code, detail, status):
    monkeypatch.setenv("TRADUTOR_TEST_RUNTIME_ROOT", str(tmp_path / "runtime"))
    bridge = UiBridge()
    logged = []
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
    principal = SimpleNamespace(owner_id="local", user_id="local", authenticated=True)
    preflight_error = SourceError(code, detail)
    preflight_error.preflight_result = {
        "adapter": "comix", "http_status": status,
        "reason_code": code,
        "classification": "source_captcha_detected" if code == "challenge_required"
        else "transient_browser_fallback" if status == 522 else "source_access_denied",
    }
    analysis = SourceAnalysis(
        adapter="comix", final_host="comix.to", outcome=SUPPORTED_SPECIFIC_ADAPTER,
        confidence=1.0, accepted=[],
    )
    monkeypatch.setattr(app_ui, "BRIDGE", bridge)
    monkeypatch.setattr(app_ui, "_ui_principal", lambda *_args, **_kwargs: principal)
    monkeypatch.setattr(
        app_ui, "_append_diagnostic_log",
        lambda _file, event, **fields: logged.append((event, fields)))

    try:
        with mock.patch("http_source_discovery.discover_via_http", return_value=None), \
             mock.patch.object(down, "analyze_chapter_source", side_effect=preflight_error), \
             mock.patch("scrapling_reader_resolver.resolve", return_value=analysis) as resolver:
            response = asyncio.run(app_ui.api_source_analyze(
                request, {"source_type": "url", "url": URL, "trace_id": "trace-test"}))
        resolver.assert_called_once()
        assert response["ready"] is True
        names = [name for name, _ in logged]
        assert "SOURCE_FALLBACK_DECISION" in names
        assert "DYNAMIC_RESOLVER_START" in names
        assert "DYNAMIC_RESOLVER_END" in names
        assert all(fields.get("trace_id") == "trace-test"
                   for name, fields in logged
                   if name in {"SOURCE_FALLBACK_DECISION", "DYNAMIC_RESOLVER_START",
                               "DYNAMIC_RESOLVER_END"})
        assert bridge.store.list_jobs(statuses=None, limit=None) == []
    finally:
        bridge.close()
