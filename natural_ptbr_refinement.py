"""Contextual, opt-in PT-BR refinement contracts for an injected provider."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone
import threading
from dataclasses import dataclass
from typing import Any, Callable

from pipeline_cache import atomic_write_json, load_json

SCHEMA_VERSION = "2"
PROMPT_CONTRACT_VERSION = "natural-ptbr-refinement-v2"
RESPONSE_SCHEMA_VERSION = "natural-ptbr-response-v2"
RESULT_KEYS = {
    "natural_ptbr", "compact_ptbr", "neutral_ptbr",
    "literalness_detected", "meaning_preserved", "emotion_preserved",
    "information_added", "information_removed", "glossary_respected",
    "fits_visual_limit", "confidence", "warnings", "brief_reason",
}
BOOLEAN_RESULT_KEYS = {
    "literalness_detected", "meaning_preserved", "emotion_preserved",
    "information_added", "information_removed", "glossary_respected",
    "fits_visual_limit",
}
RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": sorted(RESULT_KEYS),
    "properties": {
        "natural_ptbr": {"type": "string", "minLength": 1},
        "compact_ptbr": {"type": "string", "minLength": 1},
        "neutral_ptbr": {"type": "string", "minLength": 1},
        "literalness_detected": {"type": "boolean"},
        "meaning_preserved": {"type": "boolean"},
        "emotion_preserved": {"type": "boolean"},
        "information_added": {"type": "boolean"},
        "information_removed": {"type": "boolean"},
        "glossary_respected": {"type": "boolean"},
        "fits_visual_limit": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "brief_reason": {"type": "string", "minLength": 1},
    },
}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


SCHEMA_HASH = _hash(RESPONSE_SCHEMA)


def build_request(**fields: Any) -> dict[str, Any]:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "prompt_contract_version": PROMPT_CONTRACT_VERSION,
        "response_schema_version": RESPONSE_SCHEMA_VERSION,
        **{key: fields.get(key) for key in (
            "owner", "job_id", "run_id", "revision_id", "page_id",
            "region_id", "source_text", "current_translation",
            "context_before", "context_after", "region_type", "speaker",
            "tone", "emotion", "register", "visual_character_limit",
            "glossary", "previous_decision_id", "provider", "model")},
        "supersedes_request_hash": fields.get("supersedes_request_hash"),
        "previous_invalid_result_id": fields.get("previous_invalid_result_id"),
        "previous_response_hash": fields.get("previous_response_hash"),
    }
    if not str(payload["source_text"] or "").strip():
        raise ValueError("refinement_source_empty")
    if not str(payload["current_translation"] or "").strip():
        raise ValueError("refinement_translation_empty")
    return {**payload, "request_hash": _hash(payload),
            "created_at": str(fields.get("created_at") or datetime.now(timezone.utc).isoformat()),
            "status": "ready_for_explicit_authorization"}


def build_prompt(request: dict[str, Any]) -> str:
    data = {key: request.get(key) for key in (
        "source_text", "current_translation", "context_before",
        "context_after", "region_type", "speaker", "tone", "emotion",
        "register", "visual_character_limit", "glossary")}
    example = {
        "natural_ptbr": "Exemplo natural.",
        "compact_ptbr": "Exemplo curto.",
        "neutral_ptbr": "Exemplo neutro.",
        "literalness_detected": False, "meaning_preserved": True,
        "emotion_preserved": True, "information_added": False,
        "information_removed": False, "glossary_respected": True,
        "fits_visual_limit": True, "confidence": 0.91,
        "warnings": [], "brief_reason": "A tradução já estava natural.",
    }
    return (
        "Você é um revisor profissional de tradução de quadrinhos para "
        "português brasileiro. Use o texto original como fonte de verdade. "
        "Reescreva a tradução atual para soar natural em português brasileiro, "
        "preservando significado, intenção, emoção, intensidade, nomes próprios "
        "e glossário. Não acrescente nem remova informação. Produza opções "
        "natural, compacta e neutra; a compacta não pode omitir informação. "
        "Retorne somente JSON válido exatamente no schema abaixo, sem chaves "
        "adicionais e sem explicações externas. Use booleanos JSON reais "
        "(true/false), confidence como número entre 0 e 1 e warnings como array. "
        "Não coloque esses tipos entre aspas. brief_reason deve ser curto."
        "\nSCHEMA FORMAL:\n" + _canonical(RESPONSE_SCHEMA)
        + "\nEXEMPLO APENAS DE FORMATO:\n" + _canonical(example)
        + "\nENTRADA:\n" + _canonical(data))


def structural_reason_codes(raw: Any) -> list[str]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (ValueError, TypeError, json.JSONDecodeError):
        return ["provider_response_invalid"]
    reasons = []
    if set(value) - RESULT_KEYS:
        reasons.append("provider_response_schema_invalid")
    if RESULT_KEYS - set(value):
        reasons.append("provider_response_schema_incomplete")
    if any(not isinstance(value.get(key), bool) for key in BOOLEAN_RESULT_KEYS):
        reasons.append("provider_response_schema_invalid")
    if not isinstance(value.get("warnings"), list) or any(
        not isinstance(item, str) for item in (value.get("warnings") or [])
    ):
        reasons.append("provider_response_schema_invalid")
    for key in ("natural_ptbr", "compact_ptbr", "neutral_ptbr", "brief_reason"):
        if not isinstance(value.get(key), str) or not value.get(key).strip():
            reasons.append("provider_response_schema_invalid")
    confidence = value.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        reasons.append("provider_response_schema_invalid")
    return sorted(set(reasons))


def validate_result(raw: Any, *, request: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (ValueError, TypeError, json.JSONDecodeError):
        return {"status": "provider_response_invalid",
                "reason_codes": ["provider_response_invalid"]}
    reasons = structural_reason_codes(value)
    for key in ("natural_ptbr", "compact_ptbr", "neutral_ptbr"):
        if not isinstance(value.get(key), str) or not value.get(key).strip():
            reasons.append("refinement_option_empty")
    confidence = value.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        reasons.append("refinement_confidence_invalid")
    if value.get("information_added") is True:
        reasons.append("information_added")
    if value.get("information_removed") is True:
        reasons.append("information_removed")
    if value.get("meaning_preserved") is False:
        reasons.append("semantic_drift_detected")
    if value.get("emotion_preserved") is False:
        reasons.append("emotion_not_preserved")
    if value.get("glossary_respected") is False:
        reasons.append("glossary_violation")
    limit = int(request.get("visual_character_limit") or 0)
    if limit and len(str(value.get("compact_ptbr") or "")) > limit:
        reasons.append("visual_limit_exceeded")
    status = "valid_suggestion" if not reasons else "needs_human_review"
    if {
        "provider_response_schema_incomplete",
        "provider_response_schema_invalid",
    } & set(reasons):
        status = "provider_response_invalid"
    return {"status": status, "reason_codes": sorted(set(reasons)),
            "result": value, "result_hash": _hash(value)}


class RefinementService:
    """Idempotent adapter; the caller must inject the configured provider."""
    _global_locks_guard = threading.Lock()
    _global_request_locks: dict[str, threading.Lock] = {}

    def __init__(self, provider: Callable[[str], Any], *, store: "RefinementStore | None" = None):
        self.provider = provider
        self.cache: dict[str, dict[str, Any]] = {}
        self.store = store

    def _request_lock(self, key: str) -> threading.Lock:
        with self._global_locks_guard:
            return self._global_request_locks.setdefault(key, threading.Lock())

    @staticmethod
    def _cacheable(record: dict[str, Any] | None) -> bool:
        return bool(record) and record.get("status") in {
            "valid_suggestion",
            "needs_human_review",
            "provider_response_invalid",
            "repair_semantic_content_changed",
        }

    def refine(self, request: dict[str, Any], *, authorized: bool) -> dict[str, Any]:
        if not authorized:
            raise ValueError("refinement_explicit_authorization_required")
        key = str(request["request_hash"])
        with self._request_lock(key):
            if self.store:
                persisted = self.store.get_result(
                    key, owner=str(request.get("owner") or ""))
                if self._cacheable(persisted):
                    return {**persisted, "cache_hit": True}
            if self._cacheable(self.cache.get(key)):
                return {**self.cache[key], "cache_hit": True}
            try:
                provider_value = (
                    self.provider.refine(build_prompt(request), request=request)
                    if hasattr(self.provider, "refine")
                    else self.provider(build_prompt(request))
                )
                trace = {}
                if isinstance(provider_value, RefinementProviderResponse):
                    trace = provider_value.trace
                    provider_value = provider_value.payload
                validated = validate_result(provider_value, request=request)
                if trace.get("status") == "repair_semantic_content_changed":
                    validated["status"] = "repair_semantic_content_changed"
                    validated["reason_codes"] = sorted(set(
                        validated.get("reason_codes", [])
                        + ["repair_semantic_content_changed"]))
                validated["provider_trace"] = trace
            except Exception as exc:
                reason = str(getattr(exc, "reason_code", "") or "provider_unavailable")
                validated = {
                    "status": reason,
                    "reason_codes": [reason],
                    "error_type": type(exc).__name__,
                }
            stored = {"request": request, **validated, "request_hash": key, "cache_hit": False,
                      "applied_automatically": False}
            self.cache[key] = stored
            if self.store:
                self.store.put_result(request, stored)
            return stored


def select_option(result: dict[str, Any], *, owner: str, option: str,
                  reviewer: str, authorization: str,
                  previous_decision_id: str = "", manual_text: str = "") -> dict[str, Any]:
    if authorization != "delegated_by_user":
        raise ValueError("refinement_selection_authorization_required")
    if option not in {"natural", "compact", "neutral", "keep_current", "manual"}:
        raise ValueError("refinement_selection_invalid")
    if option == "manual" and not str(manual_text or "").strip():
        raise ValueError("refinement_manual_text_empty")
    payload = {"owner": owner, "result_hash": result.get("result_hash"),
               "option": option, "reviewer": reviewer,
               "authorization": authorization,
               "previous_decision_id": previous_decision_id,
               "manual_text": manual_text if option == "manual" else ""}
    return {**payload, "selection_decision_id": _hash(payload),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "confirmed_human_selection"}


class RefinementStore:
    """Owner-scoped append-only persistence used by UI and F5 restoration."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, owner: str, kind: str, identity: str) -> Path:
        if not owner or not identity:
            raise ValueError("refinement_identity_required")
        return self.root / _hash(owner) / kind / f"{identity}.json"

    def put_result(self, request: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        owner = str(request.get("owner") or "")
        record = {"request": request, **result}
        atomic_write_json(self._path(owner, "results", request["request_hash"]), record)
        return record

    def get_result(self, request_hash: str, *, owner: str) -> dict[str, Any] | None:
        path = self._path(owner, "results", request_hash)
        return load_json(path) if path.is_file() else None

    def append_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        owner = str(decision.get("owner") or "")
        identity = str(decision.get("selection_decision_id") or "")
        path = self._path(owner, "decisions", identity)
        if not path.is_file():
            atomic_write_json(path, decision)
        return load_json(path)


class NvidiaRefinementProvider:
    """Thin adapter over the configured translator's retry/rate-limit/client path."""

    def __init__(self, translator: Any):
        self.translator = translator

    @staticmethod
    def _repair_hash(parent_hash: str, attempt: int, raw: Any,
                     errors: list[str]) -> dict[str, str]:
        original_response_hash = _hash(raw)
        validation_error_hash = _hash(errors)
        identity = {
            "parent_request_hash": parent_hash,
            "repair_attempt_index": attempt,
            "original_response_hash": original_response_hash,
            "validation_error_hash": validation_error_hash,
            "schema_hash": SCHEMA_HASH,
        }
        return {**identity, "repair_request_hash": _hash(identity)}

    @staticmethod
    def _content_signature(payload: Any) -> dict[str, Any]:
        try:
            value = json.loads(payload) if isinstance(payload, str) else dict(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        comparable = dict(value)
        for key in BOOLEAN_RESULT_KEYS:
            raw = comparable.get(key)
            if isinstance(raw, str) and raw.strip().lower() in {"true", "false"}:
                comparable[key] = raw.strip().lower() == "true"
        warnings = comparable.get("warnings")
        if isinstance(warnings, str):
            try:
                parsed_warnings = json.loads(warnings)
                if isinstance(parsed_warnings, list):
                    comparable["warnings"] = parsed_warnings
            except json.JSONDecodeError:
                pass
        return {
            key: comparable.get(key) for key in (
                "natural_ptbr", "compact_ptbr", "neutral_ptbr", "brief_reason",
                "warnings", *sorted(BOOLEAN_RESULT_KEYS))
            if comparable.get(key) not in (None, "")
        }

    def refine(self, prompt: str, *, request: dict[str, Any]) -> "RefinementProviderResponse":
        if not getattr(self.translator, "is_configured", False):
            raise RuntimeError("provider_not_configured")
        messages = [
            {
                "role": "system",
                "content": "Retorne somente o JSON solicitado para revisão linguística.",
            },
            {"role": "user", "content": prompt},
        ]
        deadline = (
            self.translator._clock()
            + self.translator.timeout_policy.total_timeout_seconds
        )
        modes = [
            ("json_schema", {
                "type": "json_schema",
                "json_schema": {
                    "name": "natural_ptbr_refinement",
                    "strict": True,
                    "schema": RESPONSE_SCHEMA,
                },
            }),
            ("json_object", {"type": "json_object"}),
            ("prompt_only_structured_output", None),
        ]
        raw = None
        native_mode = ""
        native_rejections = []
        for native_mode, response_format in modes:
            try:
                raw = self.translator._request_with_retry(
                    messages, deadline=deadline,
                    response_format=response_format)
                break
            except Exception as exc:
                if (
                    response_format is None
                    or getattr(exc, "reason_code", "") != "provider_client_error"
                ):
                    raise
                native_rejections.append(native_mode)
        attempts = []
        errors = structural_reason_codes(raw)
        original_signature = self._content_signature(raw)
        original_raw = raw
        for attempt in range(1, self.translator.json_retry_limit):
            if not errors:
                break
            identity = self._repair_hash(
                str(request["request_hash"]), attempt, raw, errors)
            repair_messages = messages + [
                {"role": "assistant", "content": str(raw)[:6000]},
                {
                    "role": "user",
                    "content": (
                        "Corrija SOMENTE a estrutura e os tipos JSON da resposta "
                        "anterior. Preserve exatamente as três traduções e o motivo "
                        "breve. Não melhore, reescreva ou altere conteúdo linguístico. "
                        f"Erros objetivos: {_canonical(errors)}. "
                        f"Schema obrigatório: {_canonical(RESPONSE_SCHEMA)}. "
                        "Retorne apenas o objeto JSON corrigido."
                    ),
                },
            ]
            repaired = self.translator._request_with_retry(
                repair_messages, deadline=deadline,
                response_format=next(
                    value for name, value in modes if name == native_mode))
            self.translator._increment_stat("json_repair_attempts")
            repaired_errors = structural_reason_codes(repaired)
            repaired_signature = self._content_signature(repaired)
            changed = bool(original_signature) and any(
                repaired_signature.get(key) != value
                for key, value in original_signature.items()
            )
            attempts.append({
                **identity,
                "repair_response_hash": _hash(repaired),
                "validation_errors": repaired_errors,
                "semantic_content_changed": changed,
            })
            raw = repaired
            errors = repaired_errors
            if changed:
                return RefinementProviderResponse(
                    payload=raw,
                    trace={
                        "status": "repair_semantic_content_changed",
                        "native_mode": native_mode,
                        "native_rejections": native_rejections,
                        "raw_response": original_raw,
                        "repair_attempts": attempts,
                    })
        status = (
            "valid_without_repair" if not attempts and not errors
            else "valid_after_repair" if attempts and not errors
            else "repair_schema_still_invalid"
        )
        final_payload = (
            json.loads(raw) if isinstance(raw, str) and not errors else raw
        )
        return RefinementProviderResponse(
            payload=final_payload,
            trace={
                "status": status,
                "native_mode": native_mode,
                "native_rejections": native_rejections,
                "raw_response": original_raw,
                "repair_attempts": attempts,
            })

    def __call__(self, prompt: str) -> Any:
        raise RuntimeError("refinement_request_context_required")


@dataclass(frozen=True)
class RefinementProviderResponse:
    payload: Any
    trace: dict[str, Any]
