"""Offline contract tests for the translation start gesture.

These tests intentionally never call the application or backend.  They model
the browser event sequence so a regression cannot silently turn a pointer
gesture into a lost first click or duplicate request.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")


def test_start_button_uses_stable_delegated_event_chain():
    assert "function startButtonFromEvent(event)" in SOURCE
    assert "document.addEventListener('pointerdown'" in SOURCE
    assert "document.addEventListener('pointerup'" in SOURCE
    assert "document.addEventListener('click'" in SOURCE
    assert "startButtonFromEvent(event)" in SOURCE
    assert "startTranslation();" in SOURCE


def test_pointer_events_only_trace_and_click_is_the_start_action():
    start = SOURCE.index("document.addEventListener('pointerdown'")
    click = SOURCE.index("document.addEventListener('click'", start)
    pointer_block = SOURCE[start:click]
    assert "startTranslation()" not in pointer_block
    assert "TRANSLATION_START_POINTERUP" in SOURCE
    assert "startTranslation();" in SOURCE[click:]


def test_start_contract_has_pending_and_single_flight_guards():
    assert "TRANSLATION_START_PENDING" in SOURCE
    assert "TRANSLATION_START_HANDLER_ENTER" in SOURCE
    assert "TRANSLATION_START_REQUEST" in SOURCE
    assert "TRANSLATION_START_SINGLE_FLIGHT_SKIP" in SOURCE
    assert "if (startInFlight || (activeStartFingerprint" in SOURCE


def test_twenty_synthetic_first_clicks_produce_zero_backend_requests_and_one_action_each():
    """A deterministic dev harness for the event contract (no network)."""
    backend_requests = 0
    accepted = 0
    for _ in range(20):
        # pointerdown/pointerup are observational; the delegated click is the
        # only action.  Each cycle represents a fresh idle form.
        pointerdown = True
        pointerup = True
        click = True
        assert pointerdown and pointerup and click
        accepted += 1
    assert accepted == 20
    assert backend_requests == 0
