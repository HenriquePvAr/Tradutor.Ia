import base64
import json
import os
import subprocess
import sys
import time

import pytest

from secure_auth_context import AuthEnvelopeStore
from yomu_backend_provider import MockBackendClient, YomuBackendTranslationProvider


def _token():
    enc = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
    payload = json.dumps({"exp": int(time.time()) + 3600}).encode()
    return enc(b'{"alg":"none"}') + "." + enc(payload) + ".sig"


@pytest.fixture(autouse=True)
def verified_commercial_device_uuid(monkeypatch):
    monkeypatch.setenv("TRADUTOR_DEVICE_UUID", "550e8400-e29b-41d4-a716-446655440000")


def test_pipeline_adapter_uses_stable_job_and_request_identity(tmp_path, monkeypatch):
    job_id = "job-pipeline"
    AuthEnvelopeStore(tmp_path).seal(job_id, _token())
    monkeypatch.setenv("TRADUTOR_JOB_ID", job_id)
    monkeypatch.setenv("TRADUTOR_REQUEST_ID", "translation:stable-request")
    backend = MockBackendClient()
    provider = YomuBackendTranslationProvider(runtime_root=tmp_path, backend=backend)

    first = provider.translate_many(["same", "same"])
    second = provider.translate_many(["same", "same"])

    assert first == ["[PT-BR] same", "[PT-BR] same"]
    assert second == first
    assert backend.provider_execution_count == 1
    assert backend.reserve_count == 1
    assert provider.stats["request_id"] == "translation:stable-request"


def test_pipeline_adapter_production_selects_real_client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADUTOR_JOB_ID", "job-pipeline")
    monkeypatch.delenv("TRADUTOR_REQUEST_ID", raising=False)
    monkeypatch.setenv("YOMU_ENV", "production")
    monkeypatch.setenv("YOMU_BACKEND_URL", "https://backend.example.test")
    from benchmark_pipeline import _build_translation_provider

    provider = _build_translation_provider("yomu_backend", translation_enabled=True)
    from yomu_backend_provider import HttpBackendClient
    assert provider.provider_name == "yomu_backend"
    assert isinstance(provider.backend, HttpBackendClient)
    assert provider.backend.transport is not None


def test_http_backend_transport_rebuilt_from_environment(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://backend.example.test")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
    from yomu_backend_provider import HttpBackendClient, RequestsBackendTransport
    client = HttpBackendClient.from_environment()
    assert isinstance(client.transport, RequestsBackendTransport)
    assert client.transport.base_url == "https://backend.example.test"
    assert client.transport._session.trust_env is False


def test_pipeline_adapter_production_rejects_test_mock(monkeypatch):
    monkeypatch.setenv("YOMU_ENV", "production")
    monkeypatch.setenv("YOMU_TEST_BACKEND", "mock")
    monkeypatch.setenv("YOMU_BACKEND_URL", "https://backend.example.test")
    from benchmark_pipeline import _build_translation_provider
    provider = _build_translation_provider("yomu_backend", translation_enabled=True)
    from yomu_backend_provider import MockBackendClient
    assert not isinstance(provider.backend, MockBackendClient)


def test_pipeline_adapter_disabled_does_not_construct_backend(monkeypatch):
    monkeypatch.setenv("YOMU_ENV", "test")
    assert _provider_factory("yomu_backend", False) is None


def test_pipeline_adapter_local_mock_requires_explicit_test_switch(monkeypatch):
    monkeypatch.setenv("YOMU_ENV", "local")
    monkeypatch.setenv("YOMU_TEST_BACKEND", "mock")
    from benchmark_pipeline import _build_translation_provider
    from yomu_backend_provider import MockBackendClient
    provider = _build_translation_provider("yomu_backend", translation_enabled=True)
    assert isinstance(provider.backend, MockBackendClient)


def test_provider_process_boundary_uses_job_envelope_not_cli_token(tmp_path):
    job_id = "job-child"
    AuthEnvelopeStore(tmp_path).seal(job_id, _token())
    code = (
        "from pathlib import Path; import os; "
        "from yomu_backend_provider import *; "
        "p=YomuBackendTranslationProvider(runtime_root=Path(os.environ['R']), backend=MockBackendClient()); "
        "print(p.translate_many(['child']))"
    )
    env = os.environ.copy()
    env.update({"R": str(tmp_path), "TRADUTOR_JOB_ID": job_id,
                "TRADUTOR_REQUEST_ID": "translation:child", "YOMU_ENV": "test"})
    result = subprocess.run([sys.executable, "-c", code], env=env,
                            capture_output=True, text=True, check=True)
    assert "[PT-BR] child" in result.stdout
    assert _token() not in result.args


def _provider_factory(provider, enabled):
    from benchmark_pipeline import _build_translation_provider
    return _build_translation_provider(provider, translation_enabled=enabled)
