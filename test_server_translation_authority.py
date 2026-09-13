import threading
from pathlib import Path
import pytest
from yomu_backend_provider import BatchItem, BatchRequest, BatchResponse, BackendTranslationError
from server_translation_authority import ServerTranslationAuthority


def req(rid="r1", text="Hello"):
    return BatchRequest(rid, "job-1", "JA", "PT-BR", (BatchItem("a", text), BatchItem("b", text)))


class Provider:
    def __init__(self): self.calls = 0
    def __call__(self, request, reservation):
        self.calls += 1
        return BatchResponse(request.request_id, "completed", tuple(BatchItem(i.item_id, "T:" + i.text) for i in request.items), reservation)


def test_result_persisted_before_response_and_replayed():
    p = Provider(); s = ServerTranslationAuthority(p)
    with pytest.raises(RuntimeError): s.execute(user_id="u", request=req(), reservation_id="res", crash_at="after_result")
    out = s.execute(user_id="u", request=req(), reservation_id="res")
    assert [i.item_id for i in out.items] == ["a", "b"]
    assert p.calls == 1 and s.reservation_count == 1 and s.commit_count == 1
    assert s.operation("u", "job-1", "r1")["status"] == "completed"


def test_crash_after_commit_and_payload_conflict():
    p = Provider(); s = ServerTranslationAuthority(p)
    with pytest.raises(RuntimeError): s.execute(user_id="u", request=req(), reservation_id="res", crash_at="after_commit")
    s.execute(user_id="u", request=req(), reservation_id="res")
    assert p.calls == 1 and s.commit_count == 1
    with pytest.raises(BackendTranslationError, match="IDEMPOTENCY_CONFLICT"):
        s.execute(user_id="u", request=req(text="Different"), reservation_id="res")


def test_concurrent_same_request_one_provider():
    p = Provider(); s = ServerTranslationAuthority(p); out = []
    def run(): out.append(s.execute(user_id="u", request=req(), reservation_id="res"))
    ts = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(out) == 2 and p.calls == 1 and s.reservation_count == 1
