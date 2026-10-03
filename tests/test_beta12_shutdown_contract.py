from pathlib import Path

import desktop_app


def test_update_shutdown_requests_explicit_update_context(monkeypatch):
    calls = []
    monkeypatch.setattr(desktop_app, "_REQUEST_WINDOW_CLOSE", lambda reason: calls.append(reason))
    api = object.__new__(desktop_app.DesktopApi)
    assert api.request_shutdown() == {"requested": True}
    assert calls == ["update"]


def test_production_shutdown_contract_is_update_only_noninteractive():
    source = Path("desktop_app.py").read_text(encoding="utf-8")
    assert "window.confirm_close = False" in source
    assert "confirm_close=True" in source
    assert "UPDATE_SHUTDOWN_CONTEXT_SET" in source
    assert "API_SHUTDOWN_BEGIN" in source
    assert "WEBVIEW_LOOP_RETURNED" in source


def test_wait_all_helper_contract_is_unchanged():
    source = Path("installer_update.py").read_text(encoding="utf-8")
    assert "timeout" in source
    assert "wait_process_ids" in source
    assert "subprocess.Popen" in source
