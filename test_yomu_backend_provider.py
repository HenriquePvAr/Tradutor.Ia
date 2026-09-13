import base64
import json
import os
import threading
import time
from pathlib import Path

import pytest

from secure_auth_context import AuthEnvelopeStore
from yomu_backend_provider import (BackendTranslationError, BatchItem, BatchRequest,
                                   HttpBackendClient, MockBackendClient,
                                   YomuBackendTranslationProvider)


def tok(exp=None):
    exp = exp or int(time.time()) + 3600
    enc = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
    return enc(b'{"alg":"none"}') + "." + enc(json.dumps({"exp": exp}).encode()) + ".sig"


def request(job="job-1", rid="req-1"):
    return BatchRequest(rid, job, "JA", "PT-BR", (BatchItem("a", "Hello"), BatchItem("b", "Hello")))


def test_batch_mapping_duplicate_text_and_idempotency(tmp_path: Path):
    AuthEnvelopeStore(tmp_path).seal("job-1", tok())
    backend = MockBackendClient()
    provider = YomuBackendTranslationProvider(runtime_root=tmp_path, backend=backend)
    first = provider.translate_batch(request())
    second = provider.translate_batch(request())
    assert [x.item_id for x in first.items] == ["a", "b"]
    assert first == second
    assert backend.reserve_count == 1
    assert backend.provider_execution_count == 1


