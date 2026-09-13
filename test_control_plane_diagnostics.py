from pathlib import Path
import asyncio
import json


CLIENT = Path("static/control_plane_client.js").read_text(encoding="utf-8")
APP = Path("app_ui.py").read_text(encoding="utf-8")


def test_control_plane_trace_uses_existing_app_diagnostic_sink():
    assert "api_control_plane_trace" in APP
    assert '_append_diagnostic_log("app_current.jsonl"' in APP


def test_bootstrap_and_ensure_device_events_are_instrumented():
    assert "CONTROL_PLANE_BOOTSTRAP_STARTED" in CLIENT
    assert "CONTROL_PLANE_BOOTSTRAP_RESULT" in CLIENT
    assert "CONTROL_PLANE_LICENSE_STATE" in CLIENT
    assert "CONTROL_PLANE_ENSURE_DEVICE_STARTED" in CLIENT


def test_identity_registration_challenge_sign_and_verify_events_are_instrumented():
    for event in (
        "INSTALL_IDENTITY_REQUESTED", "INSTALL_IDENTITY_RESULT",
        "DEVICE_REGISTER_STARTED", "DEVICE_REGISTER_RESULT",
        "DEVICE_CHALLENGE_STARTED", "DEVICE_CHALLENGE_RESULT",
        "DEVICE_SIGN_STARTED", "DEVICE_SIGN_RESULT",
        "DEVICE_VERIFY_STARTED", "DEVICE_VERIFY_RESULT",
    ):
        assert event in CLIENT


def test_auth_restore_and_auth_event_are_instrumented():
    assert "AUTH_RESTORED_SESSION_DETECTED" in CLIENT
    assert "AUTH_CHANGED_EVENT_RECEIVED" in CLIENT
    assert "tradutor-auth-changed" in CLIENT


def test_heartbeat_and_error_events_are_instrumented():
    assert "DEVICE_HEARTBEAT_STARTED" in CLIENT
    assert "DEVICE_HEARTBEAT_RESULT" in CLIENT
    assert "CONTROL_PLANE_ERROR" in CLIENT


def test_diagnostic_payload_excludes_credentials_and_crypto_material():
    # The client sends only an explicit allow-list, never the request body or
    # cryptographic values (nonce/signature/public/private keys).
    assert "Authorization" not in CLIENT[CLIENT.find("const payload"):CLIENT.find("const payload") + 1200]
    assert "signature: signed.signature" in CLIENT  # remains only in the API request
    assert "nonce: challenge.nonce" in CLIENT  # remains only in the API request
    endpoint = APP[APP.find('async def api_control_plane_trace'):APP.find('async def api_profile_media', APP.find('async def api_control_plane_trace'))]
    for forbidden in ("access_token", "refresh_token", "service_role", "private_key", "signature", "nonce"):
        assert forbidden not in endpoint


def test_diagnostic_sink_is_best_effort():
    # The existing sink catches filesystem errors; the UI sender also catches
    # fetch failures, so diagnostics cannot block the main flow.
    assert "except OSError:" in APP[APP.find("def _append_diagnostic_log"):APP.find("def _source_trace_id")]
    assert "_append_diagnostic_log" in APP[APP.find('async def api_control_plane_trace'):]


def test_control_plane_diagnostics_are_not_a_second_runtime_store():
    assert "window.__tradutorUiTrace" in CLIENT
    assert "fetch('/api/ui/control-plane-trace'" in CLIENT


def test_ui_trace_policy_events_use_same_control_plane_sink():
    ui = Path("static/tradutor_ui.js").read_text(encoding="utf-8")
    assert "UI_TRACE_CHANNEL_PROBE" in ui
    assert "fetch('/api/ui/control-plane-trace'" in ui
    assert "UI_TRACE_FETCH_STATUS" in ui and "UI_TRACE_FETCH_ERROR" in ui


def test_server_records_ui_trace_request_before_sanitizer():
    assert "UI_TRACE_HTTP_REQUEST_RECEIVED" in APP
    start = APP.find("async def api_control_plane_trace")
    assert "top_level_keys" in APP[start:start + 5000]


def test_wallet_numeric_fields_survive_final_jsonl_and_auth_diagnostics_endpoint(tmp_path, monkeypatch):
    import app_ui

    monkeypatch.setattr(app_ui, "DIAGNOSTICS_ROOT", tmp_path)

    class Request:
        def __init__(self, payload): self.payload = payload
        async def json(self): return self.payload

    payload = {
        "event": "WALLET_NORMALIZED", "source": "wallet-summary", "status": "ready",
        "daily": 5, "subscription": 2, "permanent": 3, "reserved": 1, "active_yk": 9,
        "top_text": "9 YK", "rewards_text": "9 YK",
        "access_token": "SECRET", "Authorization": "Bearer SECRET", "email": "x@example.com",
    }
    asyncio.run(app_ui.api_control_plane_trace(Request(payload)))
    rows = [json.loads(line) for line in (tmp_path / "app_current.jsonl").read_text(encoding="utf-8").splitlines()]
    event = rows[-1]
    for key, value in {"daily": 5, "subscription": 2, "permanent": 3, "reserved": 1, "active_yk": 9, "top_text": "9 YK", "rewards_text": "9 YK"}.items():
        assert event[key] == value
    for forbidden in ("access_token", "Authorization", "email"):
        assert forbidden not in event

    monkeypatch.setattr(app_ui, "_AUTH_DIAGNOSTICS_ENABLED", True)
    response = asyncio.run(app_ui.auth_diagnostics_post(Request(payload)))
    body = json.loads(response.body)
    assert body == {"status": "accepted"}
    auth_event = list(app_ui._AUTH_DIAGNOSTIC_EVENTS)[-1]
    assert auth_event["daily"] == 5 and auth_event["active_yk"] == 9
    assert "access_token" not in auth_event and "Authorization" not in auth_event and "email" not in auth_event
