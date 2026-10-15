from __future__ import annotations

import requests

from yomu.runtime.remote_readiness import probe_remote_clean_readiness
from yomu.runtime.transport_errors import classify_transport_error
from yomu.ui import app as app_ui


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _ready_responses():
    return [
        _Response({
            "api": "UP", "queue": "UP", "worker": "UP",
            "worker_health": {"ready_consumers": 1, "gemini_ui_available": True},
        }),
        _Response({
            "schema_version": "safe-clean.v2",
            "supported_schemas": ["safe-clean.v2", "remote-visual-recovery.v1"],
            "translation_enabled": False,
        }),
    ]


def test_readiness_is_loopback_only_and_does_not_claim_provider_auth(monkeypatch):
    monkeypatch.setenv("YOMU_GEMINI_REMOTE_URL", "http://127.0.0.1:18777")
    responses = _ready_responses()
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return responses.pop(0)

    result = probe_remote_clean_readiness(get)
    assert result["ready_for_remote_dispatch"] is True
    assert result["gemini_ui_state"] == "UI_AVAILABLE"
    assert result["provider_auth"] == "NOT_VERIFIED"
    assert result["ready_for_real_generation"] is False
    assert [call[0] for call in calls] == [
        "http://127.0.0.1:18777/health",
        "http://127.0.0.1:18777/v1/hard-clean/schema",
    ]
    assert all(call[1] == {"timeout": 2, "allow_redirects": False} for call in calls)


def test_readiness_rejects_non_loopback_and_never_sends_request(monkeypatch):
    monkeypatch.setenv("YOMU_GEMINI_REMOTE_URL", "https://example.invalid")
    called = False

    def get(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("non-loopback URL must not be contacted")

    result = probe_remote_clean_readiness(get)
    assert result["remote_error_code"] == "REMOTE_CONFIG_ERROR"
    assert result["ready_for_remote_dispatch"] is False
    assert called is False


def test_readiness_requires_worker_consumer_and_both_schemas(monkeypatch):
    monkeypatch.setenv("YOMU_GEMINI_REMOTE_URL", "http://localhost:18777")
    responses = [
        _Response({"api": "UP", "queue": "UP", "worker": "UP",
                   "worker_health": {"ready_consumers": 0, "gemini_ui_available": True}}),
        _Response({"schema_version": "safe-clean.v2",
                   "supported_schemas": ["safe-clean.v2"], "translation_enabled": False}),
    ]
    result = probe_remote_clean_readiness(lambda *_args, **_kwargs: responses.pop(0))
    assert result["status"] == "degraded"
    assert result["consumer"] == "UNAVAILABLE"
    assert result["schema"] == "INCOMPATIBLE"
    assert result["ready_for_remote_dispatch"] is False
    assert result["provider_auth"] == "NOT_VERIFIED"


def test_readiness_reports_queue_and_worker_gates_separately(monkeypatch):
    monkeypatch.setenv("YOMU_GEMINI_REMOTE_URL", "http://127.0.0.1:18777")
    queue_down = [
        _Response({"api": "UP", "queue": "DOWN", "worker": "UP",
                   "worker_health": {"ready_consumers": 1}}),
        _Response({"schema_version": "safe-clean.v2",
                   "supported_schemas": ["safe-clean.v2", "remote-visual-recovery.v1"],
                   "translation_enabled": False}),
    ]
    result = probe_remote_clean_readiness(lambda *_args, **_kwargs: queue_down.pop(0))
    assert result["remote_error_code"] == "QUEUE_UNAVAILABLE"
    worker_down = [
        _Response({"api": "UP", "queue": "UP", "worker": "DOWN",
                   "worker_health": {"ready_consumers": 0}}),
        _Response({"schema_version": "safe-clean.v2",
                   "supported_schemas": ["safe-clean.v2", "remote-visual-recovery.v1"],
                   "translation_enabled": False}),
    ]
    result = probe_remote_clean_readiness(lambda *_args, **_kwargs: worker_down.pop(0))
    assert result["remote_error_code"] == "WORKER_UNAVAILABLE"


def test_readiness_classifies_connection_refusal_without_exposing_exception(monkeypatch):
    monkeypatch.setenv("YOMU_GEMINI_REMOTE_URL", "http://127.0.0.1:18777")
    error = requests.exceptions.ConnectionError(
        "HTTPConnectionPool(host='127.0.0.1', port=18777): [Errno 10061] refused"
    )
    result = probe_remote_clean_readiness(lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
    assert result["remote_error_code"] == "CONNECTION_REFUSED"
    assert "127.0.0.1" not in str(result)


def test_transport_error_classifies_timeout_reset_and_tls():
    assert classify_transport_error(requests.exceptions.Timeout()) == "HTTP_TIMEOUT"
    assert classify_transport_error(requests.exceptions.ConnectionError(
        "connection reset [Errno 10054]")) == "CONNECTION_RESET"
    assert classify_transport_error(requests.exceptions.SSLError()) == "TLS_ERROR"


def test_ui_readiness_endpoint_uses_read_only_probe(monkeypatch):
    monkeypatch.setattr(app_ui, "probe_remote_clean_readiness", lambda: {
        "ready_for_remote_dispatch": False, "provider_auth": "NOT_VERIFIED",
    })
    assert app_ui.api_remote_clean_readiness() == {
        "ready_for_remote_dispatch": False, "provider_auth": "NOT_VERIFIED",
    }
