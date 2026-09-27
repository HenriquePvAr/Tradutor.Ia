"""Local contract for the server-side Yomu translation provider.

The default implementation is deliberately injectable/mocked in this phase.  It
keeps auth, reservation and result identity separate from provider transport so a
future Edge Function deployment can adopt the same contract without DeepL locally.
"""
from __future__ import annotations

import threading
import uuid
import hashlib
import os
import json
import time
import tempfile
from pathlib import Path
from enum import Enum
from urllib.parse import urljoin
from dataclasses import dataclass
from typing import Any, Protocol

from secure_auth_context import AuthContextError, AuthEnvelopeStore
from runtime_paths import runtime_root


class BackendTranslationError(RuntimeError):
    pass


class ReservationOutcomeClass(str, Enum):
    """Safety classification used before any reservation-release decision."""

    PRE_PROVIDER_DEFINITE_FAILURE = "pre_provider_definite_failure"
    PROVIDER_NOT_STARTED_UNCERTAIN = "provider_not_started_uncertain"
    PROVIDER_STARTED_OR_OUTCOME_UNKNOWN = "provider_started_or_outcome_unknown"


_PRE_PROVIDER_FAILURE_CODES = frozenset({
    "invalid_translation_request", "invalid_license_device_uuid", "invalid_batch_request",
    "invalid_batch_item", "duplicate_item_id", "batch_limits_exceeded",
    "request_validation_failed", "begin_translation_request_failed",
})


def classify_reservation_outcome(error_code: str, *, provider_started: bool = False,
                                 outcome_unknown: bool = False) -> ReservationOutcomeClass:
    """Classify a failed translation without performing release/finalize side effects."""
    if provider_started or outcome_unknown:
        return ReservationOutcomeClass.PROVIDER_STARTED_OR_OUTCOME_UNKNOWN
    code = str(error_code or "").strip().lower()
    if code in _PRE_PROVIDER_FAILURE_CODES:
        return ReservationOutcomeClass.PRE_PROVIDER_DEFINITE_FAILURE
    return ReservationOutcomeClass.PROVIDER_NOT_STARTED_UNCERTAIN


@dataclass(frozen=True)
class BatchItem:
    item_id: str
    text: str


@dataclass(frozen=True)
class BatchRequest:
    request_id: str
    job_id: str
    source_lang: str
    target_lang: str
    items: tuple[BatchItem, ...]

    def __post_init__(self):
        if not self.request_id or not self.job_id or not self.items:
            raise BackendTranslationError("invalid_batch_request")
        ids = [item.item_id for item in self.items]
        if any(not item.item_id or not item.text.strip() for item in self.items):
            raise BackendTranslationError("invalid_batch_item")
        if len(ids) != len(set(ids)):
            raise BackendTranslationError("duplicate_item_id")
        if len(self.items) > 100 or sum(len(i.text) for i in self.items) > 100_000:
            raise BackendTranslationError("batch_limits_exceeded")


MAX_BATCH_ITEMS = 100
MAX_BATCH_CHARS = 100_000


def _validated_device_uuid() -> str:
    """Return the verified license_devices row UUID for wallet RPC calls."""
    value = str(os.getenv("TRADUTOR_DEVICE_UUID", "") or "").strip()
    if not value:
        # The wallet RPC requires the UUID of the authenticated
        # ``license_devices`` row.  An empty value used to cross the HTTP
        # boundary and become PostgreSQL SQLSTATE 22P02/``invalid_request``.
        raise BackendTranslationError("commercial_device_id_missing")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise BackendTranslationError("invalid_license_device_uuid") from exc
    return str(parsed)


