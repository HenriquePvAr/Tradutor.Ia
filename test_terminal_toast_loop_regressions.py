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
