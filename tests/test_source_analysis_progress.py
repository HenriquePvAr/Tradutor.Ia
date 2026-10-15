"""Live source-analysis progress + cooperative-cancel registry (Webtoon 'parece parado' fix)."""
import asyncio

import pytest
from fastapi import HTTPException

import yomu.review.source_analysis_progress as sap


def _fresh():
    # Isolate each test from module-global state.
    with sap._LOCK:
        sap._TRACES.clear()


def test_stage_transitions_and_candidate_count():
    _fresh()
    task = sap.start("owner-a", "t1")
    assert sap.snapshot("owner-a", "t1")["stage"] == "validating"
    sap.record(task, "DYNAMIC_RESOLVER_START")
    assert sap.snapshot("owner-a", "t1")["stage"] == "opening"
    sap.record(task, "SOURCE_FALLBACK_DECISION", candidate_count=37)
    snap = sap.snapshot("owner-a", "t1")
    assert snap["candidates_found"] == 37
    assert "37" in snap["message"] and "Localizando" in snap["message"]
    assert snap["done"] is False


def test_finish_marks_done():
    _fresh()
    task = sap.start("owner-a", "t2")
    sap.finish(task)
    assert sap.snapshot("owner-a", "t2")["done"] is True


def test_unknown_trace_returns_none():
    _fresh()
    assert sap.snapshot("owner-a", "nope") is None


def test_record_updates_only_registered_generation():
    _fresh()
    task = sap.start("owner-a", "t3")
    sap.record(task, "DYNAMIC_RESOLVER_START")
    assert sap.snapshot("owner-a", "t3")["stage"] == "opening"


def test_cancel_flag_lifecycle():
    _fresh()
    task = sap.start("owner-a", "t4")
    assert sap.is_cancelled(task) is False
    assert sap.request_cancel("owner-a", "t4") is True
    assert sap.is_cancelled(task) is True
    assert sap.request_cancel("owner-a", "unknown") is False


def test_finished_trace_can_be_reused_without_inheriting_cancel():
    _fresh()
    task = sap.start("owner-a", "t5")
    assert sap.request_cancel("owner-a", "t5") is True
    assert sap.is_cancelled(task) is True
    sap.finish(task)
    with pytest.raises(sap.DuplicateSourceAnalysisTrace):
        sap.start("owner-a", "t5")
    next_task = sap.start("owner-a", "t5-next")
    assert sap.is_cancelled(next_task) is False


def test_registry_is_bounded():
    _fresh()
    for i in range(sap._MAX_TRACES + 20):
        task = sap.start("owner-a", f"t{i}")
        sap.finish(task)
    assert len(sap._TRACES) <= sap._MAX_TRACES
    # oldest evicted, newest kept
    assert sap.snapshot("owner-a", "t0") is None
    assert sap.snapshot("owner-a", f"t{sap._MAX_TRACES + 19}") is not None


def test_elapsed_is_reported_non_negative():
    _fresh()
    sap.start("owner-a", "t6")
    assert sap.snapshot("owner-a", "t6")["elapsed_ms"] >= 0


def test_unauthenticated_source_analysis_does_not_register_trace(monkeypatch):
    _fresh()
    import yomu.ui.app as app_ui

    monkeypatch.setattr(app_ui, "_append_diagnostic_log", lambda *args, **kwargs: None)

    def reject_auth(*args, **kwargs):
        raise HTTPException(status_code=401, detail="authentication_required")

    monkeypatch.setattr(app_ui, "_ui_principal", reject_auth)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(app_ui.api_source_analyze(object(), {"trace_id": "untrusted-trace"}))

    assert exc_info.value.status_code == 401
    assert sap.snapshot("owner-a", "untrusted-trace") is None