def test_concurrent_duplicate_is_one_operation(tmp_path: Path):
    AuthEnvelopeStore(tmp_path).seal("job-1", tok())
    backend = MockBackendClient()
    provider = YomuBackendTranslationProvider(runtime_root=tmp_path, backend=backend)
    out = []
    threads = [threading.Thread(target=lambda: out.append(provider.translate_batch(request()))) for _ in range(2)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert len(out) == 2 and out[0] == out[1]
    assert backend.provider_execution_count == 1 and backend.reserve_count == 1


def test_auth_failure_and_mapping_failure_fail_closed(tmp_path: Path):
    provider = YomuBackendTranslationProvider(runtime_root=tmp_path, backend=MockBackendClient())
    with pytest.raises(BackendTranslationError, match="auth_context"):
        provider.translate_batch(request())
    AuthEnvelopeStore(tmp_path).seal("job-1", tok())
    with pytest.raises(BackendTranslationError, match="PROVIDER_FAILED"):
        YomuBackendTranslationProvider(runtime_root=tmp_path, backend=MockBackendClient(fail="PROVIDER_FAILED")).translate_batch(request())


def test_chunking_keeps_one_logical_reservation(tmp_path: Path):
    AuthEnvelopeStore(tmp_path).seal("job-1", tok())
    items = tuple(BatchItem(str(i), "x") for i in range(5))
    backend = MockBackendClient(chunk_size=2)
    provider = YomuBackendTranslationProvider(runtime_root=tmp_path, backend=backend)
    provider.translate_batch(BatchRequest("req", "job-1", "JA", "PT-BR", items))
    assert backend.reserve_count == 1
    assert backend.provider_chunk_count == 3


def test_translate_many_splits_large_logical_batch_and_reuses_reservation(tmp_path: Path):
    job_id = "job-large"
    AuthEnvelopeStore(tmp_path).seal(job_id, tok())
    backend = MockBackendClient()
    provider = YomuBackendTranslationProvider(runtime_root=tmp_path, backend=backend)
    old_job, old_request = os.environ.get("TRADUTOR_JOB_ID"), os.environ.get("TRADUTOR_REQUEST_ID")
    os.environ["TRADUTOR_JOB_ID"] = job_id
    os.environ["TRADUTOR_REQUEST_ID"] = "translation:large"
    try:
        result = provider.translate_many(["x" * 4000] * 35)
    finally:
        if old_job is None: os.environ.pop("TRADUTOR_JOB_ID", None)
        else: os.environ["TRADUTOR_JOB_ID"] = old_job
        if old_request is None: os.environ.pop("TRADUTOR_REQUEST_ID", None)
        else: os.environ["TRADUTOR_REQUEST_ID"] = old_request
    assert len(result) == 35
    assert provider.stats["translation_batches"] == 2
    assert backend.reserve_count == 1


def test_provider_uses_verified_license_device_row_uuid_for_reservation(tmp_path: Path, monkeypatch):
    class CaptureBackend(MockBackendClient):
        def reserve(self, *, job_id, request_id, auth_token, device_id=""):
            self.captured_device_id = device_id
            return super().reserve(job_id=job_id, request_id=request_id, auth_token=auth_token, device_id=device_id)

    monkeypatch.setenv("TRADUTOR_DEVICE_UUID", "550e8400-e29b-41d4-a716-446655440000")
    AuthEnvelopeStore(tmp_path).seal("job-1", tok())
    backend = CaptureBackend()
    YomuBackendTranslationProvider(runtime_root=tmp_path, backend=backend).translate_batch(request())
    assert backend.captured_device_id == "550e8400-e29b-41d4-a716-446655440000"


def test_invalid_install_device_id_fails_before_remote(tmp_path: Path, monkeypatch):
    class FailingBackend(MockBackendClient):
        def reserve(self, **kwargs):
            raise AssertionError("remote reserve must not be reached")

    monkeypatch.setenv("TRADUTOR_DEVICE_UUID", "ys-install-123")
    AuthEnvelopeStore(tmp_path).seal("job-1", tok())
    with pytest.raises(BackendTranslationError, match="invalid_license_device_uuid"):
        YomuBackendTranslationProvider(runtime_root=tmp_path, backend=FailingBackend()).translate_batch(request())


def test_translation_execute_contract_carries_device_and_reservation(tmp_path: Path, monkeypatch):
    class CaptureBackend(MockBackendClient):
        def translate_batch(self, request, *, reservation_id, auth_token):
            self.captured = (request, reservation_id)
            return super().translate_batch(request, reservation_id=reservation_id, auth_token=auth_token)

    device = "550e8400-e29b-41d4-a716-446655440000"
    monkeypatch.setenv("TRADUTOR_DEVICE_UUID", device)
    AuthEnvelopeStore(tmp_path).seal("job-1", tok())
    backend = CaptureBackend()
    YomuBackendTranslationProvider(runtime_root=tmp_path, backend=backend).translate_batch(request())
    assert backend.captured[1].startswith("res_")
    assert backend.captured[0].request_id == "req-1"
    assert backend.captured[0].job_id == "job-1"


def test_http_translation_execute_payload_contains_device_and_reservation(monkeypatch):
    calls = []

    def transport(path, payload, token):
        calls.append((path, payload, token))
        if path.endswith("wallet-reserve"):
            return {"reservation_id": "550e8400-e29b-41d4-a716-446655440001", "required_yk": 1}
        return {"request_id": payload["request_id"], "status": "completed",
                "reservation_id": payload["reservation_id"],
                "items": [{"item_id": "a", "translated_text": "Oi"},
                          {"item_id": "b", "translated_text": "Olá"}]}

    monkeypatch.setenv("TRADUTOR_DEVICE_UUID", "550e8400-e29b-41d4-a716-446655440000")
    client = HttpBackendClient("http://localhost:54321", transport=transport)
    reservation_id = "550e8400-e29b-41d4-a716-446655440001"
    response = client.translate_batch(request(), reservation_id=reservation_id, auth_token="token")
    assert response.reservation_id == reservation_id
    path, payload, token = calls[-1]
    assert path.endswith("translation-execute")
    assert token == "token"
    assert payload["device_id"] == "550e8400-e29b-41d4-a716-446655440000"
    assert payload["reservation_id"] == reservation_id
    assert payload["request_id"] == "req-1"
    assert payload["job_id"] == "job-1"
    assert [item["item_id"] for item in payload["items"]] == ["a", "b"]


def test_translation_execute_incident_shape_9_items_262_chars(monkeypatch):
    captured = {}

    def transport(path, payload, token):
        captured.update(payload)
        if path.endswith("wallet-reserve"):
            return {"reservation_id": "550e8400-e29b-41d4-a716-446655440001"}
        return {"request_id": payload["request_id"], "status": "completed",
                "reservation_id": payload["reservation_id"],
                "items": [{"item_id": i["item_id"], "translated_text": "ok"} for i in payload["items"]]}

    monkeypatch.setenv("TRADUTOR_DEVICE_UUID", "550e8400-e29b-41d4-a716-446655440000")
    client = HttpBackendClient("http://localhost:54321", transport=transport)
    items = tuple(BatchItem(f"item-{i}", "x" * (254 if i == 0 else 1)) for i in range(9))
    client.translate_batch(BatchRequest("req-incident", "job-incident", "JA", "PT-BR", items),
                           reservation_id="550e8400-e29b-41d4-a716-446655440001", auth_token="token")
    assert len(captured["items"]) == 9
    assert sum(len(item["text"]) for item in captured["items"]) == 262
    assert len({item["item_id"] for item in captured["items"]}) == 9


@pytest.mark.parametrize(("field", "value", "error"), [
    ("request_id", "", "request_id_missing"),
    ("device_id", "ys-install-1", "device_id_invalid_uuid"),
    ("reservation_id", "", "reservation_id_missing"),
    ("items", [], "items_empty"),
])
def test_translation_execute_preflight_rejects_invalid_shape(field, value, error):
    from yomu_backend_provider import _translation_execute_shape
    payload = {"request_id": "req", "job_id": "job",
               "device_id": "550e8400-e29b-41d4-a716-446655440000",
               "reservation_id": "550e8400-e29b-41d4-a716-446655440001",
               "source_lang": "JA", "target_lang": "PT-BR",
               "items": [{"item_id": "a", "text": "x"}]}
    payload[field] = value
    with pytest.raises(BackendTranslationError, match=error):
        _translation_execute_shape(payload)


def test_request_shape_is_persisted_before_http_failure(tmp_path: Path, monkeypatch):
    from yomu_backend_provider import _persist_translation_execute_shape, _translation_execute_shape
    monkeypatch.setattr("yomu_backend_provider.runtime_root", lambda: tmp_path)
    payload = {"request_id": "req", "job_id": "job",
               "device_id": "550e8400-e29b-41d4-a716-446655440000",
               "reservation_id": "550e8400-e29b-41d4-a716-446655440001",
               "source_lang": "JA", "target_lang": "PT-BR",
               "items": [{"item_id": "a", "text": "x"}]}
    shape = _translation_execute_shape(payload)
    _persist_translation_execute_shape(shape)
    lines = (tmp_path / "diagnostics" / "translation_execute_request_shape.jsonl").read_text().splitlines()
    assert len(lines) == 1 and "device_id_uuid_valid" in lines[0]
    assert '"items"' not in lines[0] and "Hello" not in lines[0]
