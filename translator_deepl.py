"""DeepL translation provider (TDD #27).

A first-class provider next to Riva/Nemotron, not a variant of them: DeepL is a
dedicated MT endpoint, so this module owns the whole HTTP contract (request
construction, response validation, error normalization, billed-character
telemetry) and exposes only the translator interface the pipeline already
speaks -- ``translate_many``/``translate``/``stats``/``model``.

Deliberately absent, and each absence is a contract:

* no ``translate_strict``  -> the quality gate never asks DeepL to re-translate
  a region (``ocr_balloon``/``provider_execution`` both gate retries on
  ``hasattr(translator, "translate_strict")``).  The TDD #26 benchmark needed
  none, so none is offered.
* no transport retry        -> one HTTP attempt per chunk.  A retry here would
  stack under the pipeline's own bounded retries and amplify.
* no translation cache      -> a run's provider request count stays honest.
* no cross-provider or paid-endpoint fallback.  A failed batch yields empty
  candidates, never the source text and never another provider.
"""
from __future__ import annotations

import json
import time
from urllib.parse import urlparse

import config
import semantic_fidelity
from provider_transport import ProviderTimeoutPolicy

PROVIDER_NAME = "deepl"
PROVIDER_FAMILY = "deepl"
DEFAULT_MODEL_TYPE = "quality_optimized"
TRANSLATE_PATH = "/v2/translate"

# DeepL's documented hard maximum is 128 KiB per request body.  The budget is
# the serialized-body size actually measured before sending, not a text-count
# heuristic: an item count says nothing about bytes once UTF-8 and JSON
# escaping are involved.
PROVIDER_MAX_REQUEST_BYTES = 128 * 1024

# Source language names this repository already uses (translator_nllb.get_translator)
# mapped to DeepL source codes.  Target is PT-BR, the only pair this product ships.
_SOURCE_LANG = {"ingles": "EN", "japones": "JA", "coreano": "KO"}
TARGET_LANG = "PT-BR"

# Normalized failure taxonomy.  Collapsing everything into "translation failed"
# is what makes a quota wall indistinguishable from a typo in a key.
AUTH_ERROR = "deepl_auth_error"
QUOTA_EXCEEDED = "deepl_quota_exceeded"
RATE_LIMITED = "deepl_rate_limited"
PROVIDER_OVERLOAD = "deepl_provider_overload"
PROVIDER_HTTP_ERROR = "deepl_provider_http_error"
TRANSPORT_ERROR = "deepl_transport_error"
INVALID_RESPONSE = "deepl_invalid_response"
RESPONSE_COUNT_MISMATCH = "deepl_response_count_mismatch"
NOT_CONFIGURED = "deepl_not_configured"

REASON_CODES = frozenset({
    AUTH_ERROR, QUOTA_EXCEEDED, RATE_LIMITED, PROVIDER_OVERLOAD,
    PROVIDER_HTTP_ERROR, TRANSPORT_ERROR, INVALID_RESPONSE,
    RESPONSE_COUNT_MISMATCH, NOT_CONFIGURED,
})

# https://developers.deepl.com/docs/best-practices/error-handling
_STATUS_REASON = {
    401: AUTH_ERROR,
    403: AUTH_ERROR,
    429: RATE_LIMITED,
    456: QUOTA_EXCEEDED,
    500: PROVIDER_OVERLOAD,
    502: PROVIDER_OVERLOAD,
    503: PROVIDER_OVERLOAD,
    504: PROVIDER_OVERLOAD,
    529: RATE_LIMITED,
}


class DeepLProviderError(RuntimeError):
    """A normalized DeepL failure.  Never carries a credential."""

    def __init__(self, reason_code: str, *, status_code=None, detail: str = ""):
        super().__init__(reason_code)
        self.reason_code = str(reason_code)
        self.status_code = status_code
        # Bounded, sanitized diagnostic: a response head, never a request header.
        self.detail = _sanitize_detail(detail)

    def public(self) -> dict:
        return {
            "reason_code": self.reason_code,
            "status_code": self.status_code,
            "detail": self.detail,
        }


def _sanitize_detail(value, limit: int = 200) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    # A DeepL error body never contains the key, but an unexpected proxy body
    # might echo the Authorization header back; drop anything that looks like it.
    lowered = text.casefold()
    if "deepl-auth-key" in lowered or "authorization" in lowered:
        return "redacted_possible_credential_echo"
    return text[:limit]


def reason_for_status(status_code) -> str:
    try:
        status = int(status_code)
    except (TypeError, ValueError):
        return TRANSPORT_ERROR
    if status in _STATUS_REASON:
        return _STATUS_REASON[status]
    if 500 <= status < 600:
        return PROVIDER_OVERLOAD
    return PROVIDER_HTTP_ERROR


