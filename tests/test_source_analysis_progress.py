"""Live source-analysis progress + cooperative-cancel registry (Webtoon 'parece parado' fix)."""
import source_analysis_progress as sap


def _fresh():
    # Isolate each test from module-global state.
    sap._TRACES.clear()
    sap._CANCELLED.clear()


def test_stage_transitions_and_candidate_count():
    _fresh()
    sap.start("t1")
    assert sap.snapshot("t1")["stage"] == "validating"
    sap.record("t1", "DYNAMIC_RESOLVER_START")
    assert sap.snapshot("t1")["stage"] == "opening"
    sap.record("t1", "SOURCE_FALLBACK_DECISION", candidate_count=37)
    snap = sap.snapshot("t1")
    assert snap["candidates_found"] == 37
    assert "37" in snap["message"] and "Localizando" in snap["message"]
    assert snap["done"] is False


def test_finish_marks_done():
    _fresh()
    sap.start("t2")
    sap.finish("t2")
    assert sap.snapshot("t2")["done"] is True


def test_unknown_trace_returns_none():
    _fresh()
    assert sap.snapshot("nope") is None


def test_record_without_start_still_creates_entry():
    _fresh()
    sap.record("t3", "DYNAMIC_RESOLVER_START")
    assert sap.snapshot("t3")["stage"] == "opening"


def test_cancel_flag_lifecycle():
    _fresh()
    sap.start("t4")
    assert sap.is_cancelled("t4") is False
    assert sap.request_cancel("t4") is True      # known in-flight trace
    assert sap.is_cancelled("t4") is True
    assert sap.request_cancel("unknown") is False  # not an in-flight trace


def test_start_clears_stale_cancel_flag():
    _fresh()
    sap.request_cancel("t5")
    assert sap.is_cancelled("t5") is True
    sap.start("t5")  # a fresh run must not inherit a stale cancel
    assert sap.is_cancelled("t5") is False


def test_registry_is_bounded():
    _fresh()
    for i in range(sap._MAX_TRACES + 20):
        sap.start(f"t{i}")
    assert len(sap._TRACES) <= sap._MAX_TRACES
    # oldest evicted, newest kept
    assert sap.snapshot("t0") is None
    assert sap.snapshot(f"t{sap._MAX_TRACES + 19}") is not None


def test_elapsed_is_reported_non_negative():
    _fresh()
    sap.start("t6")
    assert sap.snapshot("t6")["elapsed_ms"] >= 0
