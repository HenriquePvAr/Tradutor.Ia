"""Guard terminal pipeline notifications against polling/render loops."""

from __future__ import annotations

import _test_bootstrap  # noqa: F401

from pathlib import Path


ROOT = Path(__file__).resolve().parent
UI_SOURCE = ROOT / "static" / "tradutor_ui.js"


def _source() -> str:
    return UI_SOURCE.read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index)
    return source[start_index:end_index]


def test_render_runtime_does_not_emit_terminal_toasts():
    source = _source()
    render_runtime = _between(source, "function renderRuntime(runtime)", "function renderRunStatus")

    assert "showToast(" not in render_runtime
    assert "flashFrame(" not in render_runtime
    assert "PDF finalizado e registrado no histórico" not in render_runtime


def test_terminal_toasts_are_transition_based_and_persisted_for_f5():
    source = _source()

    assert "function handleTerminalRuntimeTransition(runtime)" in source
    assert "function rememberRuntimeTerminalState(runtime)" in source
    assert "TERMINAL_NOTIFICATION_STORAGE_KEY" in source
    assert "sessionStorage.setItem" in source
    assert "consumedTerminalNotifications.has(key)" in source
    assert "showToast(notification.message, notification.type, {key, kind: 'terminal'})" in source


def test_bootstrap_primes_terminal_state_without_toast():
    source = _source()
    bootstrap_tail = _between(source, "async function refreshBootstrap()", "async function pollState()")

    assert "renderRuntime(data);" in bootstrap_tail
    assert "rememberRuntimeTerminalState(data);" in bootstrap_tail
    assert "handleTerminalRuntimeTransition(data);" not in bootstrap_tail


def test_polling_handles_terminal_transition_after_render():
    source = _source()
    poll_state = _between(source, "async function pollState()", "refreshBootstrap();")

    assert "renderRuntime(data);" in poll_state
    assert "handleTerminalRuntimeTransition(data);" in poll_state
    assert poll_state.index("renderRuntime(data);") < poll_state.index(
        "handleTerminalRuntimeTransition(data);"
    )
    assert "EVENT_CURSOR_ADVANCED" in poll_state


def test_toast_manager_deduplicates_and_bounds_visible_toasts():
    source = _source()

    assert "const MAX_VISIBLE_TOASTS = 3;" in source
    assert "const toastRegistry = new Map();" in source
    assert "function removeToastByKey(key)" in source
    assert "function scheduleToastRemoval(key, toast)" in source
    assert "toast.dataset.toastKey = key;" in source
    assert "while (toastRegistry.size >= MAX_VISIBLE_TOASTS)" in source
    assert "existing?.node?.isConnected" in source


def test_failed_terminal_job_does_not_promote_shell_badge_to_error():
    source = _source()
    render_runtime = _between(source, "function renderRuntime(runtime)", "function renderRunStatus")

    assert "let presentationStatus = appState.status;" in render_runtime
    assert "presentationStatus = String(terminalLatest.status || appState.status);" in render_runtime
    assert "appState.status = String(terminalLatest.status" not in render_runtime
    assert "status.textContent = runStatusLabels[appState.status] || appState.status;" in render_runtime


def test_first_terminal_observation_refreshes_wallet_before_notification_dedupe():
    source = _source()
    handler = _between(source, "function handleTerminalRuntimeTransition(runtime)", "function canonicalProgressStage")
    assert "const shouldRefreshWallet = !previous || !terminalRunStatuses.has(previous);" in handler
    assert handler.index("if (shouldRefreshWallet) void refreshWalletAfterTerminal(record);") < handler.index(
        "if (!previous || terminalRunStatuses.has(previous) || !notification)"
    )
    assert "YK_WALLET_POST_FINALIZE_REFRESH_START" in source
    assert "YK_WALLET_POST_FINALIZE_REFRESH_RESULT" in source


def test_terminal_wallet_refresh_does_not_storm_on_duplicate_terminal_state():
    source = _source()
    handler = _between(source, "function handleTerminalRuntimeTransition(runtime)", "function canonicalProgressStage")
    # Refresh is gated by the prior terminal status; duplicate terminal
    # observations still take the existing notification-dedupe path.
    assert "!terminalRunStatuses.has(previous)" in handler
    assert handler.count("refreshWalletAfterTerminal(record)") == 1


def test_translation_start_has_single_flight_identity_and_trace():
    source = _source()
    assert "let activeStartFingerprint = '';" in source
    assert "TRANSLATION_START_IGNORED_IN_FLIGHT" in source
    assert "TRANSLATION_START_ACCEPTED" in source
    assert "startInFlight || (activeStartFingerprint && activeStartFingerprint === fingerprint)" in source


def test_translation_start_takes_visual_lock_before_async_policy_refresh():
    source = _source()
    start = _between(source, "async function runStartTranslation(sequence = 0)", "function visibleCancelControl")
    assert start.index("button.dataset.busy = '1';") < start.index("await controlPlane.bootstrap()")
    assert "button.setAttribute('aria-busy', 'true');" in start
    assert "TRANSLATION_START_IGNORED_IN_FLIGHT" in source


def test_translation_start_remains_disabled_while_request_is_in_flight():
    source = _source()
    controls = _between(source, "function updateTranslationStartControls()", "function invalidateSourceValidation")
    assert "const startRequestBusy = Boolean(startInFlight || activeStartFingerprint);" in controls
    assert "|| startRequestBusy" in controls


def test_translation_start_r3_persists_pointer_and_lifecycle_telemetry():
    source = _source()
    for event in (
        "TRANSLATION_START_POINTER",
        "TRANSLATION_START_CLICK",
        "TRANSLATION_START_HANDLER_ENTER",
        "TRANSLATION_START_PENDING",
        "TRANSLATION_START_REQUEST",
        "TRANSLATION_START_RESPONSE",
        "TRANSLATION_START_ERROR",
        "TRANSLATION_START_SINGLE_FLIGHT_SKIP",
    ):
        assert event in source
    assert "sequence" in source
    assert "request_count" in source
