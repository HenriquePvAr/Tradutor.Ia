"""Persisted, UI-driven full-chapter quality revision.

This module intentionally works from existing pipeline artifacts.  It never downloads
chapter pages, opens a browser, publishes anything, or mutates the original PDF.  A
revision run is a separate audit/review workspace tied to a parent job/run.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

import config
from ocr_balloon import TextGroup, render_analyzed_image
from ocr_engine import OCRLine
from pdf import generate_pdf
from translator_nvidia import TranslatorNvidiaBatch


REVISION_PHASES = [
    "preparing",
    "inspecting_pages",
    "detecting_missing_text",
    "revalidating_ocr",
    "contextual_translation_review",
    "terminology_validation",
    "layout_validation",
    "applying_fixes",
    "incremental_render",
    "pdf_generation",
    "pdf_inspection",
    "finalized",
]

TRANSLATABLE_CLASSES = {
    "speech",
    "dialogue",
    "narration",
    "thought",
    "system_message",
    "location",
    "title",
    "unknown",
}
PRESERVED_CLASSES = {"sfx", "decorative", "credit", "watermark", "editorial"}
REVIEW_SCHEMA_VERSION = "1.0"
REVIEW_ACTIONS = {"keep", "rewrite", "preserve_original", "manual_review"}
REVIEW_RISKS = {"low", "medium", "high"}
REVIEW_RESULT_FIELDS = {
    "region_id",
    "action",
    "revised_translation",
    "reason_code",
    "confidence",
    "risk",
    "terminology",
}
REVIEW_ENVELOPE_FIELDS = {"schema_version", "batch_id", "results"}
NVIDIA_REVIEW_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "batch_id", "results"],
    "properties": {
        "schema_version": {"type": "string", "const": REVIEW_SCHEMA_VERSION},
        "batch_id": {"type": "string"},
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "region_id",
                    "action",
                    "revised_translation",
                    "reason_code",
                    "confidence",
                    "risk",
                    "terminology",
                ],
                "properties": {
                    "region_id": {"type": "string"},
                    "action": {"type": "string", "enum": sorted(REVIEW_ACTIONS)},
                    "revised_translation": {"type": "string"},
                    "reason_code": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "risk": {"type": "string", "enum": sorted(REVIEW_RISKS)},
                    "terminology": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["source", "target"],
                            "properties": {
                                "source": {"type": "string"},
                                "target": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    },
}
_NVIDIA_REVIEW_REQUEST_SCRIPT = r"""
import json
import sys
import time
import urllib.error
import urllib.request

import config

request = json.loads(sys.stdin.read() or "{}")
url = str(request.get("url") or "")
payload = request.get("payload") or {}
http_request = urllib.request.Request(
    url,
    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    headers={
        "Authorization": "Bearer " + str(config.NVIDIA_API_KEY or ""),
        "Content-Type": "application/json",
    },
    method="POST",
)
started = time.perf_counter()
try:
    with urllib.request.urlopen(http_request, timeout=45) as response:
        body = response.read().decode("utf-8", errors="replace")
        sys.stdout.write(json.dumps({
            "ok": True,
            "status_http": int(getattr(response, "status", 0) or 0),
            "duration_seconds": time.perf_counter() - started,
            "body": body,
        }, ensure_ascii=False))
except urllib.error.HTTPError as exc:
    body = exc.read().decode("utf-8", errors="replace")
    sys.stdout.write(json.dumps({
        "ok": False,
        "status_http": int(getattr(exc, "code", 0) or 0),
        "duration_seconds": time.perf_counter() - started,
        "body": body,
        "error": "http_error",
    }, ensure_ascii=False))
    sys.exit(0)
except Exception as exc:
    sys.stdout.write(json.dumps({
        "ok": False,
        "status_http": None,
        "duration_seconds": time.perf_counter() - started,
        "body": "",
        "error": type(exc).__name__,
        "error_message": str(exc)[:500],
    }, ensure_ascii=False))
    sys.exit(0)
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def looks_like_source_english(text: str) -> bool:
    tokens = re.findall(r"[A-Za-z]{2,}", str(text or ""))
    if not tokens:
        return False
    common = {
        "the", "and", "you", "that", "this", "with", "for", "are", "was",
        "were", "have", "has", "not", "what", "when", "where", "from",
        "your", "their", "will", "just", "maybe", "should", "can", "cannot",
        "into", "there", "here", "then", "than", "but", "because", "after",
        "before", "while", "about", "right", "outside",
    }
    hits = sum(1 for token in tokens if token.lower() in common)
    return hits >= 1 and hits / max(1, len(tokens)) >= 0.18


def looks_like_mojibake(text: str) -> bool:
    return bool(re.search(r"Ã.|â€|�", str(text or "")))


def target_text_is_safe(candidate: str) -> bool:
    value = str(candidate or "").strip()
    if not value:
        return False
    if looks_like_mojibake(value):
        return False
    if looks_like_source_english(value):
        return False
    if re.search(r"```|^\s*[{[]|\"action\"\s*:", value):
        return False
    return True


def stable_region_key(page: int, item: dict[str, Any]) -> str:
    raw_id = str(item.get("region_id") or item.get("id") or "").strip() or "region"
    return f"p{int(page):03d}:{raw_id}"


def page_number(page: dict[str, Any]) -> int:
    return int(page.get("index") or page.get("sequence_index") or 0)


def item_text(item: dict[str, Any]) -> str:
    return str(item.get("clean_text") or item.get("text") or item.get("repaired_text") or item.get("raw_text") or "")


def item_translation(item: dict[str, Any]) -> str:
    return str(item.get("translation") or item.get("translation_candidate") or "")


@dataclass
class RevisionPaths:
    root: Path
    manifest: Path
    page_audit: Path
    contextual_review: Path
    glossary: Path
    visual_inspection: Path
    render_audit: Path
    checkpoint: Path
    raw_responses: Path


@dataclass
class ReviewContractResult:
    valid: bool
    items: list[dict[str, Any]]
    categories: list[str]
    error: str
    diagnostics: dict[str, Any]
    repaired: bool = False


