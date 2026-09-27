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


@pytest.fixture(autouse=True)
def verified_commercial_device_uuid(monkeypatch):
    # Production workers receive this from the authenticated license_devices
    # snapshot; tests must model that boundary explicitly.
    monkeypatch.setenv("TRADUTOR_DEVICE_UUID", "550e8400-e29b-41d4-a716-446655440000")


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


def test_provider_records_only_reservation_metadata_before_translation(tmp_path: Path, monkeypatch):
    import yk_reservation
    output_dir = tmp_path / "job-output"
    monkeypatch.setenv("TRADUTOR_JOB_OUTPUT_DIR", str(output_dir))
    AuthEnvelopeStore(tmp_path).seal("job-1", tok())
    backend = MockBackendClient()
    provider = YomuBackendTranslationProvider(runtime_root=tmp_path, backend=backend)
    provider.translate_batch(request())
    saved = yk_reservation.read_reservation(output_dir)
    assert saved["job_id"] == "job-1"
    assert saved["state"] == "held"
    assert "reservation_id" in saved
    raw = (output_dir / "yk_reservation.json").read_text(encoding="utf-8")
    assert "finalize_items" not in raw and "Hello" not in raw
    assert backend.net_yk(saved["reservation_id"]) == 0


def test_missing_runner_output_dir_fails_before_reserving_yk(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TRADUTOR_JOB_OUTPUT_DIR", raising=False)
    monkeypatch.delenv("TRADUTOR_IA_HERMETIC_TEST_ENV", raising=False)
    AuthEnvelopeStore(tmp_path).seal("job-1", tok())
    backend = MockBackendClient()
    with pytest.raises(BackendTranslationError, match="job_output_dir_missing"):
        YomuBackendTranslationProvider(runtime_root=tmp_path, backend=backend).translate_batch(request())
    assert backend.reserve_count == 0


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


def test_translate_many_consumes_after_all_batches_and_local_result_are_durable(tmp_path: Path, monkeypatch):
    class FinalizationCaptureBackend(MockBackendClient):
        def __init__(self):
            super().__init__()
            self.finalize_flags = []
            self.debit_count = 0
            self.xp_count = 0

        def translate_batch(self, request, *, reservation_id, auth_token, finalize_job=True):
            self.finalize_flags.append(bool(finalize_job))
            if finalize_job:
                self.debit_count += 1
                self.xp_count += 1
            return super().translate_batch(request, reservation_id=reservation_id,
                                            auth_token=auth_token, finalize_job=finalize_job)

    job_id = "job-finalize"
    AuthEnvelopeStore(tmp_path).seal(job_id, tok())
    monkeypatch.setenv("TRADUTOR_JOB_ID", job_id)
    monkeypatch.setenv("TRADUTOR_REQUEST_ID", "translation:finalize")
    backend = FinalizationCaptureBackend()
    provider = YomuBackendTranslationProvider(runtime_root=tmp_path, backend=backend)
    provider.translate_many(["x" * 4000] * 35)
    assert backend.reserve_count == 1
    assert backend.finalize_flags == [False, False]
    assert backend.debit_count == 0  # wallet RPC, not provider transport's legacy flag
    rid = next(iter(backend._reservation_jobs))
    assert backend.net_yk(rid) == 0  # still held until translation validation is done
    provider.commit_translation_success()
    assert backend.net_yk(rid) == 1


def test_multi_batch_failure_never_consumes_chapter_reservation(tmp_path: Path, monkeypatch):
    class FailingBatchBackend(MockBackendClient):
        def __init__(self):
            super().__init__()
            self.calls = 0
            self.finalize_flags = []
            self.debit_count = 0

        def translate_batch(self, request, *, reservation_id, auth_token, finalize_job=True):
            self.calls += 1
            self.finalize_flags.append(bool(finalize_job))
            if self.calls == 2:
                raise BackendTranslationError("provider_failed")
            response = super().translate_batch(request, reservation_id=reservation_id,
                                               auth_token=auth_token, finalize_job=finalize_job)
            if finalize_job:
                self.debit_count += 1
            return response

    job_id = "job-failure"
    AuthEnvelopeStore(tmp_path).seal(job_id, tok())
    monkeypatch.setenv("TRADUTOR_JOB_ID", job_id)
    monkeypatch.setenv("TRADUTOR_REQUEST_ID", "translation:failure")
    backend = FailingBatchBackend()
    provider = YomuBackendTranslationProvider(runtime_root=tmp_path, backend=backend)
    with pytest.raises(BackendTranslationError, match="provider_failed"):
        provider.translate_many(["x" * 4000] * 35)
    assert backend.reserve_count == 1
    assert backend.finalize_flags == [False, False]
    assert backend.debit_count == 0


def test_multiple_provider_batches_consume_once_after_job_result_is_durable(tmp_path: Path, monkeypatch):
    job_id = "job-legacy-repro"
    AuthEnvelopeStore(tmp_path).seal(job_id, tok())
    monkeypatch.setenv("TRADUTOR_JOB_ID", job_id)
    monkeypatch.setenv("TRADUTOR_REQUEST_ID", "translation:legacy-repro")
    backend = MockBackendClient()
    provider = YomuBackendTranslationProvider(runtime_root=tmp_path, backend=backend)
    assert len(provider.translate_many(["x" * 4000] * 35)) == 35
    assert backend.reserve_count == 1
    assert backend.net_yk(next(iter(backend._reservation_jobs))) == 0
    provider.commit_translation_success()
    assert backend.net_yk(next(iter(backend._reservation_jobs))) == 1


def test_restart_and_ten_output_retries_reuse_local_translation_without_provider_or_yk(tmp_path: Path, monkeypatch):
    import yk_reservation
    job_id = "job-recovery"
    output = tmp_path / "job-output"
    monkeypatch.setenv("TRADUTOR_JOB_ID", job_id)
    monkeypatch.setenv("TRADUTOR_JOB_OUTPUT_DIR", str(output))
    monkeypatch.setenv("TRADUTOR_REQUEST_ID", f"translation:{job_id}")
    AuthEnvelopeStore(tmp_path).seal(job_id, tok())
    backend = MockBackendClient()

    first = YomuBackendTranslationProvider(runtime_root=tmp_path, backend=backend)
    expected = first.translate_many(["first region", "second region"])
    reservation = yk_reservation.read_reservation(output)
    assert reservation["state"] == "held"
    assert backend.net_yk(reservation["reservation_id"]) == 0
    assert backend.provider_execution_count == 1
    first.commit_translation_success()
    assert backend.net_yk(reservation["reservation_id"]) == 1

    # New provider object models app/worker restart; output retries replay durable
    # job-local translated items and do not invoke the provider or settle again.
    for _ in range(10):
        restarted = YomuBackendTranslationProvider(runtime_root=tmp_path, backend=backend)
        assert restarted.translate_many(["first region", "second region"]) == expected
    assert backend.provider_execution_count == 1
    assert backend.net_yk(reservation["reservation_id"]) == 1
    assert yk_reservation.translation_ready(output, job_id=job_id)


def test_distinct_translation_retry_is_same_job_one_yk_commit(tmp_path: Path, monkeypatch):
    job_id = "job-strict-retry"
    monkeypatch.setenv("TRADUTOR_JOB_ID", job_id)
    monkeypatch.setenv("TRADUTOR_JOB_OUTPUT_DIR", str(tmp_path / "job-output"))
    monkeypatch.setenv("TRADUTOR_REQUEST_ID", f"translation:{job_id}")
    AuthEnvelopeStore(tmp_path).seal(job_id, tok())
    backend = MockBackendClient()
    provider = YomuBackendTranslationProvider(runtime_root=tmp_path, backend=backend)
    assert provider.translate_many(["first wording"])
    assert provider.translate_many(["revised wording"])
    assert backend.provider_execution_count == 2
    assert backend.reserve_count == 1
    provider.commit_translation_success()
    provider.commit_translation_success()
    reservation = next(iter(backend._reservation_jobs))
    assert backend.net_yk(reservation) == 1


def test_local_result_persist_failure_does_not_commit_yk(tmp_path: Path, monkeypatch):
    job_id = "job-local-persist-fail"
    output = tmp_path / "job-output"
    monkeypatch.setenv("TRADUTOR_JOB_ID", job_id)
    monkeypatch.setenv("TRADUTOR_JOB_OUTPUT_DIR", str(output))
    monkeypatch.setenv("TRADUTOR_REQUEST_ID", f"translation:{job_id}")
    AuthEnvelopeStore(tmp_path).seal(job_id, tok())
    backend = MockBackendClient()

    def fail_persist(*_args, **_kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr("yomu_backend_provider.ResultStore.persist_for_job", fail_persist)
    provider = YomuBackendTranslationProvider(runtime_root=tmp_path, backend=backend)
    with pytest.raises(OSError, match="disk unavailable"):
        provider.translate_many(["source text"])
    reservation = next(iter(backend._reservation_jobs))
    assert backend.net_yk(reservation) == 0
    assert not (output / "translation_ready.json").exists()


def test_crash_after_remote_commit_retries_idempotently_without_provider_replay(tmp_path: Path, monkeypatch):
    job_id = "job-crash-after-commit"
    output = tmp_path / "job-output"
    monkeypatch.setenv("TRADUTOR_JOB_ID", job_id)
    monkeypatch.setenv("TRADUTOR_JOB_OUTPUT_DIR", str(output))
    monkeypatch.setenv("TRADUTOR_REQUEST_ID", f"translation:{job_id}")
    AuthEnvelopeStore(tmp_path).seal(job_id, tok())
    backend = MockBackendClient()
    provider = YomuBackendTranslationProvider(runtime_root=tmp_path, backend=backend)
    expected = provider.translate_many(["same source"])
    original = backend.settle_job
    lost_response = {"value": True}

    def commit_then_lose_response(**kwargs):
        result = original(**kwargs)
        if lost_response["value"]:
            lost_response["value"] = False
            raise BackendTranslationError("backend_transport_failed")
        return result

    monkeypatch.setattr(backend, "settle_job", commit_then_lose_response)
    with pytest.raises(BackendTranslationError, match="wallet_settlement_pending"):
        provider.commit_translation_success()
    restarted = YomuBackendTranslationProvider(runtime_root=tmp_path, backend=backend)
    assert restarted.translate_many(["same source"]) == expected
    restarted.commit_translation_success()
    assert backend.provider_execution_count == 1
    assert backend.net_yk(next(iter(backend._reservation_jobs))) == 1


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


@pytest.mark.parametrize("value", ["", None])
def test_missing_commercial_device_id_fails_closed_before_remote(tmp_path: Path, monkeypatch, value):
    class FailingBackend(MockBackendClient):
        def reserve(self, **kwargs):
            raise AssertionError("remote reserve must not be reached")

    if value is None:
        monkeypatch.delenv("TRADUTOR_DEVICE_UUID", raising=False)
    else:
        monkeypatch.setenv("TRADUTOR_DEVICE_UUID", value)
    AuthEnvelopeStore(tmp_path).seal("job-1", tok())
    with pytest.raises(BackendTranslationError, match="commercial_device_id_missing"):
        YomuBackendTranslationProvider(runtime_root=tmp_path, backend=FailingBackend()).translate_batch(request())


def test_translation_execute_contract_carries_device_and_reservation(tmp_path: Path, monkeypatch):
    class CaptureBackend(MockBackendClient):
        def translate_batch(self, request, *, reservation_id, auth_token, finalize_job=True):
            self.captured = (request, reservation_id, finalize_job)
            return super().translate_batch(request, reservation_id=reservation_id,
                                            auth_token=auth_token, finalize_job=finalize_job)

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
    assert payload["finalize_job"] is False
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
