"""Local contract for the server-side Yomu translation provider.

The default implementation is deliberately injectable/mocked in this phase.  It
keeps auth, reservation and result identity separate from provider transport so a
future Edge Function deployment can adopt the same contract without DeepL locally.
"""
from __future__ import annotations

import threading
import uuid
import os
import json
import time
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
        return ""
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
                        finalize_job: bool = True) -> BatchResponse: ...


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

    def translate_batch(self, request: BatchRequest, *, reservation_id: str, auth_token: str,
                        finalize_job: bool = True) -> BatchResponse:
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
    """Small idempotent result store; production can replace this with SQLite/RPC."""
    def __init__(self):
        self._lock = threading.RLock()
        self._records: dict[tuple[str, str], BatchResponse] = {}
        self._states: dict[tuple[str, str], str] = {}

    def get(self, job_id: str, request_id: str) -> BatchResponse | None:
        with self._lock:
            return self._records.get((job_id, request_id))

    def begin(self, job_id: str, request_id: str) -> str:
        with self._lock:
            key = (job_id, request_id)
            if key in self._records:
                return "completed"
            if self._states.get(key) == "provider_pending":
                return "provider_pending"
            self._states[key] = "provider_pending"
            return "created"

    def persist_for_job(self, job_id: str, response: BatchResponse) -> None:
        with self._lock:
            key = (job_id, response.request_id)
            self._records[key] = response
            self._states[key] = "completed"

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
                self.reserve_count += 1
            return {"reservation_id": reservation, "required_yk": 1}

    def translate_batch(self, request: BatchRequest, *, reservation_id: str, auth_token: str,
                        finalize_job: bool = True) -> BatchResponse:
        if self.fail:
            raise BackendTranslationError(self.fail)
        with self._lock:
            if request.request_id in self._responses:
                return self._responses[request.request_id]
            self.provider_execution_count += 1
            chunks = [request.items[i:i + self.chunk_size] for i in range(0, len(request.items), self.chunk_size)]
            self.provider_chunk_count += len(chunks)
            response = BatchResponse(request.request_id, "completed", tuple(
                BatchItem(item.item_id, f"[{request.target_lang}] {item.text}") for item in request.items), reservation_id)
            self._responses[request.request_id] = response
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
        existing = self.results.get(request.job_id, request.request_id)
        if existing:
            return existing
        with self._lock_for((request.job_id, request.request_id)):
            existing = self.results.get(request.job_id, request.request_id)
            if existing:
                return existing
            try:
                context = self.auth.acquire(request.job_id)
            except AuthContextError as exc:
                raise BackendTranslationError(str(exc)) from exc
            self.results.begin(request.job_id, request.request_id)
            reservation = self.backend.reserve(job_id=request.job_id, request_id=request.request_id, auth_token=context.access_token, device_id=_validated_device_uuid())
            reservation_id = str(reservation.get("reservation_id") or "")
            if not reservation_id:
                raise BackendTranslationError("RESERVATION_FAILED")
            response = self.backend.translate_batch(request, reservation_id=reservation_id,
                                                    auth_token=context.access_token,
                                                    finalize_job=True)
            expected = {item.item_id for item in request.items}
            actual = [item.item_id for item in response.items]
            if response.request_id != request.request_id or set(actual) != expected or len(actual) != len(set(actual)):
                raise BackendTranslationError("TRANSLATION_RESULT_MISMATCH")
            self.results.persist_for_job(request.job_id, response)
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
        job_id = str(os.getenv("TRADUTOR_JOB_ID") or "").strip()
        request_id = str(os.getenv("TRADUTOR_REQUEST_ID") or f"translation:{job_id}").strip()
        if not job_id:
            raise BackendTranslationError("job_id_required")
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

        # Keep the original request identity for the common single-batch case.
        # For a split request, deterministic suffixes make each persisted result
        # independently replayable while retaining one logical job/reservation.
        if len(chunks) == 1:
            response = self.translate_batch(BatchRequest(request_id, job_id, "EN", "PT-BR", chunks[0]))
            responses = [response]
        else:
            try:
                context = self.auth.acquire(job_id)
            except AuthContextError as exc:
                raise BackendTranslationError(str(exc)) from exc
            reservation = self.backend.reserve(job_id=job_id, request_id=request_id,
                                               auth_token=context.access_token,
                                               device_id=_validated_device_uuid())
            reservation_id = str(reservation.get("reservation_id") or "")
            if not reservation_id:
                raise BackendTranslationError("RESERVATION_FAILED")
            device_id = _validated_device_uuid()
            responses = []
            for index, chunk in enumerate(chunks, start=1):
                sub_request_id = f"{request_id}:batch:{index:04d}"
                sub_request = BatchRequest(sub_request_id, job_id, "EN", "PT-BR", chunk)
                responses.append(self._translate_batch_with_reservation(
                    sub_request, reservation_id=reservation_id,
                    auth_token=context.access_token,
                    finalize_job=(index == len(chunks))))
        self.stats.update({"request_id": request_id, "job_id": job_id,
                           "translation_results": len(items),
                           "translation_batches": len(responses)})
        by_id = {item.item_id: item.text for response in responses for item in response.items}
        return [by_id[item.item_id] for item in items]

    def _translate_batch_with_reservation(self, request: BatchRequest, *, reservation_id: str,
                                          auth_token: str, finalize_job: bool = True) -> BatchResponse:
        existing = self.results.get(request.job_id, request.request_id)
        if existing:
            return existing
        with self._lock_for((request.job_id, request.request_id)):
            existing = self.results.get(request.job_id, request.request_id)
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
            self.results.persist_for_job(request.job_id, response)
            return response

    def translate_strict(self, text, *, previous_translation="", validation_reason="", force=False):
        # Strict retries are independent logical requests, but use the same
        # job-scoped auth and backend contract.  The base translation adapter
        # remains the source of truth for item mapping and idempotency.
        previous = str(previous_translation or "").strip()
        value = str(text or "").strip()
        if not value:
            return ""
        return self.translate_many([value], force=force)[0]