class ReviewContractError(ValueError):
    def __init__(self, error: str, categories: list[str] | None = None, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(error)
        self.error = error
        self.categories = categories or ["unknown"]
        self.diagnostics = diagnostics or {}


class ContextualNvidiaReviewer:
    """Small JSON reviewer with stable region IDs.

    The prompt asks for review decisions, not fresh translation cache entries.  This
    class therefore calls the configured NVIDIA-compatible endpoint directly and does
    not write to the translation cache.
    """

    def __init__(self) -> None:
        self.model = config.NVIDIA_TRANSLATION_MODEL
        self.base_url = config.NVIDIA_BASE_URL
        self.api_key = config.NVIDIA_API_KEY
        self.requests = 0
        self.duration_seconds = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_key != "sua_chave_aqui")

    def review_batch(
        self,
        records: list[dict[str, Any]],
        glossary: dict[str, Any],
        *,
        batch_id: str | None = None,
        raw_response_dir: str | Path | None = None,
        request_budget: int | None = None,
    ) -> list[dict[str, Any]]:
        if not records:
            return []
        if not self.configured:
            raise RuntimeError("nvidia_not_configured")
        self.valid_batches = getattr(self, "valid_batches", 0)
        self.repaired_batches = getattr(self, "repaired_batches", 0)
        self.fallback_individual = getattr(self, "fallback_individual", 0)
        self.invalid_batches = getattr(self, "invalid_batches", 0)
        batch_id = str(batch_id or f"review-{uuid.uuid4().hex[:12]}")
        raw_dir = Path(raw_response_dir) if raw_response_dir else None
        response = self._send_review_request(records, glossary, batch_id, "review")
        if response.get("provider_error") and not response.get("content"):
            parsed = ReviewContractResult(
                False,
                [],
                self._provider_error_categories(str(response.get("provider_error") or "")),
                str(response.get("provider_error") or "nvidia_review_request_failed"),
                {},
            )
            self._write_raw_response(raw_dir, batch_id, "review", records, response, parsed)
            self.invalid_batches += 1
            return self._manual_reviews(records, str(response.get("provider_error") or "nvidia_review_request_failed"), parsed.categories)
        parsed = self._parse_contract_response(response.get("content", ""), records, batch_id)
        self._write_raw_response(raw_dir, batch_id, "review", records, response, parsed)
        if parsed.valid:
            self.valid_batches += 1
            return parsed.items

        repair = self._send_repair_request(records, batch_id, response.get("content", ""), parsed)
        if repair.get("provider_error") and not repair.get("content"):
            repair_parsed = ReviewContractResult(
                False,
                [],
                self._provider_error_categories(str(repair.get("provider_error") or "")),
                str(repair.get("provider_error") or "nvidia_review_repair_failed"),
                {},
            )
            self._write_raw_response(raw_dir, batch_id, "repair", records, repair, repair_parsed)
            return self._manual_reviews(records, str(repair.get("provider_error") or "nvidia_review_repair_failed"), repair_parsed.categories)
        repair_parsed = self._parse_contract_response(repair.get("content", ""), records, batch_id)
        repair_parsed.repaired = repair_parsed.valid
        self._write_raw_response(raw_dir, batch_id, "repair", records, repair, repair_parsed)
        if repair_parsed.valid:
            self.repaired_batches += 1
            for item in repair_parsed.items:
                item["contract_path"] = "repaired"
            return repair_parsed.items

        self.invalid_batches += 1
        if len(records) > 1 and (request_budget is None or self.requests + len(records) <= request_budget):
            results: list[dict[str, Any]] = []
            for offset, record in enumerate(records, start=1):
                individual_id = f"{batch_id}-region-{offset:02d}"
                individual = self._send_review_request([record], glossary, individual_id, "individual_fallback")
                individual_parsed = self._parse_contract_response(individual.get("content", ""), [record], individual_id)
                self._write_raw_response(raw_dir, individual_id, "individual_fallback", [record], individual, individual_parsed)
                if individual_parsed.valid:
                    self.valid_batches += 1
                    self.fallback_individual += 1
                    for item in individual_parsed.items:
                        item["contract_path"] = "individual_fallback"
                    results.extend(individual_parsed.items)
                else:
                    results.extend(self._manual_reviews([record], "individual_contract_parse_failed", individual_parsed.categories))
            return results
        return self._manual_reviews(records, "batch_contract_parse_failed", sorted(set(parsed.categories + repair_parsed.categories)))

    def _send_review_request(
        self,
        records: list[dict[str, Any]],
        glossary: dict[str, Any],
        batch_id: str,
        purpose: str,
    ) -> dict[str, Any]:
        prompt_regions = [self._prompt_record(record) for record in records]
        payload = {
            "schema_version": REVIEW_SCHEMA_VERSION,
            "batch_id": batch_id,
            "target_language": "pt-BR",
            "review_goal": "naturalidade e fidelidade sem hardcode",
            "glossary": glossary,
            "regions": prompt_regions,
        }
        started = time.perf_counter()
        request_payload = {
            "model": self.model,
            "temperature": 0.0,
            "top_p": 0.2,
            "max_tokens": 4096,
            "nvext": {"guided_json": NVIDIA_REVIEW_JSON_SCHEMA},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Revise traducoes de quadrinhos de ingles para portugues brasileiro natural. "
                        "Responda apenas com um objeto JSON valido no schema fornecido, sem Markdown, "
                        "sem introducao e sem conclusao. Nao altere IDs, nao remova itens, nao acrescente "
                        "itens e nao ordene por preferencia. Use exatamente o batch_id recebido e exatamente "
                        "os region_id recebidos. Respeite text_type, preserve credit/watermark/decorative, "
                        "preserve nomes proprios e use o glossario. Escolha manual_review quando houver "
                        "duvida, OCR incerto ou risco alto. Nao invente texto ausente e nao corrija OCR sem evidencia."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
                ],
        }
        return self._post_chat_completion(request_payload, started, purpose)

    def _send_repair_request(
        self,
        records: list[dict[str, Any]],
        batch_id: str,
        invalid_response: str,
        failure: ReviewContractResult,
    ) -> dict[str, Any]:
        payload = {
            "schema_version": REVIEW_SCHEMA_VERSION,
            "batch_id": batch_id,
            "expected_region_ids": [str(item["region_id"]) for item in records],
            "validation_error": failure.error,
            "validation_categories": failure.categories,
            "invalid_response": str(invalid_response or "")[:16000],
            "schema": NVIDIA_REVIEW_JSON_SCHEMA,
        }
        started = time.perf_counter()
        request_payload = {
            "model": self.model,
            "temperature": 0.0,
            "top_p": 0.1,
            "max_tokens": 4096,
            "nvext": {"guided_json": NVIDIA_REVIEW_JSON_SCHEMA},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Converta somente o conteudo fornecido para o schema solicitado. "
                        "Nao reavalie, nao reescreva semanticamente e nao crie IDs. "
                        "Se nao for possivel preservar exatamente todos os IDs e campos, use manual_review."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        }
        return self._post_chat_completion(request_payload, started, "repair")

    def _post_chat_completion(self, request_payload: dict[str, Any], started: float, purpose: str) -> dict[str, Any]:
        self.requests += 1
        try:
            python_executable = self._subprocess_python_executable()
            completed = subprocess.run(
                [
                    python_executable,
                    "-c",
                    _NVIDIA_REVIEW_REQUEST_SCRIPT,
                ],
                input=json.dumps({
                    "url": self._chat_completions_url(),
                    "payload": request_payload,
                }, ensure_ascii=False),
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            if completed.returncode != 0:
                return {
                    "purpose": purpose,
                    "status_http": None,
                    "duration_seconds": time.perf_counter() - started,
                    "content": "",
                    "raw_body": "",
                    "finish_reason": None,
                    "provider_error": "nvidia_review_subprocess_failed",
                    "provider_error_detail": (completed.stderr or completed.stdout or "")[:1000],
                    "returncode": completed.returncode,
                }
            subprocess_payload = json.loads(completed.stdout or "{}")
            status_http = int(subprocess_payload.get("status_http") or 0)
            body = str(subprocess_payload.get("body") or "")
            if status_http in {401, 403, 429}:
                raise RuntimeError(f"nvidia_provider_stop_{status_http}")
            if not subprocess_payload.get("ok"):
                raw_error = str(subprocess_payload.get("error") or "provider_error")
                provider_error = "nvidia_review_timeout" if raw_error == "TimeoutError" else raw_error
                return {
                    "purpose": purpose,
                    "status_http": status_http,
                    "duration_seconds": subprocess_payload.get("duration_seconds"),
                    "content": "",
                    "raw_body": body,
                    "finish_reason": None,
                    "provider_error": provider_error,
                    "provider_error_detail": subprocess_payload.get("error_message") or "",
                }
            response_payload = json.loads(body or "{}")
        except subprocess.TimeoutExpired:
            return {
                "purpose": purpose,
                "status_http": None,
                "duration_seconds": time.perf_counter() - started,
                "content": "",
                "raw_body": "",
                "finish_reason": None,
                "provider_error": "nvidia_review_timeout",
            }
        except (OSError, ValueError, RuntimeError) as exc:
            return {
                "purpose": purpose,
                "status_http": None,
                "duration_seconds": time.perf_counter() - started,
                "content": "",
                "raw_body": "",
                "finish_reason": None,
                "provider_error": "nvidia_review_request_failed",
                "provider_error_detail": str(exc)[:500],
            }
        finally:
            self.duration_seconds += time.perf_counter() - started
        text = (((response_payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        finish_reason = ((response_payload.get("choices") or [{}])[0].get("finish_reason") or None)
        return {
            "purpose": purpose,
            "status_http": status_http,
            "duration_seconds": time.perf_counter() - started,
            "content": str(text or ""),
            "raw_body": json.dumps(response_payload, ensure_ascii=False),
            "finish_reason": finish_reason,
            "provider_error": None,
        }

    def _parse_contract_response(self, text: str, records: list[dict[str, Any]], batch_id: str) -> ReviewContractResult:
        try:
            parsed, syntax_categories = self._loads_contract_json(text)
            items = self._validate_contract(parsed, records, batch_id)
            categories = syntax_categories or ["valid_json"]
            return ReviewContractResult(True, items, categories, "", {
                "returned_ids": [item["region_id"] for item in items],
                "missing_ids": [],
                "extra_ids": [],
            })
        except ReviewContractError as exc:
            return ReviewContractResult(False, [], exc.categories, exc.error, exc.diagnostics)

    @staticmethod
    def _loads_contract_json(text: str) -> tuple[Any, list[str]]:
        value = str(text or "").lstrip("\ufeff").strip()
        categories: list[str] = []
        if not value:
            raise ReviewContractError("empty_response", ["invalid_json"], {})
        fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", value, flags=re.IGNORECASE | re.DOTALL)
        if fence:
            value = fence.group(1).strip()
            categories.append("markdown_fence")
        spans = ContextualNvidiaReviewer._json_object_spans(value)
        if not spans:
            category = "wrong_root_type" if value.startswith("[") else "truncated_json" if value.startswith("{") else "invalid_json"
            raise ReviewContractError("json_object_not_found", [category], {"response_size": len(value)})
        if len(spans) > 1:
            raise ReviewContractError("multiple_json_objects", ["invalid_json"], {"objects": len(spans)})
        start, end = spans[0]
        if value[:start].strip():
            categories.append("prose_before_json")
        if value[end:].strip():
            categories.append("prose_after_json")
        candidate = value[start:end]
        try:
            return json.loads(candidate), categories or ["valid_json"]
        except ValueError as exc:
            category = "truncated_json" if candidate.count("{") != candidate.count("}") else "invalid_json"
            raise ReviewContractError("json_decode_failed", [category], {"error": str(exc)[:200]}) from exc

    @staticmethod
    def _json_object_spans(value: str) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        depth = 0
        start: int | None = None
        in_string = False
        escaped = False
        for index, char in enumerate(value):
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                if depth == 0:
                    start = index
                depth += 1
            elif char == "}":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start is not None:
                        spans.append((start, index + 1))
                        start = None
        return spans

    @staticmethod
    def _validate_contract(parsed: Any, records: list[dict[str, Any]], batch_id: str) -> list[dict[str, Any]]:
        expected = {str(item["region_id"]): item for item in records}
        diagnostics: dict[str, Any] = {"expected_ids": sorted(expected)}
        categories: list[str] = []
        if not isinstance(parsed, dict):
            raise ReviewContractError("root_must_be_object", ["wrong_root_type"], diagnostics)
        extra_root = sorted(set(parsed) - REVIEW_ENVELOPE_FIELDS)
        if extra_root:
            categories.append("wrong_field_names")
            diagnostics["additional_fields"] = extra_root
        if parsed.get("schema_version") != REVIEW_SCHEMA_VERSION:
            categories.append("wrong_field_names")
            diagnostics["schema_version"] = parsed.get("schema_version")
        if str(parsed.get("batch_id") or "") != str(batch_id):
            categories.append("wrong_field_names")
            diagnostics["batch_id"] = parsed.get("batch_id")
        results = parsed.get("results")
        if not isinstance(results, list):
            raise ReviewContractError("results_must_be_list", sorted(set(categories + ["wrong_root_type"])), diagnostics)
        returned: list[str] = []
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        duplicates: list[str] = []
        unknown: list[str] = []
        missing_fields: dict[str, list[str]] = {}
        additional_fields: dict[str, list[str]] = {}
        for index, entry in enumerate(results):
            if not isinstance(entry, dict):
                categories.append("wrong_root_type")
                continue
            rid = str(entry.get("region_id") or "")
            if not rid:
                categories.append("missing_region_id")
                rid = f"<missing:{index}>"
            returned.append(rid)
            if rid in seen:
                duplicates.append(rid)
            seen.add(rid)
            if rid and rid not in expected:
                unknown.append(rid)
            miss = sorted(REVIEW_RESULT_FIELDS - set(entry))
            if miss:
                missing_fields[rid] = miss
                categories.append("wrong_field_names")
            extra = sorted(set(entry) - REVIEW_RESULT_FIELDS)
            if extra:
                additional_fields[rid] = extra
                categories.append("wrong_field_names")
            action = str(entry.get("action") or "").lower()
            risk = str(entry.get("risk") or "").lower()
            reason = str(entry.get("reason_code") or "")
            revised = str(entry.get("revised_translation") or "")
            if action not in REVIEW_ACTIONS:
                categories.append("invalid_action")
            try:
                confidence = float(entry.get("confidence"))
            except (TypeError, ValueError):
                confidence = -1.0
            if confidence < 0.0 or confidence > 1.0:
                categories.append("invalid_confidence")
            if risk not in REVIEW_RISKS:
                categories.append("invalid_risk")
            if action == "rewrite" and not revised.strip():
                categories.append("empty_translation")
            if action == "rewrite" and rid in expected:
                source_text = str(expected[rid].get("source_text") or "").strip()
                if revised.strip() and revised.strip().casefold() == source_text.casefold() and not reason:
                    categories.append("source_text_copied_without_reason")
            if re.search(r"```|schema|json|region_id|batch_id", revised, re.IGNORECASE):
                categories.append("model_instruction_leak")
            if action in {"preserve_original", "manual_review"} and revised.strip():
                categories.append("empty_translation")
            if not isinstance(entry.get("terminology"), list):
                categories.append("wrong_field_names")
            normalized.append({
                "region_id": rid,
                "action": action,
                "revised_translation": revised,
                "reason_code": reason or "model_review",
                "confidence": max(0.0, min(1.0, confidence)),
                "risk": risk if risk in REVIEW_RISKS else "high",
                "terminology": entry.get("terminology") if isinstance(entry.get("terminology"), list) else [],
                "contract_path": "batch",
            })
        returned_set = set(returned)
        missing = sorted(set(expected) - returned_set)
        extra_ids = sorted(returned_set - set(expected))
        if duplicates:
            categories.append("duplicate_region_id")
        if unknown:
            categories.append("unknown_region_id")
        if missing:
            categories.append("missing_regions")
        if extra_ids:
            categories.append("extra_regions")
        diagnostics.update({
            "returned_ids": returned,
            "missing_ids": missing,
            "extra_ids": extra_ids,
            "duplicate_ids": duplicates,
            "unknown_ids": unknown,
            "missing_fields": missing_fields,
            "additional_fields": additional_fields,
        })
        if categories or set(expected) != returned_set:
            raise ReviewContractError("review_contract_validation_failed", sorted(set(categories or ["unknown"])), diagnostics)
        return normalized

    def _chat_completions_url(self) -> str:
        base = str(self.base_url or "").rstrip("/")
        return base if base.endswith("/chat/completions") else f"{base}/chat/completions"

    @staticmethod
    def _subprocess_python_executable() -> str:
        executable = Path(str(sys.executable or ""))
        if executable.name.lower() == "pythonw.exe":
            sibling = executable.with_name("python.exe")
            if sibling.is_file():
                return str(sibling)
        return str(executable or sys.executable)

    @staticmethod
    def _provider_error_categories(provider_error: str) -> list[str]:
        value = str(provider_error or "")
        if "timeout" in value.lower():
            return ["timeout"]
        return ["provider_error"]

    @staticmethod
    def review_items_from_parsed(parsed: Any) -> Any:
        items = parsed.get("reviews") if isinstance(parsed, dict) else parsed
        if isinstance(items, dict):
            return ContextualNvidiaReviewer.review_items_from_parsed(items)
        if items is None and isinstance(parsed, dict):
            if {"region_id", "action"}.intersection(parsed.keys()):
                return [parsed]
            keyed_items = []
            for key, value in parsed.items():
                if isinstance(value, dict):
                    keyed_items.append({"region_id": str(value.get("region_id") or key), **value})
                elif isinstance(value, str) and re.match(r"^p\d{3}:", str(key)):
                    keyed_items.append({
                        "region_id": str(key),
                        "action": "manual_review",
                        "revised_translation": value,
                        "reason_code": "non_contract_translation_only_response",
                        "confidence": 0.0,
                        "risk": "high",
                        "terminology": [],
                    })
            if keyed_items:
                items = keyed_items
        return items

    @staticmethod
    def _prompt_record(record: dict[str, Any]) -> dict[str, Any]:
        source = str(record.get("source_text") or "")
        current = str(record.get("current_translation") or "")
        limit = max(len(current), len(source), 40)
        return {
            "region_id": str(record.get("region_id") or ""),
            "page_id": str(record.get("page_id") or record.get("page") or ""),
            "source_text": source,
            "current_translation": current,
            "text_type": str(record.get("text_type") or "unknown"),
            "ocr_confidence": float(record.get("ocr_confidence") or record.get("confidence") or 0.0),
            "previous_context": str(record.get("previous_context") or ""),
            "next_context": str(record.get("next_context") or ""),
            "glossary": record.get("glossary") if isinstance(record.get("glossary"), dict) else {},
            "constraints": {
                "max_characters": int(record.get("max_characters") or min(220, max(40, int(limit * 1.35)))),
                "max_lines": int(record.get("max_lines") or 4),
            },
        }

    @staticmethod
    def _manual_reviews(records: list[dict[str, Any]], reason_code: str, categories: list[str] | None = None) -> list[dict[str, Any]]:
        return [
            {
                "region_id": str(item["region_id"]),
                "action": "manual_review",
                "revised_translation": "",
                "reason_code": reason_code,
                "confidence": 0.0,
                "risk": "high",
                "terminology": [],
                "contract_categories": sorted(set(categories or ["unknown"])),
                "contract_path": "manual_review",
            }
            for item in records
        ]

    @staticmethod
    def _write_raw_response(
        raw_response_dir: Path | None,
        batch_id: str,
        stage: str,
        records: list[dict[str, Any]],
        response: dict[str, Any],
        parsed: ReviewContractResult,
    ) -> None:
        if raw_response_dir is None:
            return
        raw_response_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "batch_id": batch_id,
            "stage": stage,
            "region_ids": [str(item.get("region_id") or "") for item in records],
            "status_http": response.get("status_http"),
            "duration_seconds": response.get("duration_seconds"),
            "response_size_bytes": len(str(response.get("content") or "").encode("utf-8")),
            "finish_reason": response.get("finish_reason"),
            "provider_error": response.get("provider_error"),
            "provider_error_detail": response.get("provider_error_detail") or "",
            "returncode": response.get("returncode"),
            "valid": parsed.valid,
            "categories": parsed.categories,
            "parse_error": parsed.error,
            "diagnostics": parsed.diagnostics,
            "raw_content": response.get("content") or "",
        }
        write_json(raw_response_dir / f"{batch_id}-{stage}.json", payload)


class ChapterQualityRevision:
    def __init__(
        self,
        output_dir: str | Path,
        *,
        job_id: str,
        run_id: str,
        reviewer_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.job_id = str(job_id or "")
        self.run_id = str(run_id or "")
        self.reviewer_factory = reviewer_factory or ContextualNvidiaReviewer

    def latest_status(self) -> dict[str, Any] | None:
        root = self.output_dir / "quality_revision"
        latest = root / "latest_revision.json"
        if not latest.is_file():
            return None
        pointer = read_json(latest, {})
        manifest = Path(str(pointer.get("manifest_path") or ""))
        if manifest.is_file():
            return read_json(manifest, {})
        return None

    def start(self, *, max_iterations: int = 3) -> dict[str, Any]:
        progress = read_json(self.output_dir / "progress.json", {})
        quality = read_json(self.output_dir / "quality_report.json", {})
        if not isinstance(progress, dict) or not progress.get("pages"):
            raise ValueError("revision_progress_missing")
        if not isinstance(quality, dict) or len(quality.get("pages") or []) == 0:
            raise ValueError("revision_quality_report_missing")
        source_pdf = self._current_pdf_path(progress, quality)
        if not source_pdf.is_file():
            raise ValueError("revision_source_pdf_missing")
        revision_id = uuid.uuid4().hex
        paths = self._paths(revision_id)
        manifest = self._initial_manifest(revision_id, source_pdf, max_iterations)
        write_json(paths.manifest, manifest)
        write_json(self.output_dir / "quality_revision" / "latest_revision.json", {
            "revision_id": revision_id,
            "manifest_path": str(paths.manifest),
            "updated_at": utc_now(),
        })
        try:
            return self._run(progress, quality, source_pdf, paths, manifest)
        except Exception as exc:  # noqa: BLE001 - persist failed revision state
            manifest.update({
                "status": "failed",
                "phase": "failed",
                "error_code": type(exc).__name__,
                "error_message": str(exc),
                "updated_at": utc_now(),
            })
            write_json(paths.manifest, manifest)
            raise

    def start_canary(self, *, max_regions: int = 10) -> dict[str, Any]:
        progress = read_json(self.output_dir / "progress.json", {})
        quality = read_json(self.output_dir / "quality_report.json", {})
        if not isinstance(progress, dict) or not progress.get("pages"):
            raise ValueError("revision_progress_missing")
        if not isinstance(quality, dict) or len(quality.get("pages") or []) == 0:
            raise ValueError("revision_quality_report_missing")
        source_pdf = self._current_pdf_path(progress, quality)
        if not source_pdf.is_file():
            raise ValueError("revision_source_pdf_missing")
        revision_id = uuid.uuid4().hex
        paths = self._paths(revision_id)
        manifest = self._initial_manifest(revision_id, source_pdf, 1)
        manifest.update({
            "revision_type": "contract_canary",
            "phase": "contextual_translation_review",
            "phase_label": "Testando contrato NVIDIA",
            "canary_max_regions": max(1, min(10, int(max_regions or 10))),
        })
        write_json(paths.manifest, manifest)
        write_json(self.output_dir / "quality_revision" / "latest_revision.json", {
            "revision_id": revision_id,
            "manifest_path": str(paths.manifest),
            "updated_at": utc_now(),
        })
        try:
            valid_page_numbers = {
                page_number(page)
                for page in (quality.get("pages") or [])
                if isinstance(page, dict) and page_number(page) > 0
            }
            pages = [
                page for page in (progress.get("pages") or [])
                if isinstance(page, dict)
                and page.get("debug_data")
                and (not valid_page_numbers or page_number(page) in valid_page_numbers)
            ]
            regions = self._collect_regions(pages)
            manifest["total_pages"] = len(pages)
            manifest["total_regions"] = len(regions)
            glossary = self._build_glossary(regions)
            write_json(paths.glossary, glossary)
            contextual = self._review_translations(
                regions,
                glossary,
                manifest,
                paths,
                canary=True,
                max_regions=manifest["canary_max_regions"],
            )
            write_json(paths.contextual_review, contextual)
            reviewed = max(1, int(contextual.get("reviewed_regions") or 0))
            structurally_valid_regions = sum(
                1 for item in contextual.get("reviews", [])
                if str(item.get("contract_path") or "") in {"batch", "repaired", "individual_fallback"}
            )
            validity_rate = structurally_valid_regions / reviewed
            ids_ok = all(str(item.get("region_id") or "") for item in contextual.get("reviews", []))
            passed = validity_rate >= 0.90 and ids_ok
            manifest.update({
                "status": "contract_canary_passed" if passed else "contract_canary_failed",
                "phase": "finalized",
                "phase_label": "Contrato NVIDIA aprovado" if passed else "Contrato NVIDIA requer correção",
                "reviewed_regions": int(contextual.get("reviewed_regions") or 0),
                "canary_region_ids": [item.get("region_id") for item in contextual.get("reviews", [])],
                "validity_rate": validity_rate,
                "manual_review": int(contextual.get("manual_review") or 0),
                "safe_changes_applied": 0,
                "no_reviewed_pdf_reason": "contract_canary_only",
                "updated_at": utc_now(),
            })
            write_json(paths.manifest, manifest)
            return manifest
        except Exception as exc:  # noqa: BLE001 - persist failed canary state
            manifest.update({
                "status": "failed",
                "phase": "failed",
                "error_code": type(exc).__name__,
                "error_message": str(exc),
                "updated_at": utc_now(),
            })
            write_json(paths.manifest, manifest)
            raise

    def _paths(self, revision_id: str) -> RevisionPaths:
        root = self.output_dir / "quality_revision" / revision_id
        return RevisionPaths(
            root=root,
            manifest=root / "revision_manifest.json",
            page_audit=root / "page_audit.json",
            contextual_review=root / "contextual_translation_review.json",
            glossary=root / "chapter_glossary.json",
            visual_inspection=root / "visual_inspection.json",
            render_audit=root / "incremental_render_audit.json",
            checkpoint=root / "nvidia_revision_checkpoint.json",
            raw_responses=root / "nvidia_revision" / "raw-responses",
        )

    def _initial_manifest(self, revision_id: str, source_pdf: Path, max_iterations: int) -> dict[str, Any]:
        return {
            "revision_id": revision_id,
            "parent_job_id": self.job_id,
            "parent_run_id": self.run_id,
            "revision_iteration": 0,
            "status": "running",
            "phase": "preparing",
            "phase_label": "Preparando revisão",
            "current_page": 0,
            "current_region": "",
            "total_pages": 0,
            "total_regions": 0,
            "max_iterations": max(1, min(3, int(max_iterations or 3))),
            "source_pdf_path": str(source_pdf),
            "source_pdf_sha256": self._sha256(source_pdf),
            "reviewed_pdf_path": "",
            "reviewed_pdf_sha256": "",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "requests": 0,
            "model": "",
            "safe_changes_applied": 0,
            "manual_review": 0,
            "publication_created": False,
        }

    def _run(
        self,
        progress: dict[str, Any],
        quality: dict[str, Any],
        source_pdf: Path,
        paths: RevisionPaths,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        valid_page_numbers = {
            page_number(page)
            for page in (quality.get("pages") or [])
            if isinstance(page, dict) and page_number(page) > 0
        }
        pages = [
            page for page in (progress.get("pages") or [])
            if isinstance(page, dict)
            and page.get("debug_data")
            and (not valid_page_numbers or page_number(page) in valid_page_numbers)
        ]
        manifest["total_pages"] = len(pages)
        self._checkpoint(paths, manifest, "inspecting_pages", "Inspecionando páginas")
        regions = self._collect_regions(pages)
        manifest["total_regions"] = len(regions)
        page_audit = self._audit_pages(pages, regions)
        write_json(paths.page_audit, page_audit)

        self._checkpoint(paths, manifest, "detecting_missing_text", "Procurando textos não detectados")
        missing_text_candidates = self._detect_missing_text_candidates(quality)

        self._checkpoint(paths, manifest, "revalidating_ocr", "Revalidando OCR duvidoso")
        ocr_rechecks = self._planned_ocr_rechecks(regions)

        self._checkpoint(paths, manifest, "contextual_translation_review", "Revisando traduções com contexto")
        glossary = self._build_glossary(regions)
        write_json(paths.glossary, glossary)
        contextual = self._review_translations(regions, glossary, manifest, paths)
        write_json(paths.contextual_review, contextual)

        self._checkpoint(paths, manifest, "terminology_validation", "Validando terminologia")
        applicable = self._select_safe_changes(regions, contextual.get("reviews", []))

        self._checkpoint(paths, manifest, "layout_validation", "Verificando layout")
        manifest["manual_review"] = sum(1 for item in contextual.get("reviews", []) if item.get("action") == "manual_review")

        self._checkpoint(paths, manifest, "applying_fixes", "Aplicando correções seguras")
        changed_by_page = self._apply_safe_changes_to_pages(pages, applicable)

        self._checkpoint(paths, manifest, "incremental_render", "Redesenhando regiões alteradas")
        rendered_images, render_audit = self._render_changed_pages(pages, changed_by_page)
        render_audit["missing_text_candidates"] = missing_text_candidates
        render_audit["ocr_rechecks"] = ocr_rechecks
        write_json(paths.render_audit, render_audit)

        self._checkpoint(paths, manifest, "pdf_generation", "Gerando PDF revisado")
        if changed_by_page:
            reviewed_pdf = self._next_reviewed_pdf_path(source_pdf)
            generate_pdf(rendered_images, str(reviewed_pdf))
            manifest["reviewed_pdf_path"] = str(reviewed_pdf)
            manifest["reviewed_pdf_sha256"] = self._sha256(reviewed_pdf)
            self._checkpoint(paths, manifest, "pdf_inspection", "Inspecionando o novo PDF")
            visual = self._inspect_pdf_pages(reviewed_pdf, rendered_images, expected_pages=len(pages))
        else:
            manifest["reviewed_pdf_path"] = ""
            manifest["reviewed_pdf_sha256"] = ""
            manifest["no_reviewed_pdf_reason"] = "no_safe_changes_applied"
            visual = {
                "pdf_path": "",
                "pdf_exists": False,
                "pages_inspected": 0,
                "expected_pages": len(pages),
                "reason_code": "no_safe_changes_applied",
                "created_at": utc_now(),
            }
        write_json(paths.visual_inspection, visual)

        status = "review_required" if manifest["manual_review"] or not contextual.get("quality_passed") else "finished"
        manifest.update({
            "status": status,
            "phase": "finalized",
            "phase_label": "Finalizado" if status == "finished" else "Revisão humana necessária",
            "revision_iteration": 1,
            "safe_changes_applied": sum(len(v) for v in changed_by_page.values()),
            "pages_changed": sorted(int(page) for page in changed_by_page),
            "regions_changed": [item["region_id"] for items in changed_by_page.values() for item in items],
            "visual_pages_inspected": visual.get("pages_inspected", 0),
            "updated_at": utc_now(),
        })
        write_json(paths.manifest, manifest)
        return manifest

    def _checkpoint(self, paths: RevisionPaths, manifest: dict[str, Any], phase: str, label: str) -> None:
        manifest.update({"phase": phase, "phase_label": label, "updated_at": utc_now()})
        write_json(paths.manifest, manifest)

    def _collect_regions(self, pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        regions: list[dict[str, Any]] = []
        for page in pages:
            number = page_number(page)
            items = page.get("debug_data", {}).get("items") or []
            for offset, item in enumerate(items):
                if not isinstance(item, dict) or item.get("ignored"):
                    continue
                region_id = stable_region_key(number, item)
                classification = str(item.get("classification") or "unknown").lower()
                source = item_text(item)
                current = item_translation(item)
                regions.append({
                    "region_id": region_id,
                    "page": number,
                    "order": offset,
                    "source_text": source,
                    "current_translation": current,
                    "text_type": self._text_type(classification),
                    "classification": classification,
                    "confidence": float(item.get("confidence") or 0.0),
                    "quality_reasons": item.get("quality_reasons") if isinstance(item.get("quality_reasons"), list) else [],
                    "terminal_state": str(item.get("translation_final_state") or ""),
                    "terminal_reason": str(item.get("translation_final_reason") or ""),
                    "sent_to_translation": bool(item.get("sent_to_nvidia") or item.get("sent_to_translation")),
                    "redrawn": bool(item.get("redrawn")),
                    "preserved_original": bool(item.get("preserved_original")),
                    "manual_review_required": bool(item.get("manual_review_required")),
                    "bounding_box": item.get("bounding_box"),
                    "translation_box": item.get("translation_box"),
                    "raw_item": item,
                })
        return regions

    def _audit_pages(self, pages: list[dict[str, Any]], regions: list[dict[str, Any]]) -> dict[str, Any]:
        by_page: dict[int, list[dict[str, Any]]] = {}
        for region in regions:
            by_page.setdefault(int(region["page"]), []).append(region)
        page_records = []
        for page in pages:
            number = page_number(page)
            items = by_page.get(number, [])
            issues = []
            for item in items:
                if item["quality_reasons"]:
                    issues.extend(item["quality_reasons"])
                if item["manual_review_required"]:
                    issues.append("manual_review_required")
                if item["classification"] in TRANSLATABLE_CLASSES and looks_like_source_english(item["current_translation"]):
                    issues.append("source_language_residual")
                if looks_like_mojibake(item["current_translation"]):
                    issues.append("mojibake")
            unique_issues = sorted(set(str(issue) for issue in issues if issue))
            page_records.append({
                "page": number,
                "issues": unique_issues,
                "regions_checked": len(items),
                "regions_changed": 0,
                "visual_status": "fail" if any(issue in unique_issues for issue in ("mojibake", "source_language_residual")) else ("warning" if unique_issues else "pass"),
            })
        return {"pages": page_records, "created_at": utc_now()}

    def _detect_missing_text_candidates(self, quality: dict[str, Any]) -> list[dict[str, Any]]:
        details = (((quality.get("summary") or {}).get("quality_validation") or {}).get("smart_split_details") or [])
        result = []
        for detail in details:
            if isinstance(detail, dict) and detail.get("requires_review"):
                result.append({
                    "page": detail.get("page"),
                    "reason_code": "smart_split_requires_visual_review",
                    "decision": "manual_review",
                    "evidence": {k: detail.get(k) for k in ("safe_band", "band_score", "reason") if k in detail},
                })
        return result

    def _planned_ocr_rechecks(self, regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for region in regions:
            reasons = set(str(item) for item in region.get("quality_reasons") or [])
            if region["confidence"] < 0.75 or reasons.intersection({"improbable_tokens", "alphanumeric_ocr_artifact", "long_consonant_run"}):
                result.append({
                    "region_id": region["region_id"],
                    "page": region["page"],
                    "engines": ["rapidocr", "paddle"],
                    "decision": "manual_review",
                    "reason_code": "ocr_uncertain_requires_targeted_recheck",
                })
        return result

    def _build_glossary(self, regions: list[dict[str, Any]]) -> dict[str, Any]:
        candidates: dict[str, int] = {}
        for region in regions:
            for token in re.findall(r"\b[A-Z][A-Za-z][A-Za-z'-]{1,}\b", region["source_text"]):
                if token.lower() in {"The", "This", "That", "And", "But", "For"}:
                    continue
                candidates[token] = candidates.get(token, 0) + 1
        terms = [
            {"term": term, "category": "name_or_term", "count": count, "policy": "preserve_until_reviewed"}
            for term, count in sorted(candidates.items(), key=lambda item: (-item[1], item[0].casefold()))
            if count >= 2
        ]
        return {"terms": terms, "created_at": utc_now(), "editable": True}

    def _review_translations(
        self,
        regions: list[dict[str, Any]],
        glossary: dict[str, Any],
        manifest: dict[str, Any],
        paths: RevisionPaths,
        *,
        canary: bool = False,
        max_regions: int | None = None,
    ) -> dict[str, Any]:
        reviewable = [
            self._review_record(regions, idx)
            for idx, region in enumerate(regions)
            if self._is_reviewable(region)
        ]
        if canary:
            reviewable = self._select_canary_records(reviewable, max_regions=max_regions or 10)
        reviewer = self.reviewer_factory()
        model = getattr(reviewer, "model", "unknown")
        manifest["model"] = model
        reviews: list[dict[str, Any]] = []
        batch_size = 6
        for start in range(0, len(reviewable), batch_size):
            batch = reviewable[start:start + batch_size]
            batch_id = f"{manifest.get('revision_id', 'revision')}-batch-{(start // batch_size) + 1:03d}"
            try:
                raw = reviewer.review_batch(
                    batch,
                    glossary,
                    batch_id=batch_id,
                    raw_response_dir=paths.raw_responses,
                    request_budget=max(1, len(reviewable) * 3),
                )
            except TypeError:
                raw = reviewer.review_batch(batch, glossary)
            normalized = self._normalize_reviews(batch, raw)
            reviews.extend(normalized)
            manifest["requests"] = int(getattr(reviewer, "requests", manifest.get("requests", 0) or 0))
            checkpoint = {
                "revision_id": manifest.get("revision_id"),
                "batch_id": batch_id,
                "region_ids_completed": [item["region_id"] for item in reviews],
                "valid": int(getattr(reviewer, "valid_batches", 0)),
                "repaired": int(getattr(reviewer, "repaired_batches", 0)),
                "individual": int(getattr(reviewer, "fallback_individual", 0)),
                "invalid": int(getattr(reviewer, "invalid_batches", 0)),
                "applicable": sum(1 for item in reviews if item.get("action") == "rewrite" and item.get("risk") == "low"),
                "manual": sum(1 for item in reviews if item.get("action") == "manual_review"),
                "requests_used": int(getattr(reviewer, "requests", 0)),
                "last_error": "",
                "updated_at": utc_now(),
            }
            write_json(paths.checkpoint, checkpoint)
        manifest["requests"] = int(getattr(reviewer, "requests", manifest.get("requests", 0) or 0))
        blocked = sum(1 for item in reviews if item.get("action") == "manual_review")
        residual = sum(1 for item in reviews if item.get("reason_code") == "source_language_residual")
        valid_batches = int(getattr(reviewer, "valid_batches", 0))
        repaired_batches = int(getattr(reviewer, "repaired_batches", 0))
        fallback_individual = int(getattr(reviewer, "fallback_individual", 0))
        invalid_batches = int(getattr(reviewer, "invalid_batches", 0))
        manifest["valid_response_batches"] = valid_batches
        manifest["repaired_batches"] = repaired_batches
        manifest["fallback_individual_requests"] = fallback_individual
        manifest["invalid_response_batches"] = invalid_batches
        return {
            "model": model,
            "requests": manifest["requests"],
            "reviewed_regions": len(reviewable),
            "reviews": reviews,
            "manual_review": blocked,
            "source_language_residual": residual,
            "valid_response_batches": valid_batches,
            "repaired_batches": repaired_batches,
            "fallback_individual_requests": fallback_individual,
            "invalid_response_batches": invalid_batches,
            "quality_passed": blocked == 0 and residual == 0,
            "created_at": utc_now(),
        }

    def _review_record(self, regions: list[dict[str, Any]], idx: int) -> dict[str, Any]:
        region = regions[idx]
        previous_region = regions[idx - 1] if idx > 0 else {}
        next_region = regions[idx + 1] if idx + 1 < len(regions) else {}
        return {
            "page_id": str(region["page"]),
            "region_id": region["region_id"],
            "source_text": region["source_text"],
            "current_translation": region["current_translation"],
            "text_type": region["text_type"],
            "ocr_confidence": region.get("confidence", 0.0),
            "previous_context": previous_region.get("current_translation") or previous_region.get("source_text") or "",
            "next_context": next_region.get("current_translation") or next_region.get("source_text") or "",
            "quality_reasons": region.get("quality_reasons") or [],
        }

    def _select_canary_records(self, records: list[dict[str, Any]], *, max_regions: int = 10) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(items: list[dict[str, Any]], limit: int) -> None:
            for item in items:
                rid = str(item.get("region_id") or "")
                if rid and rid not in seen and len(selected) < max_regions and limit > 0:
                    selected.append(item)
                    seen.add(rid)
                    limit -= 1

        residual = [item for item in records if looks_like_source_english(item.get("current_translation", ""))]
        long_items = sorted(records, key=lambda item: len(str(item.get("current_translation") or item.get("source_text") or "")), reverse=True)
        short_items = sorted(records, key=lambda item: len(str(item.get("current_translation") or item.get("source_text") or "")))
        low_conf = sorted(records, key=lambda item: float(item.get("ocr_confidence") or 0.0))
        by_type: dict[str, list[dict[str, Any]]] = {}
        for item in records:
            by_type.setdefault(str(item.get("text_type") or "unknown"), []).append(item)
        add(residual, 2)
        add(long_items, 2)
        add(short_items, 2)
        add(low_conf, 2)
        for text_type in sorted(by_type):
            add(by_type[text_type], 1)
            if len(selected) >= max_regions:
                break
        add(records, max_regions - len(selected))
        return selected[:max_regions]

    def _normalize_reviews(self, batch: list[dict[str, Any]], raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        expected = {item["region_id"]: item for item in batch}
        by_id = {str(item.get("region_id") or ""): item for item in raw if isinstance(item, dict)}
        result = []
        for region_id, source in expected.items():
            item = by_id.get(region_id) or {}
            action = str(item.get("action") or "manual_review").lower()
            if action not in {"keep", "rewrite", "preserve_original", "manual_review"}:
                action = "manual_review"
            revised = str(item.get("revised_translation") or "")
            risk = str(item.get("risk") or "high").lower()
            if risk not in {"low", "medium", "high"}:
                risk = "high"
            confidence = float(item.get("confidence") or 0.0)
            reason = str(item.get("reason_code") or "model_review")
            if action == "rewrite" and not target_text_is_safe(revised):
                action = "manual_review"
                reason = "unsafe_rewrite_candidate"
                risk = "high"
            if looks_like_source_english(source.get("current_translation", "")) and action == "keep":
                action = "manual_review"
                reason = "source_language_residual"
                risk = "high"
            result.append({
                "region_id": region_id,
                "action": action,
                "revised_translation": revised,
                "reason_code": reason,
                "confidence": confidence,
                "risk": risk,
                "terminology": item.get("terminology") if isinstance(item.get("terminology"), list) else [],
                "contract_path": str(item.get("contract_path") or "batch"),
                "contract_categories": item.get("contract_categories") if isinstance(item.get("contract_categories"), list) else [],
            })
        return result

    def _select_safe_changes(self, regions: list[dict[str, Any]], reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_region = {region["region_id"]: region for region in regions}
        safe = []
        for review in reviews:
            region = by_region.get(str(review.get("region_id") or ""))
            if not region:
                continue
            if review.get("action") != "rewrite":
                continue
            if review.get("risk") != "low" or float(review.get("confidence") or 0.0) < 0.95:
                continue
            if not region.get("redrawn") or region.get("preserved_original"):
                continue
            safe.append({
                "region_id": region["region_id"],
                "page": region["page"],
                "previous_translation": region["current_translation"],
                "revised_translation": review["revised_translation"],
                "reason_code": review["reason_code"],
            })
        return safe

    def _apply_safe_changes_to_pages(self, pages: list[dict[str, Any]], changes: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
        by_region = {change["region_id"]: change for change in changes}
        changed_by_page: dict[int, list[dict[str, Any]]] = {}
        for page in pages:
            number = page_number(page)
            for item in page.get("debug_data", {}).get("items") or []:
                if not isinstance(item, dict):
                    continue
                rid = stable_region_key(number, item)
                change = by_region.get(rid)
                if not change:
                    continue
                item["translation"] = change["revised_translation"]
                item["translation_candidate"] = change["revised_translation"]
                item["translation_final_reason"] = change["reason_code"]
                changed_by_page.setdefault(number, []).append(change)
        return changed_by_page

    def _render_changed_pages(self, pages: list[dict[str, Any]], changed_by_page: dict[int, list[dict[str, Any]]]) -> tuple[list[str], dict[str, Any]]:
        review_pages = self.output_dir / "quality_revision_pages"
        review_pages.mkdir(parents=True, exist_ok=True)
        rendered_paths: list[str] = []
        render_records = []
        for page in pages:
            number = page_number(page)
            current_output = Path(str(page.get("output_path") or ""))
            if number not in changed_by_page:
                rendered_paths.append(str(current_output))
                continue
            image_path = Path(str(page.get("image_path") or page.get("debug_data", {}).get("image_path") or ""))
            original = cv2.imread(str(image_path))
            if original is None:
                rendered_paths.append(str(current_output))
                render_records.append({"page": number, "status": "warning", "reason_code": "source_image_missing"})
                continue
            groups = self._groups_from_page_items(page)
            final, debug_data = render_analyzed_image(
                original,
                [],
                [],
                groups,
                font_path=config.FONT_PATH or None,
                page_index=number,
                image_path=str(image_path),
            )
            target = review_pages / f"page_{number:03d}.jpg"
            cv2.imwrite(str(target), final)
            rendered_paths.append(str(target))
            render_records.append({
                "page": number,
                "status": "rendered",
                "changed_regions": [item["region_id"] for item in changed_by_page[number]],
                "redrawn_groups": debug_data.get("redrawn_group_count"),
            })
        return rendered_paths, {
            "pages_rerendered": len(changed_by_page),
            "regions_rerendered": sum(len(items) for items in changed_by_page.values()),
            "records": render_records,
            "created_at": utc_now(),
        }

    def _groups_from_page_items(self, page: dict[str, Any]) -> list[TextGroup]:
        number = page_number(page)
        groups: list[TextGroup] = []
        for item in page.get("debug_data", {}).get("items") or []:
            if not isinstance(item, dict) or item.get("ignored"):
                continue
            box = tuple(int(v) for v in (item.get("bounding_box") or [0, 0, 1, 1])[:4])
            polygon = np.array([[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]], dtype=np.float32)
            line = OCRLine(
                text=item_text(item),
                confidence=float(item.get("confidence") or 0.0),
                polygon=polygon,
                box=box,
                raw_text=str(item.get("raw_text") or item_text(item)),
                engine=str(item.get("engine") or item.get("source_engine") or ""),
                page=number,
                metadata=item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
                original_text=str(item.get("original_text") or item_text(item)),
                repaired_text=str(item.get("repaired_text") or item_text(item)),
                repair_reason=str(item.get("repair_reason") or ""),
            )
            group = TextGroup(group_id=str(item.get("id") or item.get("region_id") or "BALAO"))
            group.lines = [line]
            group.cleanup_lines = [line]
            group.text = item_text(item)
            group.translation = item_translation(item)
            group.sent_to_translation = bool(item.get("sent_to_nvidia") or item.get("sent_to_translation"))
            group.classification = str(item.get("classification") or "unknown")
            group.region_id = str(item.get("region_id") or "")
            group.source_engine = str(item.get("source_engine") or item.get("engine") or "")
            group.background_type = str(item.get("background_type") or "unknown")
            group.background_metrics = dict(item.get("background_metrics") or {})
            group.quality_reasons = list(item.get("quality_reasons") or [])
            group.translation_valid = bool(item.get("translation_valid", True))
            group.translation_retry_count = int(item.get("translation_retry_count") or 0)
            group.translation_validation_reason = str(item.get("translation_validation_reason") or "")
            group.translation_candidate = str(item.get("translation_candidate") or "")
            group.translation_final_state = str(item.get("translation_final_state") or "")
            group.translation_final_reason = str(item.get("translation_final_reason") or "")
            group.preserved_original = bool(item.get("preserved_original"))
            group.text_overflow_ratio = float(item.get("text_overflow_ratio") or 0.0)
            group.safe_area = tuple(item.get("safe_area") or ()) or None
            group.translation_box = tuple(item.get("translation_box") or ()) or None
            group.allowed_modification_box = tuple(item.get("allowed_modification_box") or ()) or None
            group.visual_validation = dict(item.get("visual_validation") or {})
            group.manual_review_required = bool(item.get("manual_review_required"))
            groups.append(group)
        return groups

    def _inspect_pdf_pages(self, pdf_path: Path, rendered_images: list[str], expected_pages: int) -> dict[str, Any]:
        page_records = []
        for index, path_text in enumerate(rendered_images, start=1):
            path = Path(path_text)
            status = "pass" if path.is_file() and path.stat().st_size > 1024 else "fail"
            page_records.append({"page": index, "visual_status": status, "image_path": str(path)})
        return {
            "pdf_path": str(pdf_path),
            "pdf_exists": pdf_path.is_file(),
            "pages_inspected": len(page_records),
            "expected_pages": expected_pages,
            "pages": page_records,
            "created_at": utc_now(),
        }

    def _next_reviewed_pdf_path(self, source_pdf: Path) -> Path:
        source_stem = source_pdf.stem
        clean_stem = re.sub(r"_reviewed_v\d+$", "", source_stem)
        idx = 2
        while True:
            candidate = self.output_dir / f"{clean_stem}_reviewed_v{idx}.pdf"
            if not candidate.exists():
                return candidate
            idx += 1

    def _is_reviewable(self, region: dict[str, Any]) -> bool:
        classification = str(region.get("classification") or "unknown").lower()
        if classification in PRESERVED_CLASSES:
            return False
        if classification not in TRANSLATABLE_CLASSES:
            return False
        return bool(str(region.get("source_text") or "").strip())

    def _text_type(self, classification: str) -> str:
        if classification == "speech":
            return "dialogue"
        if classification in {"decorative", "sfx"}:
            return classification
        return classification or "unknown"

    def _current_pdf_path(self, progress: dict[str, Any], quality: dict[str, Any]) -> Path:
        for value in (
            progress.get("pdf_path"),
            (quality.get("summary") or {}).get("pdf_path"),
        ):
            if value:
                return Path(str(value))
        matches = sorted(self.output_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
        return matches[0] if matches else self.output_dir / "chapter.pdf"

    @staticmethod
    def _sha256(path: Path) -> str:
        import hashlib

        h = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest().upper()