def _validated_request_uuid(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise BackendTranslationError(f"invalid_translation_request.{field}_missing")
    try:
        return str(uuid.UUID(text))
    except (ValueError, AttributeError, TypeError) as exc:
        raise BackendTranslationError(f"invalid_translation_request.{field}_invalid_uuid") from exc


def _translation_execute_shape(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and return a secret-free request shape before HTTP transmission."""
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id:
        raise BackendTranslationError("invalid_translation_request.request_id_missing")
    device_id = _validated_request_uuid(payload.get("device_id"), "device_id")
    reservation_id = _validated_request_uuid(payload.get("reservation_id"), "reservation_id")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise BackendTranslationError("invalid_translation_request.items_empty")
    if len(items) > MAX_BATCH_ITEMS:
        raise BackendTranslationError("invalid_translation_request.items_too_many")
    ids = [str(item.get("item_id") or "") for item in items if isinstance(item, dict)]
    texts = [str(item.get("text") or "") for item in items if isinstance(item, dict)]
    if len(ids) != len(items):
        raise BackendTranslationError("invalid_translation_request.item_invalid")
    if any(not item_id for item_id in ids):
        raise BackendTranslationError("invalid_translation_request.item_id_empty")
    if len(ids) != len(set(ids)):
        raise BackendTranslationError("invalid_translation_request.item_id_duplicate")
    if any(not text.strip() for text in texts):
        raise BackendTranslationError("invalid_translation_request.text_empty")
    if any(len(text) > MAX_BATCH_CHARS for text in texts):
        raise BackendTranslationError("invalid_translation_request.item_too_large")
    total_chars = sum(len(text) for text in texts)
    if total_chars > MAX_BATCH_CHARS:
        raise BackendTranslationError("invalid_translation_request.total_too_large")
    return {
        "event": "TRANSLATION_EXECUTE_REQUEST_SHAPE",
        "request_id_present": True,
        "request_id_length": len(request_id),
        "job_id_present": bool(str(payload.get("job_id") or "").strip()),
        "device_id_present": True,
        "device_id_uuid_valid": bool(device_id),
        "reservation_id_present": True,
        "reservation_id_uuid_valid": bool(reservation_id),
        "items_count": len(items),
        "unique_item_ids_count": len(set(ids)),
        "empty_item_ids": sum(not item_id for item_id in ids),
        "empty_text_items": sum(not text.strip() for text in texts),
        "max_item_length": max(map(len, texts), default=0),
        "total_chars": total_chars,
        "source_lang_present": bool(str(payload.get("source_lang") or "").strip()),
        "target_lang_present": bool(str(payload.get("target_lang") or "").strip()),
        "timestamp": time.time(),
    }


def _persist_translation_execute_shape(shape: dict[str, Any]) -> None:
    try:
        path = runtime_root() / "diagnostics" / "translation_execute_request_shape.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(shape, separators=(",", ":")) + "\n")
            handle.flush()
    except (OSError, TypeError, ValueError):
        pass


@dataclass(frozen=True)
class BatchResponse:
    request_id: str
    status: str
    items: tuple[BatchItem, ...]
    reservation_id: str


class BackendClient(Protocol):
    def reserve(self, *, job_id: str, request_id: str, auth_token: str, device_id: str = "") -> dict[str, Any]: ...
    def translate_batch(self, request: BatchRequest, *, reservation_id: str, auth_token: str,
                        finalize_job: bool = False) -> BatchResponse: ...
    def release(self, *, reservation_id: str, auth_token: str) -> dict[str, Any]: ...
    def settle_job(self, *, job_id: str, reservation_id: str, action: str,
                   idempotency_key: str, auth_token: str) -> dict[str, Any]: ...


class HttpBackendClient:
    """Production transport boundary for the deployed Yomu Edge Functions.

    The transport is injectable for tests; the provider never falls back to a
    desktop/LLM translator when this client is unavailable.
    """
    def __init__(self, base_url: str, *, transport=None):
        base = str(base_url or "").strip().rstrip("/")
        if not base or not base.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise BackendTranslationError("backend_url_invalid")
        self.base_url = base
        self.transport = transport

    @classmethod
    def from_environment(cls):
        base = os.getenv("YOMU_BACKEND_URL") or os.getenv("SUPABASE_URL")
        if not base:
            raise BackendTranslationError("backend_url_missing")
        return cls(base, transport=RequestsBackendTransport.from_environment())

    def _call(self, path: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
        if self.transport is None:
            raise BackendTranslationError("backend_transport_not_configured")
        try:
            return self.transport(path, payload, token)
        except BackendTranslationError:
            raise
        except Exception as exc:
            raise BackendTranslationError("backend_transport_failed") from exc

    def reserve(self, *, job_id: str, request_id: str, auth_token: str, device_id: str = "") -> dict[str, Any]:
        return self._call("/functions/v1/wallet-reserve", {
            "job_id": job_id, "request_id": request_id, "operation": "translation",
            "device_id": str(device_id or ""),
        }, auth_token)

    def release(self, *, reservation_id: str, auth_token: str) -> dict[str, Any]:
        # Client-callable RELEASE of a still-held reservation (wallet-finalize ->
        # release_yk_reservation). Idempotent server-side; a reservation that was
        # never consumed leaves zero net YK.  Consume stays server-side.
        return self._call("/functions/v1/wallet-finalize", {
            "reservation_id": str(reservation_id or ""),
        }, auth_token)

    def settle_job(self, *, job_id: str, reservation_id: str, action: str,
                   idempotency_key: str, auth_token: str) -> dict[str, Any]:
        if action not in {"consume", "release"}:
            raise BackendTranslationError("settlement_action_invalid")
        return self._call("/functions/v1/wallet-settle-job", {
            "job_id": str(job_id), "reservation_id": str(reservation_id),
            "action": action, "idempotency_key": str(idempotency_key),
        }, auth_token)

    def translate_batch(self, request: BatchRequest, *, reservation_id: str, auth_token: str,
                        finalize_job: bool = False) -> BatchResponse:
        if finalize_job:
            raise BackendTranslationError("wallet_settlement_required")
        device_id = _validated_device_uuid()
        payload = {
            "request_id": request.request_id, "job_id": request.job_id,
            "source_lang": request.source_lang, "target_lang": request.target_lang,
            "reservation_id": reservation_id,
            "finalize_job": bool(finalize_job),
            "device_id": device_id,
            "items": [{"item_id": i.item_id, "text": i.text} for i in request.items],
        }
        shape = _translation_execute_shape(payload)
        _persist_translation_execute_shape(shape)
        print("TRANSLATION_EXECUTE_REQUEST_SHAPE " + json.dumps({
            "request_id_present": bool(payload["request_id"]),
            "device_id_present": bool(payload["device_id"]),
            "device_id_uuid_valid": bool(device_id),
            "reservation_id_present": bool(payload["reservation_id"]),
            "job_id_present": bool(payload["job_id"]),
            "items_count": len(payload["items"]),
            "unique_item_ids": len({item["item_id"] for item in payload["items"]}),
            "empty_item_ids": sum(not item["item_id"] for item in payload["items"]),
            "empty_text_items": sum(not item["text"].strip() for item in payload["items"]),
            "total_chars": sum(len(item["text"]) for item in payload["items"]),
        }, separators=(",", ":")), flush=True)
        print("TRANSLATION_EXECUTE_HTTP_REQUEST_STARTED", flush=True)
        try:
            raw = self._call("/functions/v1/translation-execute", payload, auth_token)
        except BackendTranslationError as exc:
            print(f"TRANSLATION_EXECUTE_RESPONSE_CODE code={str(exc)[:80]}", flush=True)
            raise
        items = tuple(BatchItem(str(i.get("item_id") or ""), str(i.get("text") or i.get("translated_text") or ""))
                      for i in (raw.get("items") or []))
        return BatchResponse(str(raw.get("request_id") or request.request_id),
                             str(raw.get("status") or ""), items,
                             str(raw.get("reservation_id") or reservation_id))


class RequestsBackendTransport:
    """Authenticated HTTP transport reconstructed inside the frozen child."""

    def __init__(self, base_url: str, *, publishable_key: str = "", timeout: float = 30.0):
        self.base_url = str(base_url or "").rstrip("/")
        self.publishable_key = str(publishable_key or "").strip()
        self.timeout = float(timeout)
        if not self.base_url:
            raise BackendTranslationError("backend_url_missing")
        import requests
        self._session = requests.Session()
        self._session.trust_env = False
        self.last_duration_ms = 0

    @classmethod
    def from_environment(cls):
        base = os.getenv("YOMU_BACKEND_URL") or os.getenv("SUPABASE_URL")
        if not base:
            raise BackendTranslationError("backend_url_missing")
        key = os.getenv("SUPABASE_PUBLISHABLE_KEY") or os.getenv("SUPABASE_ANON_KEY") or ""
        return cls(base, publishable_key=key)

    def __call__(self, path: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
        if not token:
            raise BackendTranslationError("AUTH_REQUIRED")
        import requests
        url = urljoin(self.base_url + "/", str(path).lstrip("/"))
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   "Authorization": f"Bearer {token}"}
        if self.publishable_key:
            headers["apikey"] = self.publishable_key
        started = time.perf_counter()
        try:
            response = self._session.post(url, headers=headers, json=payload,
                                          timeout=self.timeout)
            status = int(response.status_code)
            try:
                body = response.json()
            except (ValueError, json.JSONDecodeError):
                body = {}
        except requests.RequestException as exc:
            raise BackendTranslationError("backend_transport_failed") from exc
        finally:
            self.last_duration_ms = int((time.perf_counter() - started) * 1000)
        if status >= 400:
            code = body.get("code") if isinstance(body, dict) else None
            raise BackendTranslationError(str(code or f"backend_http_{status}"))
        if not isinstance(body, dict):
            raise BackendTranslationError("backend_response_invalid")
        return body


class ResultStore:
    """Crash-safe, job-local translation cache; never stores auth material."""
    def __init__(self, root: str | os.PathLike | None = None):
        self._lock = threading.RLock()
        self._records: dict[tuple[str, str], BatchResponse] = {}
        self._states: dict[tuple[str, str], str] = {}
        self._input_hashes: dict[tuple[str, str], str] = {}
        self._roots: dict[str, Path] = {}
        self._default_root = Path(root).resolve() if root else None

    def configure_job_root(self, job_id: str, root: str | os.PathLike) -> None:
        candidate = Path(root).resolve() / ".translation_results"
        with self._lock:
            existing = self._roots.get(job_id)
            if existing is not None and existing != candidate:
                raise BackendTranslationError("translation_result_store_job_conflict")
            self._roots[job_id] = candidate

    def _path(self, job_id: str, request_id: str) -> Path | None:
        root = self._roots.get(job_id, self._default_root)
        if root is None:
            return None
        key = hashlib.sha256(f"{job_id}\0{request_id}".encode("utf-8")).hexdigest()
        return root / f"{key}.json"

    def durable_evidence(self, job_id: str, request_ids: list[str]) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        with self._lock:
            for request_id in request_ids:
                path = self._path(job_id, request_id)
                if path is None or not path.is_file():
                    raise BackendTranslationError("translation_result_not_persisted")
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if (payload.get("schema_version") != 1 or payload.get("job_id") != job_id
                            or payload.get("request_id") != request_id
                            or not isinstance(payload.get("items"), list)
                            or not payload["items"]):
                        raise ValueError("invalid_result_cache")
                    rows.append({
                        "request_id": request_id,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "item_count": str(len(payload["items"])),
                    })
                except (OSError, ValueError, TypeError, AttributeError) as exc:
                    raise BackendTranslationError("translation_result_not_persisted") from exc
        return rows

    @staticmethod
    def _input_hash(request: BatchRequest) -> str:
        value = {
            "job_id": request.job_id, "request_id": request.request_id,
            "source_lang": request.source_lang, "target_lang": request.target_lang,
            "items": [{"item_id": item.item_id, "text": item.text} for item in request.items],
        }
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def get(self, job_id: str, request_id: str,
            expected_request: BatchRequest | None = None) -> BatchResponse | None:
        with self._lock:
            cached = self._records.get((job_id, request_id))
            if cached is not None:
                if (expected_request is not None
                        and self._input_hashes.get((job_id, request_id)) != self._input_hash(expected_request)):
                    return None
                return cached
            path = self._path(job_id, request_id)
            if path is None:
                return None
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if (payload.get("schema_version") != 1 or payload.get("job_id") != job_id
                        or payload.get("request_id") != request_id
                        or expected_request is not None
                        and payload.get("input_sha256") != self._input_hash(expected_request)
                        or not isinstance(payload.get("items"), list)):
                    return None
                response = BatchResponse(
                    request_id=request_id,
                    status="completed",
                    reservation_id=str(payload.get("reservation_id") or ""),
                    items=tuple(BatchItem(str(item["item_id"]), str(item["text"]))
                                for item in payload["items"]
                                if isinstance(item, dict) and item.get("item_id")
                                and isinstance(item.get("text"), str)),
                )
                if not response.reservation_id or len(response.items) != len(payload["items"]):
                    return None
                self._records[(job_id, request_id)] = response
                self._states[(job_id, request_id)] = "completed"
                self._input_hashes[(job_id, request_id)] = str(payload.get("input_sha256") or "")
                return response
            except (OSError, ValueError, TypeError, KeyError):
                return None

    def begin(self, job_id: str, request_id: str) -> str:
        with self._lock:
            key = (job_id, request_id)
            if key in self._records:
                return "completed"
            if self._states.get(key) == "provider_pending":
                return "provider_pending"
            self._states[key] = "provider_pending"
            return "created"

    def persist_for_job(self, job_id: str, response: BatchResponse,
                        request: BatchRequest | None = None) -> None:
        with self._lock:
            path = self._path(job_id, response.request_id)
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                payload = {
                    "schema_version": 1,
                    "job_id": job_id,
                    "request_id": response.request_id,
                    "reservation_id": response.reservation_id,
                    "input_sha256": self._input_hash(request) if request else "",
                    "items": [{"item_id": item.item_id, "text": item.text}
                              for item in response.items],
                }
                fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".result_", suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            key = (job_id, response.request_id)
            self._records[key] = response
            self._states[key] = "completed"
            if request is not None:
                self._input_hashes[key] = self._input_hash(request)

    def state(self, job_id: str, request_id: str) -> str:
        with self._lock:
            return self._states.get((job_id, request_id), "request_created")


class MockBackendClient:
    def __init__(self, *, fail: str | None = None, chunk_size: int = 100):
        self.fail = fail
        self.chunk_size = chunk_size
        self.reserve_count = 0
        self.provider_execution_count = 0
        self.provider_chunk_count = 0
        self._lock = threading.Lock()
        self._reservations: dict[tuple[str, str], str] = {}
        self._responses: dict[str, BatchResponse] = {}
        self._wallet_state: dict[str, str] = {}
        self._reservation_jobs: dict[str, str] = {}
        self._reservation_owners: dict[str, str] = {}
        self._provider_requests: dict[str, dict[str, str]] = {}
        self._settlement_keys: dict[tuple[str, str], tuple[str, str]] = {}

    def net_yk(self, reservation_id: str) -> int:
        return int(self._wallet_state.get(reservation_id) == "consumed")

    def release(self, *, reservation_id: str, auth_token: str) -> dict[str, Any]:
        raise BackendTranslationError("settlement_job_context_required")

    def settle_job(self, *, job_id: str, reservation_id: str, action: str,
                   idempotency_key: str, auth_token: str) -> dict[str, Any]:
        if not auth_token:
            raise BackendTranslationError("AUTH_REQUIRED")
        if action not in {"consume", "release"} or not idempotency_key:
            raise BackendTranslationError("settlement_request_invalid")
        rid, jid, key = str(reservation_id), str(job_id), str(idempotency_key)
        with self._lock:
            if self._reservation_jobs.get(rid) != jid:
                raise BackendTranslationError("reservation_job_mismatch")
            if self._reservation_owners.get(rid) != auth_token:
                raise BackendTranslationError("reservation_not_found")
            prior = self._settlement_keys.get((rid, key))
            if prior:
                if prior[0] != action:
                    raise BackendTranslationError("idempotency_conflict")
                return {"job_id": jid, "reservation_id": rid,
                        "status": prior[1], "idempotent": True}
            state = self._wallet_state.get(rid, "held")
            terminal = "consumed" if action == "consume" else "released"
            if state != "held":
                if state == terminal:
                    self._settlement_keys[(rid, key)] = (action, state)
                    return {"job_id": jid, "reservation_id": rid,
                            "status": state, "idempotent": True}
                raise BackendTranslationError("reservation_terminal_conflict")
            requests = self._provider_requests.get(rid, {})
            if action == "consume" and (not requests or any(status != "completed" for status in requests.values())):
                raise BackendTranslationError("translation_job_not_complete")
            if action == "release" and (
                any(status == "processing" for status in requests.values())
                or bool(requests) and all(status == "completed" for status in requests.values())
            ):
                raise BackendTranslationError("provider_success_cannot_be_released")
            self._wallet_state[rid] = terminal
            self._settlement_keys[(rid, key)] = (action, terminal)
            return {"job_id": jid, "reservation_id": rid,
                    "status": terminal, "idempotent": False}

    def reserve(self, *, job_id: str, request_id: str, auth_token: str, device_id: str = "") -> dict[str, Any]:
        if not auth_token:
            raise BackendTranslationError("AUTH_REQUIRED")
        if self.fail == "reserve":
            raise BackendTranslationError("RESERVATION_FAILED")
        with self._lock:
            key = (job_id, request_id)
            reservation = self._reservations.get(key)
            if reservation is None:
                reservation = "res_" + uuid.uuid4().hex
                self._reservations[key] = reservation
                self._wallet_state[reservation] = "held"
                self._reservation_jobs[reservation] = str(job_id)
                self._reservation_owners[reservation] = str(auth_token)
                self.reserve_count += 1
            return {"reservation_id": reservation, "required_yk": 1}

    def translate_batch(self, request: BatchRequest, *, reservation_id: str, auth_token: str,
                        finalize_job: bool = False) -> BatchResponse:
        with self._lock:
            if finalize_job:
                raise BackendTranslationError("wallet_settlement_required")
            if self._wallet_state.get(reservation_id) != "held":
                raise BackendTranslationError("reservation_not_held")
            if self._reservation_jobs.get(reservation_id) != request.job_id:
                raise BackendTranslationError("reservation_job_mismatch")
            if self._reservation_owners.get(reservation_id) != auth_token:
                raise BackendTranslationError("reservation_not_found")
            requests = self._provider_requests.setdefault(reservation_id, {})
            requests[request.request_id] = "processing"
            if self.fail:
                requests[request.request_id] = "failed"
                raise BackendTranslationError(self.fail)
            if request.request_id in self._responses:
                requests[request.request_id] = "completed"
                return self._responses[request.request_id]
            self.provider_execution_count += 1
            chunks = [request.items[i:i + self.chunk_size] for i in range(0, len(request.items), self.chunk_size)]
            self.provider_chunk_count += len(chunks)
            response = BatchResponse(request.request_id, "completed", tuple(
                BatchItem(item.item_id, f"[{request.target_lang}] {item.text}") for item in request.items), reservation_id)
            self._responses[request.request_id] = response
            requests[request.request_id] = "completed"
            return response


class YomuBackendTranslationProvider:
    def __init__(self, *, runtime_root, backend: BackendClient, result_store: ResultStore | None = None):
        self.auth = AuthEnvelopeStore(runtime_root)
        self.backend = backend
        self.results = result_store or ResultStore()
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self.provider_name = "yomu_backend"
        self.stats: dict[str, Any] = {"provider_name": self.provider_name}

    def _lock_for(self, key):
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def translate_batch(self, request: BatchRequest) -> BatchResponse:
        output_dir = self._reservation_output_dir(request.job_id)
        self.results.configure_job_root(request.job_id, output_dir)
        existing = self.results.get(request.job_id, request.request_id, request)
        if existing:
            return existing
        with self._lock_for((request.job_id, request.request_id)):
            existing = self.results.get(request.job_id, request.request_id, request)
            if existing:
                return existing
            try:
                auth_job_id = str(os.getenv("TRADUTOR_AUTH_CONTEXT_ID") or request.job_id).strip()
                context = self.auth.acquire(auth_job_id)
            except AuthContextError as exc:
                raise BackendTranslationError(str(exc)) from exc
            self.results.begin(request.job_id, request.request_id)
            device_id = _validated_device_uuid()
            print(
                "WALLET_RESERVE_REQUEST_SHAPE "
                f"job_id={request.job_id} device_id_present=YES device_id_uuid_valid=YES",
                flush=True,
            )
            reservation = self.backend.reserve(job_id=request.job_id, request_id=request.request_id, auth_token=context.access_token, device_id=device_id)
            reservation_id = str(reservation.get("reservation_id") or "")
            if not reservation_id:
                raise BackendTranslationError("RESERVATION_FAILED")
            self._record_reservation(request.job_id, reservation_id, request_id=request.request_id)
            response = self.backend.translate_batch(request, reservation_id=reservation_id,
                                                    auth_token=context.access_token,
                                                    finalize_job=False)
            expected = {item.item_id for item in request.items}
            actual = [item.item_id for item in response.items]
            if response.request_id != request.request_id or set(actual) != expected or len(actual) != len(set(actual)):
                raise BackendTranslationError("TRANSLATION_RESULT_MISMATCH")
            self.results.persist_for_job(request.job_id, response, request)
            return response

    def translate_many(self, texts, *, force: bool = False):
        """Pipeline adapter preserving positional item identity and one request id.

        The worker supplies a stable job id through the process environment.  No
        credential is carried in argv/config; the provider acquires the encrypted
        job envelope immediately before the server-side call.
        """
        texts = list(texts or [])
        if not texts:
            return []
        runtime_job_id = str(os.getenv("TRADUTOR_JOB_ID") or "").strip()
        if not runtime_job_id:
            raise BackendTranslationError("job_id_required")
        output_dir = self._reservation_output_dir(runtime_job_id)
        try:
            import yk_reservation
            prior_reservation = yk_reservation.read_reservation(output_dir) or {}
        except Exception:
            prior_reservation = {}
        job_id = str(prior_reservation.get("job_id") or runtime_job_id)
        request_id = str(
            prior_reservation.get("request_id")
            or os.getenv("TRADUTOR_REQUEST_ID")
            or f"translation:{job_id}"
        ).strip()
        self.results.configure_job_root(job_id, output_dir)
        items = tuple(BatchItem(f"translation:{index:05d}", str(text or ""))
                      for index, text in enumerate(texts))
        chunks: list[tuple[BatchItem, ...]] = []
        current: list[BatchItem] = []
        current_chars = 0
        for item in items:
            item_chars = len(item.text)
            if current and (len(current) >= MAX_BATCH_ITEMS
                            or current_chars + item_chars > MAX_BATCH_CHARS):
                chunks.append(tuple(current))
                current, current_chars = [], 0
            current.append(item)
            current_chars += item_chars
        if current:
            chunks.append(tuple(current))

        self.stats.update({
            "translation_batch_plan": {
                "total_items": len(items),
                "total_characters": sum(len(item.text) for item in items),
                "batch_count": len(chunks),
                "max_items_per_batch": MAX_BATCH_ITEMS,
                "max_characters_per_batch": MAX_BATCH_CHARS,
                "batches": [
                    {"batch_index": index, "item_count": len(chunk),
                     "character_count": sum(len(item.text) for item in chunk)}
                    for index, chunk in enumerate(chunks, start=1)
                ],
            }
        })
        print(
            "TRANSLATION_BATCH_PLAN "
            f"total_items={len(items)} total_characters={sum(len(item.text) for item in items)} "
            f"batch_count={len(chunks)} max_items={MAX_BATCH_ITEMS} max_characters={MAX_BATCH_CHARS}",
            flush=True,
        )

        try:
            context = self.auth.acquire(job_id)
        except AuthContextError as exc:
            raise BackendTranslationError(str(exc)) from exc
        device_id = _validated_device_uuid()
        print(
            "WALLET_RESERVE_REQUEST_SHAPE "
            f"job_id={job_id} device_id_present=YES device_id_uuid_valid=YES",
            flush=True,
        )
        reservation = self.backend.reserve(job_id=job_id, request_id=request_id,
                                           auth_token=context.access_token,
                                           device_id=device_id)
        reservation_id = str(reservation.get("reservation_id") or "")
        if not reservation_id:
            raise BackendTranslationError("RESERVATION_FAILED")
        self._record_reservation(job_id, reservation_id, request_id=request_id)
        # Each distinct translation batch gets a content-derived stable request
        # identity. That lets retries replay exact inputs while strict retries in
        # the same logical job remain separate provider requests under one YK.
        responses = []
        request_ids = []
        for index, chunk in enumerate(chunks, start=1):
            request_hash = ResultStore._input_hash(
                BatchRequest(request_id, job_id, "EN", "PT-BR", chunk))
            suffix = f":result:{request_hash[:32]}"
            if len(chunks) > 1:
                suffix += f":batch:{index:04d}"
            sub_request_id = f"{request_id[:220-len(suffix)]}{suffix}"
            sub_request = BatchRequest(sub_request_id, job_id, "EN", "PT-BR", chunk)
            request_ids.append(sub_request_id)
            responses.append(self._translate_batch_with_reservation(
                sub_request, reservation_id=reservation_id,
                auth_token=context.access_token, finalize_job=False))
        self.stats.update({"request_id": request_id, "job_id": job_id,
                           "translation_results": len(items),
                           "translation_batches": len(responses)})
        by_id = {item.item_id: item.text for response in responses for item in response.items}
        if set(by_id) == {item.item_id for item in items} and len(by_id) == len(items):
            try:
                import yk_reservation
                yk_reservation.mark_translation_ready(
                    output_dir, job_id=job_id, request_ids=request_ids,
                    item_count=len(items), result_files=self.results.durable_evidence(job_id, request_ids),
                )
            except Exception as exc:  # noqa: BLE001 - never bill without durable local recovery data
                raise BackendTranslationError("translation_result_persist_failed") from exc
        return [by_id[item.item_id] for item in items]

    def commit_translation_success(self) -> dict[str, Any]:
        """Consume once after all provider/retry results are durable, before rendering."""
        runtime_job_id = str(os.getenv("TRADUTOR_JOB_ID") or "").strip()
        if not runtime_job_id:
            raise BackendTranslationError("job_id_required")
        output_dir = self._reservation_output_dir(runtime_job_id)
        try:
            import yk_reservation
            ctx = yk_reservation.read_reservation(output_dir)
            if not ctx:
                return {"state": "no_reservation", "net_yk": 0}
            result = yk_reservation.settle(output_dir, output_valid=True, provider=self)
        except Exception as exc:  # noqa: BLE001 - durable results allow safe idempotent recovery
            raise BackendTranslationError("wallet_settlement_pending") from exc
        if result.get("state") != "consumed":
            raise BackendTranslationError("wallet_settlement_not_consumed")
        return result

    def _translate_batch_with_reservation(self, request: BatchRequest, *, reservation_id: str,
                                          auth_token: str, finalize_job: bool = False) -> BatchResponse:
        self.results.configure_job_root(request.job_id, self._reservation_output_dir(request.job_id))
        existing = self.results.get(request.job_id, request.request_id, request)
        if existing:
            return existing
        with self._lock_for((request.job_id, request.request_id)):
            existing = self.results.get(request.job_id, request.request_id, request)
            if existing:
                return existing
            response = self.backend.translate_batch(
                request, reservation_id=reservation_id, auth_token=auth_token,
                finalize_job=finalize_job)
            expected = {item.item_id for item in request.items}
            actual = [item.item_id for item in response.items]
            if (response.request_id != request.request_id or set(actual) != expected
                    or len(actual) != len(set(actual))):
                raise BackendTranslationError("TRANSLATION_RESULT_MISMATCH")
            self.results.persist_for_job(request.job_id, response, request)
            return response

    def _record_reservation(self, job_id: str, reservation_id: str, *, request_id: str = "") -> None:
        output_dir = self._reservation_output_dir(job_id)
        try:
            import yk_reservation
            yk_reservation.record_reservation(
                output_dir, job_id=job_id, reservation_id=reservation_id,
                request_id=request_id)
        except Exception as exc:
            raise BackendTranslationError("reservation_handoff_failed") from exc

    def _reservation_output_dir(self, job_id: str) -> Path:
        output_dir = str(os.getenv("TRADUTOR_JOB_OUTPUT_DIR") or "").strip()
        if output_dir:
            return Path(output_dir)
        if os.getenv("TRADUTOR_IA_HERMETIC_TEST_ENV") == "1":
            job_key = hashlib.sha256(str(job_id).encode()).hexdigest()[:24]
            return self.auth.root.parent / "output" / job_key
        raise BackendTranslationError("job_output_dir_missing")

    def settle_reservation(self, context: dict, *, action: str,
                           idempotency_key: str) -> dict[str, Any]:
        reservation_id = str(context.get("reservation_id") or "")
        job_id = str(context.get("job_id") or "")
        if not reservation_id or not job_id or action not in {"consume", "release"}:
            raise BackendTranslationError("invalid_reservation_context")
        auth = self.auth.acquire(job_id)
        return self.backend.settle_job(job_id=job_id, reservation_id=reservation_id,
                                       action=action, idempotency_key=idempotency_key,
                                       auth_token=auth.access_token)

    def release_reservation(self, context: dict) -> dict[str, Any]:
        raise BackendTranslationError("settlement_action_required")

    def finalize_reservation(self, context: dict) -> dict[str, Any]:
        raise BackendTranslationError("settlement_action_required")

    def translate_strict(self, text, *, previous_translation="", validation_reason="", force=False):
        # Strict retries are independent logical requests, but use the same
        # job-scoped auth and backend contract.  The base translation adapter
        # remains the source of truth for item mapping and idempotency.
        previous = str(previous_translation or "").strip()
        value = str(text or "").strip()
        if not value:
            return ""
        return self.translate_many([value], force=force)[0]
