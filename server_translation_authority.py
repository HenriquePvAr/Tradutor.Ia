"""Local server-authority simulator for batch translation recovery tests."""
from __future__ import annotations
import hashlib, json, sqlite3, threading
from dataclasses import asdict
from typing import Any, Callable
from yomu_backend_provider import BatchRequest, BatchResponse, BackendTranslationError


def canonical_hash(request: BatchRequest) -> str:
    payload = {"source_lang": request.source_lang, "target_lang": request.target_lang,
               "items": [{"item_id": i.item_id, "text": i.text} for i in request.items]}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class ServerTranslationAuthority:
    def __init__(self, provider: Callable[[BatchRequest, str], BatchResponse]):
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.executescript("""
          create table operations(user_id text, job_id text, request_id text, request_hash text,
            reservation_id text, status text not null, result_json text, primary key(user_id,job_id,request_id));
        """)
        self.provider = provider
        self.lock = threading.RLock()
        self.commit_count = 0
        self.reservation_count = 0

    def execute(self, *, user_id: str, request: BatchRequest, reservation_id: str, crash_at: str | None = None) -> BatchResponse:
        h = canonical_hash(request)
        key = (user_id, request.job_id, request.request_id)
        with self.lock:
            row = self.db.execute("select request_hash,status,result_json,reservation_id from operations where user_id=? and job_id=? and request_id=?", key).fetchone()
            if row:
                if row[0] != h: raise BackendTranslationError("IDEMPOTENCY_CONFLICT")
                if row[2]:
                    data = json.loads(row[2])
                    if row[1] != "completed":
                        self.db.execute("update operations set status='completed' where user_id=? and job_id=? and request_id=?", key)
                        self.db.commit(); self.commit_count += 1
                    return BatchResponse(data["request_id"], data["status"], tuple(__import__('yomu_backend_provider').BatchItem(**i) for i in data["items"]), data["reservation_id"])
                reservation_id = row[3]
            else:
                self.db.execute("insert into operations values(?,?,?,?,?,?,?)", (*key, h, reservation_id, "provider_pending", None))
                self.db.commit(); self.reservation_count += 1
            if crash_at == "before_provider": raise RuntimeError("simulated_crash")
        # Keep the operation claim serialized through provider execution in
        # this local authority simulator. This models the server-side
        # provider_pending lease and prevents duplicate execution races.
        with self.lock:
            response = self.provider(request, reservation_id)
            payload = {"request_id": response.request_id, "status": response.status,
                       "items": [asdict(i) for i in response.items], "reservation_id": response.reservation_id}
            self.db.execute("update operations set result_json=?,status='provider_succeeded' where user_id=? and job_id=? and request_id=?", (json.dumps(payload, ensure_ascii=False), *key)); self.db.commit()
            if crash_at == "after_result": raise RuntimeError("simulated_crash")
            self.db.execute("update operations set status='completed' where user_id=? and job_id=? and request_id=?", key); self.db.commit(); self.commit_count += 1
            if crash_at == "after_commit": raise RuntimeError("simulated_crash")
            return response

    def operation(self, user_id: str, job_id: str, request_id: str) -> dict[str, Any] | None:
        row = self.db.execute("select request_hash,status,result_json,reservation_id from operations where user_id=? and job_id=? and request_id=?", (user_id, job_id, request_id)).fetchone()
        return None if not row else {"request_hash": row[0], "status": row[1], "result": json.loads(row[2]) if row[2] else None, "reservation_id": row[3]}