def _body_bytes(texts, source_lang: str, model_type: str, context: str = "") -> bytes:
    """The exact bytes that go on the wire.  Chunk sizing measures this."""
    body = {
        "text": list(texts),
        "source_lang": source_lang,
        "target_lang": TARGET_LANG,
        "model_type": model_type,
        "show_billed_characters": True,
    }
    if context:
        # DeepL's documented `context` field: read to disambiguate, never
        # translated and never returned. The first, batched pass sends none.
        body["context"] = context
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def plan_chunks(texts, *, source_lang: str = "EN",
                model_type: str = DEFAULT_MODEL_TYPE,
                max_bytes: int = None) -> list[list[int]]:
    """Deterministic minimal chunks of *indexes*, in order, each within budget.

    Greedy and order-preserving, so every input appears exactly once and the
    positional association survives.  A single item that alone exceeds the
    budget still gets its own chunk: splitting one balloon's text would corrupt
    the mapping, so the provider is left to reject it and the batch fails closed.
    """
    budget = int(max_bytes or config.DEEPL_MAX_REQUEST_BYTES)
    budget = max(1, min(budget, PROVIDER_MAX_REQUEST_BYTES))
    chunks: list[list[int]] = []
    current: list[str] = []
    current_idx: list[int] = []
    for index, text in enumerate(texts):
        candidate = current + [str(text)]
        if current and len(_body_bytes(candidate, source_lang, model_type)) > budget:
            chunks.append(current_idx)
            current, current_idx = [str(text)], [index]
            continue
        current, current_idx = candidate, current_idx + [index]
    if current_idx:
        chunks.append(current_idx)
    return chunks


def _httpx_transport(url, headers, body, timeout):
    import httpx

    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, headers=headers, content=body)
        return response.status_code, response.text


class DeepLTranslator:
    """DeepL ``/v2/translate`` with provider-native array batching."""

    def __init__(
        self,
        api_key=None,
        base_url=None,
        model_type=None,
        source_language="ingles",
        max_request_bytes=None,
        transport=None,
        operation="translation",
    ):
        self.api_key = str(
            config.DEEPL_API_KEY if api_key is None else api_key
        ).strip()
        self.base_url = str(base_url or config.DEEPL_API_BASE_URL or "").strip().rstrip("/")
        parsed = urlparse(self.base_url)
        if not self.base_url or parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("deepl_base_url_invalid")
        self.model_type = str(model_type or config.DEEPL_MODEL_TYPE or DEFAULT_MODEL_TYPE).strip()
        self.source_language = str(source_language or "ingles")
        self.source_lang = _SOURCE_LANG.get(self.source_language.lower(), "EN")
        self.max_request_bytes = max(
            1,
            min(
                int(max_request_bytes or config.DEEPL_MAX_REQUEST_BYTES),
                PROVIDER_MAX_REQUEST_BYTES,
            ),
        )
        self.translation_provider = PROVIDER_NAME
        # `model` is what provenance/manifest record as the provider model; for
        # DeepL the model identity *is* the model type.
        self.model = self.model_type
        self.operation = str(operation or "translation")
        self._transport = transport or _httpx_transport
        self.timeout_policy = ProviderTimeoutPolicy(
            connect_timeout_seconds=float(config.DEEPL_CONNECT_TIMEOUT_SECONDS),
            read_timeout_seconds=float(config.DEEPL_READ_TIMEOUT_SECONDS),
            write_timeout_seconds=float(config.DEEPL_WRITE_TIMEOUT_SECONDS),
            pool_timeout_seconds=float(config.DEEPL_POOL_TIMEOUT_SECONDS),
            total_timeout_seconds=float(config.DEEPL_TOTAL_TIMEOUT_SECONDS),
            source="config",
            provider=PROVIDER_NAME,
            operation=self.operation,
        )
        # Accepted for interface parity with the NVIDIA translator; DeepL keeps
        # no translation cache, so there is nothing for `force` to bypass.
        self.force_cache = False
        self.ptbr_naturalizer = None
        # Attached to strict retries only - see ``translate_strict``.
        self.session_context_text = ""
        self.stats = {
            "provider_name": PROVIDER_NAME,
            "provider_family": PROVIDER_FAMILY,
            "model": self.model,
            "model_type_requested": self.model_type,
            "model_type_used": "",
            "language_pair": f"{self.source_lang}->{TARGET_LANG}",
            "input_texts": 0,
            "api_texts": 0,
            "api_requests": 0,
            "api_seconds": 0.0,
            "cache_hits": 0,
            "translation_batches": 0,
            "translation_responses_received": 0,
            "translation_responses_nonempty": 0,
            "translation_results_associated": 0,
            "successful_batches": 0,
            "failed_batches": 0,
            "provider_request_count": 0,
            "provider_billed_characters": 0,
            "provider_source_characters": 0,
            "provider_rate_limited_count": 0,
            "provider_quota_exceeded_count": 0,
            "provider_error_count": 0,
            "last_transport_reason": "",
            "provider_failures": [],
            "max_request_bytes": self.max_request_bytes,
            "naturalization_enabled": False,
            "naturalization_mode": "off",
        }

    # ------------------------------------------------------------------ config
    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}{TRANSLATE_PATH}"

    # ------------------------------------------------------------- translation
    def translate(self, text):
        return self.translate_many([text])[0]

    def translate_many(self, texts, force=None):
        """Translate in provider-native array batches.

        Returns one candidate per input, positionally.  A failed chunk yields
        empty strings for its items -- never the source, never a neighbour's
        translation, never a value from another provider.
        """
        texts = list(texts or [])
        if not texts:
            return []
        translations = ["" for _ in texts]
        pending = [(i, str(t)) for i, t in enumerate(texts) if str(t).strip()]
        self._bump("input_texts", len(pending))
        if not pending:
            return translations
        if not self.is_configured:
            self._fail_closed(NOT_CONFIGURED, len(pending))
            return translations

        chunks = plan_chunks(
            [t for _, t in pending],
            source_lang=self.source_lang,
            model_type=self.model_type,
            max_bytes=self.max_request_bytes,
        )
        self._bump("translation_batches", len(chunks))
        for positions in chunks:
            batch = [pending[p] for p in positions]
            batch_texts = [t for _, t in batch]
            self._bump("api_texts", len(batch_texts))
            self._bump("provider_source_characters", sum(len(t) for t in batch_texts))
            try:
                candidates = self._translate_chunk(batch_texts)
            except DeepLProviderError as exc:
                self._fail_closed(exc.reason_code, len(batch_texts),
                                  status_code=exc.status_code, detail=exc.detail)
                continue
            self._bump("successful_batches")
            self._bump("translation_responses_received", len(candidates))
            self._bump("translation_responses_nonempty",
                       sum(1 for c in candidates if c.strip()))
            for (original_index, _), candidate in zip(batch, candidates):
                translations[original_index] = candidate
                if candidate.strip():
                    self._bump("translation_results_associated")
        return translations

    def translate_strict(
        self,
        text,
        previous_translation="",
        validation_reason="",
        force=False,
        allow_proper_names=True,
        proper_names=None,
        retry_origin="quality_retry",
        retry_attempt=1,
        source_context=(),
    ):
        """The second attempt a rejected candidate needs.

        DeepL takes no instructions, so there is no prompt to correct and the
        rejection reason cannot be spoken to the model.  Three things can still
        differ from the first attempt, and all of them matter for the failure
        class this exists for - a class noun read as a verb:

        * the source is the canonical one, so a boundary the normalizer
          repaired since the first pass is what goes on the wire;
        * the request is single-item and carries the chapter's terminology in
          DeepL's ``context`` field, which the batched first pass never sends;
        * ``source_context`` adds the neighbouring source lines of the scene to
          that same field.  This is precisely what the field is documented for -
          text read to disambiguate and never translated - and it is the only
          thing that can settle which sense of an ambiguous word this scene
          means.  Descriptive evidence, never an instruction.

        Without this method ``validate_and_retry_translations`` skips the retry
        entirely (it gates on ``hasattr(translator, "translate_strict")``), so
        under DeepL every semantic rejection ended as an untranslated region.
        """
        text = str(text or "")
        if not text.strip() or not self.is_configured:
            return ""
        self._bump("strict_retry_requests")
        self._bump("api_texts", 1)
        self._bump("provider_source_characters", len(text))
        context = " ".join(
            part for part in (self.session_context_text,
                              semantic_fidelity.scene_context(source_context))
            if part
        )
        try:
            candidates = self._translate_chunk([text], context=context)
        except DeepLProviderError as exc:
            self._fail_closed(exc.reason_code, 1,
                              status_code=exc.status_code, detail=exc.detail)
            return ""
        candidate = candidates[0] if candidates else ""
        if candidate.strip() and candidate.strip() == str(previous_translation or "").strip():
            # The provider stood by the answer that was just rejected. Reported,
            # never re-offered: the validator will reject it again.
            self._bump("strict_retry_duplicate_candidates")
        return candidate

    def _translate_chunk(self, texts, context: str = "") -> list[str]:
        body = _body_bytes(texts, self.source_lang, self.model_type, context)
        if len(body) > PROVIDER_MAX_REQUEST_BYTES:
            raise DeepLProviderError(
                PROVIDER_HTTP_ERROR, detail="request_body_exceeds_provider_maximum")
        headers = {
            # Constructed per call and never stored, logged or echoed.
            "Authorization": f"DeepL-Auth-Key {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Tradutor.Ia/1.0",
        }
        started = time.perf_counter()
        try:
            status, payload = self._transport(
                self.endpoint, headers, body,
                self.timeout_policy.httpx_timeout(),
            )
        except DeepLProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalized, never re-raised raw
            raise DeepLProviderError(
                TRANSPORT_ERROR, detail=type(exc).__name__) from None
        finally:
            self._bump("api_requests")
            self._bump("provider_request_count")
            self._bump("api_seconds", time.perf_counter() - started)
        return self._parse_response(status, payload, len(texts))

    def _parse_response(self, status, payload, expected: int) -> list[str]:
        if int(status or 0) != 200:
            raise DeepLProviderError(
                reason_for_status(status), status_code=status,
                detail=str(payload or "")[:200])
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            raise DeepLProviderError(
                INVALID_RESPONSE, status_code=status, detail="unparseable_json") from None
        if not isinstance(data, dict) or not isinstance(data.get("translations"), list):
            raise DeepLProviderError(
                INVALID_RESPONSE, status_code=status, detail="translations_missing")
        rows = data["translations"]
        if len(rows) != expected:
            # Strict positional mapping or nothing: a short or long array must
            # never be zipped against the inputs, which would shift every
            # remaining balloon onto the wrong source.
            raise DeepLProviderError(
                RESPONSE_COUNT_MISMATCH, status_code=status,
                detail=f"expected={expected} returned={len(rows)}")
        if any(not isinstance(row, dict) for row in rows):
            raise DeepLProviderError(
                INVALID_RESPONSE, status_code=status, detail="translation_entry_malformed")
        used = str(rows[0].get("model_type_used") or "") if rows else ""
        if used:
            self.stats["model_type_used"] = used
        billed = 0
        for row in rows:
            try:
                billed += int(row.get("billed_characters") or 0)
            except (TypeError, ValueError):
                pass
        self._bump("provider_billed_characters", billed)
        # An empty string is a real (empty) candidate, not a reason to substitute
        # the source; downstream validation decides what to do with it.
        return [str(row.get("text") or "") for row in rows]

    # ------------------------------------------------------------------ stats
    def _bump(self, name, value=1):
        self.stats[name] = self.stats.get(name, 0) + value

    def _fail_closed(self, reason_code, item_count, *, status_code=None, detail=""):
        self._bump("failed_batches")
        self.stats["last_transport_reason"] = reason_code
        if reason_code == RATE_LIMITED:
            self._bump("provider_rate_limited_count")
        elif reason_code == QUOTA_EXCEEDED:
            self._bump("provider_quota_exceeded_count")
        else:
            self._bump("provider_error_count")
        failures = self.stats.setdefault("provider_failures", [])
        if len(failures) < 20:
            failures.append({
                "reason_code": reason_code,
                "status_code": status_code,
                "detail": _sanitize_detail(detail),
                "items": int(item_count),
            })

    # -------------------------------------------------- interface no-ops
    def set_session_context(self, context_store):
        """Kept for the strict retry only; the batched first pass sends none."""
        try:
            fragment = str(getattr(context_store, "prompt_fragment", lambda: "")() or "")
        except Exception:  # noqa: BLE001 - a ledger outage must never sink a chapter.
            fragment = ""
        self.session_context_text = fragment.strip()
        self.stats["context_enabled"] = bool(self.session_context_text)

    def set_detected_names(self, names):
        """Proper-name handling is DeepL-internal; nothing to inject."""
        self.stats["detected_name_count"] = len(list(names or []))


def usage(api_key=None, base_url=None, transport=None) -> dict:
    """Free-quota probe against ``/v2/usage``.  Never called per item."""
    translator = DeepLTranslator(api_key=api_key, base_url=base_url, transport=transport)
    if not translator.is_configured:
        raise DeepLProviderError(NOT_CONFIGURED)
    send = transport or _httpx_transport
    try:
        status, payload = send(
            f"{translator.base_url}/v2/usage",
            {"Authorization": f"DeepL-Auth-Key {translator.api_key}"},
            None,
            translator.timeout_policy.httpx_timeout(),
        )
    except Exception as exc:  # noqa: BLE001
        raise DeepLProviderError(TRANSPORT_ERROR, detail=type(exc).__name__) from None
    if int(status or 0) != 200:
        raise DeepLProviderError(reason_for_status(status), status_code=status)
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        raise DeepLProviderError(INVALID_RESPONSE, status_code=status) from None
    count = int(data.get("character_count") or 0)
    limit = int(data.get("character_limit") or 0)
    return {"character_count": count, "character_limit": limit,
            "character_remaining": max(0, limit - count)}
