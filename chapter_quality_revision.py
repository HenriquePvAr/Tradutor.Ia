"""Persisted, UI-driven full-chapter quality revision.

This module intentionally works from existing pipeline artifacts.  It never downloads
chapter pages, opens a browser, publishes anything, or mutates the original PDF.  A
revision run is a separate audit/review workspace tied to a parent job/run.
"""

from __future__ import annotations

import hashlib
import json
import os
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
from process_options import hidden_console_options
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
# Bump whenever the review prompt changes meaning: cached answers produced by an
# older prompt must not be reused for a newer one.
REVIEW_PROMPT_VERSION = "2"
# Bump when the LOW/MEDIUM/HIGH application policy changes: an answer cached under
# a laxer policy must not be replayed under a stricter one.
REVIEW_RISK_POLICY_VERSION = "1"
# A concurrent reader on Windows can briefly block the atomic swap.
WRITE_JSON_REPLACE_ATTEMPTS = 6
WRITE_JSON_REPLACE_BACKOFF_SECONDS = 0.05
REVIEW_ACTIONS = {"keep", "rewrite", "preserve_original", "manual_review"}
REVIEW_RISKS = {"low", "medium", "high"}
# A revision is only advanced by a worker thread inside the UI process, so these
# states are meaningless once that thread is gone.
REVISION_IN_FLIGHT_STATUSES = frozenset({"queued", "running", "cancelling"})
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
# The integrate.api.nvidia.com endpoint ignores nvext.guided_json for this
# reasoning model but honors OpenAI-style response_format json_schema (strict),
# which forces exact field names and enum values without chain-of-thought prose.
NVIDIA_REVIEW_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "chapter_review",
        "strict": True,
        "schema": NVIDIA_REVIEW_JSON_SCHEMA,
    },
}
_NVIDIA_REVIEW_REQUEST_SCRIPT = r"""
import http.client
import json
import socket
import sys
import time
import urllib.parse

import config

request = json.loads(sys.stdin.read() or "{}")
url = str(request.get("url") or "")
payload = request.get("payload") or {}
connect_timeout = float(request.get("connect_timeout") or 10.0)
read_timeout = float(request.get("read_timeout") or 120.0)
body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
parsed = urllib.parse.urlparse(url)
path = parsed.path or "/"
if parsed.query:
    path = path + "?" + parsed.query
headers = {
    "Authorization": "Bearer " + str(config.NVIDIA_API_KEY or ""),
    "Content-Type": "application/json",
    "Content-Length": str(len(body_bytes)),
}
started = time.perf_counter()
timing = {
    "request_started_at": started,
    "connect_started_at": None,
    "connection_established_at": None,
    "request_upload_started_at": None,
    "request_upload_finished_at": None,
    "first_response_byte_at": None,
    "request_finished_at": None,
    "dns_ms": None,
    "connect_ms": None,
    "tls_ms": None,
    "request_upload_ms": None,
    "time_to_first_byte_ms": None,
    "response_read_ms": None,
    "total_ms": None,
    "timeout_phase": None,
}
conn = None
try:
    conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    timing["connect_started_at"] = time.perf_counter()
    conn = conn_cls(parsed.hostname, parsed.port, timeout=connect_timeout)
    conn.connect()
    timing["connection_established_at"] = time.perf_counter()
    timing["connect_ms"] = (timing["connection_established_at"] - timing["connect_started_at"]) * 1000.0
    if getattr(conn, "sock", None) is not None:
        conn.sock.settimeout(read_timeout)
    timing["request_upload_started_at"] = time.perf_counter()
    conn.request("POST", path, body=body_bytes, headers=headers)
    timing["request_upload_finished_at"] = time.perf_counter()
    timing["request_upload_ms"] = (timing["request_upload_finished_at"] - timing["request_upload_started_at"]) * 1000.0
    response = conn.getresponse()
    timing["first_response_byte_at"] = time.perf_counter()
    timing["time_to_first_byte_ms"] = (timing["first_response_byte_at"] - started) * 1000.0
    read_started = time.perf_counter()
    raw_body = response.read()
    timing["request_finished_at"] = time.perf_counter()
    timing["response_read_ms"] = (timing["request_finished_at"] - read_started) * 1000.0
    timing["total_ms"] = (timing["request_finished_at"] - started) * 1000.0
    body = raw_body.decode("utf-8", errors="replace")
    ok = 200 <= int(response.status) < 300
    sys.stdout.write(json.dumps({
        "ok": ok,
        "status_http": int(response.status),
        "duration_seconds": timing["total_ms"] / 1000.0,
        "body": body,
        "timing": timing,
        "error": "" if ok else "http_error",
    }, ensure_ascii=False))
    sys.exit(0)
except socket.timeout as exc:
    now = time.perf_counter()
    timing["request_finished_at"] = now
    timing["total_ms"] = (now - started) * 1000.0
    if timing["connection_established_at"] is None:
        timing["timeout_phase"] = "connect"
    elif timing["first_response_byte_at"] is None:
        timing["timeout_phase"] = "read"
    else:
        timing["timeout_phase"] = "read"
    sys.stdout.write(json.dumps({
        "ok": False,
        "status_http": None,
        "duration_seconds": timing["total_ms"] / 1000.0,
        "body": "",
        "timing": timing,
        "error": "timeout",
        "error_message": str(exc)[:500],
    }, ensure_ascii=False))
    sys.exit(0)
except Exception as exc:
    now = time.perf_counter()
    timing["request_finished_at"] = now
    timing["total_ms"] = (now - started) * 1000.0
    sys.stdout.write(json.dumps({
        "ok": False,
        "status_http": None,
        "duration_seconds": timing["total_ms"] / 1000.0,
        "body": "",
        "timing": timing,
        "error": type(exc).__name__,
        "error_message": str(exc)[:500],
    }, ensure_ascii=False))
    sys.exit(0)
finally:
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass
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
    # Windows refuses the atomic replace while a reader still holds the target
    # open, and the UI polls these files for live progress. A brief retry keeps
    # the swap atomic instead of losing a revision to a transient share error.
    for attempt in range(WRITE_JSON_REPLACE_ATTEMPTS):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            time.sleep(WRITE_JSON_REPLACE_BACKOFF_SECONDS * (attempt + 1))
    # Still blocked by a reader: write in place rather than lose the revision.
    # Readers already treat an unreadable file as "no data yet", so a rare torn
    # read is far cheaper than failing a run that has real work persisted.
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


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
        self.connect_timeout_seconds = float(getattr(config, "NVIDIA_CONNECT_TIMEOUT_SECONDS", 10.0) or 10.0)
        self.read_timeout_seconds = float(getattr(config, "NVIDIA_READ_TIMEOUT_SECONDS", 120.0) or 120.0)
        self.total_timeout_seconds = float(getattr(config, "NVIDIA_TOTAL_TIMEOUT_SECONDS", 150.0) or 150.0)
        self.region_timeout_seconds = float(getattr(config, "NVIDIA_REVISION_REGION_TIMEOUT_SECONDS", 120.0) or 120.0)
        self.diagnostic_mode = bool(getattr(config, "NVIDIA_REVISION_DIAGNOSTIC_MODE", False))

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
        diagnostic_mode: bool | None = None,
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
        diagnostic = self.diagnostic_mode if diagnostic_mode is None else bool(diagnostic_mode)
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
        if diagnostic:
            self.invalid_batches += 1
            return self._manual_reviews(records, "diagnostic_contract_parse_failed", parsed.categories)

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

    def health_check(self, *, raw_response_dir: str | Path | None = None, batch_id: str = "health-check") -> dict[str, Any]:
        started = time.perf_counter()
        request_payload = {
            "model": self.model,
            "temperature": 0.0,
            "top_p": 0.1,
            "max_tokens": 512,
            "response_format": NVIDIA_REVIEW_RESPONSE_FORMAT,
            "chat_template_kwargs": {"thinking": False},
            "messages": [
                {"role": "system", "content": "Return only the guided JSON object for this health check."},
                {"role": "user", "content": json.dumps({
                    "schema_version": REVIEW_SCHEMA_VERSION,
                    "batch_id": batch_id,
                    "results": [],
                }, ensure_ascii=False, separators=(",", ":"))},
            ],
        }
        response = self._post_chat_completion(request_payload, started, "health_check", request_meta={
            "batch_size": 0,
            "system_prompt_characters": len(request_payload["messages"][0]["content"]),
            "user_prompt_characters": len(request_payload["messages"][1]["content"]),
            "prompt_characters_before": len(request_payload["messages"][1]["content"]),
            "prompt_characters_after": len(json.dumps(request_payload["messages"], ensure_ascii=False, separators=(",", ":"))),
            "estimated_tokens_before": self._estimate_tokens(len(request_payload["messages"][1]["content"])),
            "estimated_tokens_after": self._estimate_tokens(len(json.dumps(request_payload["messages"], ensure_ascii=False, separators=(",", ":")))),
            "max_tokens": 512,
            "temperature": 0.0,
            "top_p": 0.1,
            "structured_output_enabled": True,
            "structured_output_method": "response_format.json_schema",
            "response_format": "json_schema",
            "nvext": False,
            "guided_json": True,
            "schema_sent": True,
            "streaming": False,
            "glossary_terms_total": 0,
            "glossary_terms_sent": 0,
            "timeout_config": self._timeout_config(),
        })
        if response.get("provider_error") and not response.get("content"):
            parsed = ReviewContractResult(False, [], self._provider_error_categories(str(response.get("provider_error") or "")), str(response.get("provider_error") or ""), {})
        else:
            parsed = self._parse_contract_response(response.get("content", ""), [], batch_id)
        self._write_raw_response(Path(raw_response_dir) if raw_response_dir else None, batch_id, "health_check", [], response, parsed)
        return {
            "ok": parsed.valid,
            "batch_id": batch_id,
            "status_http": response.get("status_http"),
            "duration_seconds": response.get("duration_seconds"),
            "timeout_phase": response.get("timeout_phase") or "",
            "provider_error": response.get("provider_error") or "",
            "categories": parsed.categories,
            "request_meta": response.get("request_meta") or {},
        }

    def _send_review_request(
        self,
        records: list[dict[str, Any]],
        glossary: dict[str, Any],
        batch_id: str,
        purpose: str,
    ) -> dict[str, Any]:
        prompt_characters_before = len(json.dumps({
            "schema_version": REVIEW_SCHEMA_VERSION,
            "batch_id": batch_id,
            "target_language": "pt-BR",
            "review_goal": "naturalidade e fidelidade sem hardcode",
            "glossary": glossary,
            "regions": records,
        }, ensure_ascii=False))
        prompt_regions = [self._prompt_record(record) for record in records]
        compact_glossary = self._compact_glossary(glossary, prompt_regions)
        payload = {
            "schema_version": REVIEW_SCHEMA_VERSION,
            "batch_id": batch_id,
            "target_language": "pt-BR",
            "glossary": compact_glossary,
            "regions": prompt_regions,
        }
        user_content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        started = time.perf_counter()
        request_payload = {
            "model": self.model,
            "temperature": 0.0,
            "top_p": 0.2,
            "max_tokens": self._max_tokens_for_records(records),
            "response_format": NVIDIA_REVIEW_RESPONSE_FORMAT,
            "chat_template_kwargs": {"thinking": False},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return only the guided JSON object. Use exact batch_id and region_id set. "
                        "Return exactly one result per input region: same count, same region_id set, "
                        "no omissions, no additions, no reordering; use keep when a region needs no change. "
                        "Actions: keep, rewrite, preserve_original, manual_review. "
                        "revised_translation must be the corrected pt-BR text (non-empty) only for rewrite; "
                        "for keep, preserve_original and manual_review set revised_translation to \"\". "
                        "Use natural pt-BR, preserve names/glossary, and choose manual_review when unsure. "
                        "No Markdown, no extra text, no invented OCR."
                    ),
                },
                {
                    "role": "user",
                    "content": user_content,
                },
                ],
        }
        prompt_characters_after = len(json.dumps({
            "messages": request_payload["messages"],
            "response_format": request_payload.get("response_format"),
        }, ensure_ascii=False, separators=(",", ":")))
        request_meta = {
            "batch_size": len(records),
            "system_prompt_characters": len(request_payload["messages"][0]["content"]),
            "user_prompt_characters": len(user_content),
            "prompt_characters_before": prompt_characters_before,
            "prompt_characters_after": prompt_characters_after,
            "estimated_tokens_before": self._estimate_tokens(prompt_characters_before),
            "estimated_tokens_after": self._estimate_tokens(prompt_characters_after),
            "max_tokens": request_payload["max_tokens"],
            "temperature": request_payload["temperature"],
            "top_p": request_payload["top_p"],
            "structured_output_enabled": True,
            "structured_output_method": "response_format.json_schema",
            "response_format": "json_schema",
            "nvext": False,
            "guided_json": True,
            "schema_sent": True,
            "streaming": False,
            "glossary_terms_total": len((glossary or {}).get("terms") or []),
            "glossary_terms_sent": len((compact_glossary or {}).get("terms") or []),
            "timeout_config": self._timeout_config(),
        }
        return self._post_chat_completion(request_payload, started, purpose, request_meta=request_meta)

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
            "response_format": NVIDIA_REVIEW_RESPONSE_FORMAT,
            "chat_template_kwargs": {"thinking": False},
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
        return self._post_chat_completion(request_payload, started, "repair", request_meta={
            "batch_size": len(records),
            "system_prompt_characters": len(request_payload["messages"][0]["content"]),
            "user_prompt_characters": len(request_payload["messages"][1]["content"]),
            "prompt_characters_before": len(json.dumps(payload, ensure_ascii=False)),
            "prompt_characters_after": len(json.dumps(request_payload["messages"], ensure_ascii=False)),
            "estimated_tokens_before": self._estimate_tokens(len(json.dumps(payload, ensure_ascii=False))),
            "estimated_tokens_after": self._estimate_tokens(len(json.dumps(request_payload["messages"], ensure_ascii=False))),
            "max_tokens": request_payload["max_tokens"],
            "temperature": request_payload["temperature"],
            "top_p": request_payload["top_p"],
            "structured_output_enabled": True,
            "structured_output_method": "response_format.json_schema",
            "response_format": "json_schema",
            "nvext": False,
            "guided_json": True,
            "schema_sent": True,
            "streaming": False,
            "glossary_terms_total": 0,
            "glossary_terms_sent": 0,
            "timeout_config": self._timeout_config(),
        })

    def _post_chat_completion(
        self,
        request_payload: dict[str, Any],
        started: float,
        purpose: str,
        *,
        request_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
                    "connect_timeout": self.connect_timeout_seconds,
                    "read_timeout": self.read_timeout_seconds,
                }, ensure_ascii=False),
                text=True,
                capture_output=True,
                timeout=max(self.total_timeout_seconds, self.read_timeout_seconds + 5.0),
                check=False,
                # The UI runs under pythonw, so a plain python.exe child would
                # flash a console per request. Output is still captured above.
                **hidden_console_options(),
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
                    "request_meta": request_meta or {},
                }
            subprocess_payload = json.loads(completed.stdout or "{}")
            status_http = int(subprocess_payload.get("status_http") or 0)
            body = str(subprocess_payload.get("body") or "")
            timing = subprocess_payload.get("timing") if isinstance(subprocess_payload.get("timing"), dict) else {}
            if status_http in {401, 403, 429}:
                raise RuntimeError(f"nvidia_provider_stop_{status_http}")
            if not subprocess_payload.get("ok"):
                raw_error = str(subprocess_payload.get("error") or "provider_error")
                provider_error = "nvidia_review_timeout" if raw_error in {"TimeoutError", "timeout"} else raw_error
                return {
                    "purpose": purpose,
                    "status_http": status_http,
                    "duration_seconds": subprocess_payload.get("duration_seconds"),
                    "content": "",
                    "raw_body": body,
                    "finish_reason": None,
                    "provider_error": provider_error,
                    "provider_error_detail": subprocess_payload.get("error_message") or "",
                    "timing": timing,
                    "timeout_phase": timing.get("timeout_phase") or ("read" if provider_error == "nvidia_review_timeout" else ""),
                    "request_meta": request_meta or {},
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
                "timeout_phase": "subprocess",
                "request_meta": request_meta or {},
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
                "timeout_phase": "",
                "request_meta": request_meta or {},
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
            "timing": timing,
            "timeout_phase": "",
            "request_meta": request_meta or {},
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
        result = {
            "region_id": str(record.get("region_id") or ""),
            "source_text": source,
            "current_translation": current,
            "text_type": str(record.get("text_type") or "unknown"),
            "previous_context": ContextualNvidiaReviewer._short_context(record.get("previous_context")),
            "next_context": ContextualNvidiaReviewer._short_context(record.get("next_context")),
            "constraints": {
                "max_characters": int(record.get("max_characters") or min(220, max(40, int(limit * 1.35)))),
                "max_lines": int(record.get("max_lines") or 4),
            },
        }
        confidence = float(record.get("ocr_confidence") or record.get("confidence") or 0.0)
        if confidence:
            result["ocr_confidence"] = round(confidence, 3)
        return result

    @staticmethod
    def _short_context(value: Any, *, limit: int = 160) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "…"

    @staticmethod
    def _compact_glossary(glossary: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
        terms = (glossary or {}).get("terms") if isinstance(glossary, dict) else []
        if not isinstance(terms, list):
            return {"terms": []}
        haystack = " ".join(
            str(record.get(key) or "")
            for record in records
            for key in ("source_text", "current_translation", "previous_context", "next_context")
        ).casefold()
        selected = []
        for term in terms:
            if not isinstance(term, dict):
                continue
            value = str(term.get("term") or "").strip()
            if value and value.casefold() in haystack:
                selected.append({
                    key: term.get(key)
                    for key in ("term", "category", "policy")
                    if term.get(key)
                })
            if len(selected) >= 12:
                break
        return {"terms": selected}

    @staticmethod
    def _estimate_tokens(characters: int) -> int:
        return max(1, int((int(characters or 0) + 3) / 4))

    @staticmethod
    def _max_tokens_for_records(records: list[dict[str, Any]]) -> int:
        return min(2048, max(640, 360 + 220 * max(1, len(records))))

    def _timeout_config(self) -> dict[str, float]:
        return {
            "connect_seconds": self.connect_timeout_seconds,
            "read_seconds": self.read_timeout_seconds,
            "total_seconds": self.total_timeout_seconds,
            "region_seconds": self.region_timeout_seconds,
            "subprocess_seconds": max(self.total_timeout_seconds, self.read_timeout_seconds + 5.0),
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
            "timeout_phase": response.get("timeout_phase") or "",
            "timing": response.get("timing") if isinstance(response.get("timing"), dict) else {},
            "request_meta": response.get("request_meta") if isinstance(response.get("request_meta"), dict) else {},
            "returncode": response.get("returncode"),
            "valid": parsed.valid,
            "categories": parsed.categories,
            "parse_error": parsed.error,
            "diagnostics": parsed.diagnostics,
            "raw_content": response.get("content") or "",
        }
        write_json(raw_response_dir / f"{batch_id}-{stage}.json", payload)


class RevisionResponseCache:
    """Content-addressed cache of validated contextual review answers.

    An answer is reused only when every input that can change it is identical:
    provider/model/endpoint, prompt and schema version, the region's own text, its
    surrounding context, the relevant glossary subset, the OCR text, the layout
    limits and the language pair.  Only hashes and the validated response are
    persisted -- never keys, tokens, headers or raw provider payloads.
    """

    CACHEABLE_ACTIONS = frozenset({"keep", "rewrite", "preserve_original"})

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.hits = 0
        self.misses = 0
        self.invalidations = 0
        self.invalidation_reasons: list[str] = []
        self._entries: dict[str, dict[str, Any]] = {}
        self._keys_by_region: dict[str, set[str]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        # A partially written or corrupted line must never break a revision: skip it.
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            key = str(entry.get("cache_key") or "")
            region_id = str(entry.get("region_id") or "")
            if not key or not region_id or not isinstance(entry.get("response"), dict):
                continue
            self._entries[key] = entry
            self._keys_by_region.setdefault(region_id, set()).add(key)

    @staticmethod
    def _hash(value: Any) -> str:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:32]

    def build_key(
        self,
        record: dict[str, Any],
        *,
        provider: str,
        model: str,
        endpoint: str,
        glossary: dict[str, Any],
        ocr_text: str = "",
        source_language: str = "en",
        target_language: str = "pt-BR",
    ) -> tuple[str, dict[str, str]]:
        constraints = record.get("constraints") if isinstance(record.get("constraints"), dict) else {}
        parts = {
            "provider": provider,
            "model": model,
            "endpoint": endpoint,
            "prompt_version": REVIEW_PROMPT_VERSION,
            "schema_version": REVIEW_SCHEMA_VERSION,
            "region_id": str(record.get("region_id") or ""),
            "text_type": str(record.get("text_type") or "unknown"),
            "source_language": source_language,
            "target_language": target_language,
            "max_characters": str(constraints.get("max_characters") or ""),
            "max_lines": str(constraints.get("max_lines") or ""),
            "risk_policy": REVIEW_RISK_POLICY_VERSION,
            "source_text": self._hash(record.get("source_text") or ""),
            "current_translation": self._hash(record.get("current_translation") or ""),
            "previous_context": self._hash(record.get("previous_context") or ""),
            "next_context": self._hash(record.get("next_context") or ""),
            "glossary": self._hash(glossary or {}),
            "ocr": self._hash(ocr_text or record.get("source_text") or ""),
        }
        return self._hash(parts), parts

    def lookup(self, cache_key: str, region_id: str) -> dict[str, Any] | None:
        entry = self._entries.get(cache_key)
        if entry:
            self.hits += 1
            entry["hit_count"] = int(entry.get("hit_count") or 0) + 1
            entry["last_used_at"] = utc_now()
            return dict(entry["response"])
        self.misses += 1
        # The region was cached before under a different key: an input changed.
        previous = self._keys_by_region.get(str(region_id))
        if previous:
            self.invalidations += 1
            self.invalidation_reasons.append("input_changed")
        return None

    def store(self, cache_key: str, region_id: str, review: dict[str, Any], input_hashes: dict[str, str]) -> bool:
        if str(review.get("action") or "") not in self.CACHEABLE_ACTIONS:
            # manual_review is a fail-closed outcome, never a reusable answer.
            return False
        entry = {
            "cache_key": cache_key,
            "region_id": str(region_id),
            "response": review,
            "response_hash": self._hash(review),
            "model": input_hashes.get("model", ""),
            "prompt_version": REVIEW_PROMPT_VERSION,
            "schema_version": REVIEW_SCHEMA_VERSION,
            "input_hashes": input_hashes,
            "status": "valid",
            "hit_count": 0,
            "created_at": utc_now(),
            "last_used_at": utc_now(),
        }
        self._entries[cache_key] = entry
        self._keys_by_region.setdefault(str(region_id), set()).add(cache_key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True

    def stats(self) -> dict[str, Any]:
        return {
            "cache_hits": self.hits,
            "cache_misses": self.misses,
            "cache_invalidations": self.invalidations,
            "cache_entries": len(self._entries),
            "provider_requests_avoided": self.hits,
        }


class ChapterQualityRevision:
    def __init__(
        self,
        output_dir: str | Path,
        *,
        job_id: str,
        run_id: str,
        reviewer_factory: Callable[[], Any] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.job_id = str(job_id or "")
        self.run_id = str(run_id or "")
        self.reviewer_factory = reviewer_factory or ContextualNvidiaReviewer
        # Cooperative cancellation: checked between regions so an in-flight
        # request finishes and its checkpoint is preserved before stopping.
        self.should_cancel = should_cancel or (lambda: False)

    def cancel_requested(self) -> bool:
        try:
            return bool(self.should_cancel())
        except Exception:  # noqa: BLE001 - a broken predicate must never abort a revision
            return False

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

    LIVE_PROGRESS_FIELDS = (
        "regions_total", "suspicious_regions", "skipped_unchanged_regions",
        "resumed_regions", "regions_completed", "regions_pending", "applied_low",
        "risk_counts", "elapsed_ms", "valid", "repaired", "individual", "invalid",
        "manual", "applicable", "last_error",
        "cache_hits", "cache_misses", "cache_invalidations", "cache_entries",
        "provider_requests_avoided",
    )

    def live_progress(self) -> dict[str, Any]:
        """Per-region counters written by the running review loop.

        The manifest is only rewritten on phase changes, so a revision in flight
        would otherwise report zero requests while it is actually working.
        """

        root = self.output_dir / "quality_revision"
        pointer = read_json(root / "latest_revision.json", {})
        manifest_path = Path(str(pointer.get("manifest_path") or ""))
        if not manifest_path.is_file():
            return {}
        checkpoint = read_json(manifest_path.parent / "nvidia_revision_checkpoint.json", {})
        if not isinstance(checkpoint, dict) or not checkpoint:
            return {}
        progress = {key: checkpoint[key] for key in self.LIVE_PROGRESS_FIELDS if key in checkpoint}
        requests_used = checkpoint.get("requests_used")
        if requests_used is not None:
            progress["requests"] = int(requests_used or 0)
        progress["checkpoint_updated_at"] = checkpoint.get("updated_at")
        return progress

    def resume_reviews(self) -> dict[str, dict[str, Any]]:
        """Answers already stored by a cancelled/interrupted revision.

        Keyed by region_id so a resume can skip exactly those regions. Returns
        an empty mapping when the last revision is not resumable, so a fresh
        revision never silently reuses stale answers.
        """

        root = self.output_dir / "quality_revision"
        pointer = read_json(root / "latest_revision.json", {})
        manifest_path = Path(str(pointer.get("manifest_path") or ""))
        if not manifest_path.is_file():
            return {}
        manifest = read_json(manifest_path, {})
        if str(manifest.get("status") or "") not in {"cancelled", "interrupted", "failed"}:
            return {}
        checkpoint = read_json(manifest_path.parent / "nvidia_revision_checkpoint.json", {})
        completed = checkpoint.get("completed_reviews")
        if not isinstance(completed, list):
            return {}
        return {
            str(item.get("region_id")): item
            for item in completed
            if isinstance(item, dict) and item.get("region_id")
        }

    def sweep_stale_revisions(self, *, keep_revision_id: str = "") -> list[str]:
        """Settle in-flight manifests left behind by earlier processes.

        ``mark_interrupted`` only heals the revision the "latest" pointer names, so
        older runs that died before being superseded stayed ``running`` forever.
        The live revision is passed in and skipped.
        """

        healed: list[str] = []
        root = self.output_dir / "quality_revision"
        if not root.is_dir():
            return healed
        for manifest_path in root.glob("*/revision_manifest.json"):
            manifest = read_json(manifest_path, {})
            revision_id = str(manifest.get("revision_id") or "")
            if not revision_id or revision_id == str(keep_revision_id or ""):
                continue
            if str(manifest.get("status") or "") not in REVISION_IN_FLIGHT_STATUSES:
                continue
            manifest.update({
                "status": "interrupted",
                "phase": "interrupted",
                "phase_label": "Revisão interrompida",
                "reason_code": "revision_process_lost",
                "resumable": True,
                "updated_at": utc_now(),
            })
            write_json(manifest_path, manifest)
            healed.append(revision_id)
        return healed

    def mark_cancelling(self) -> dict[str, Any] | None:
        """Flag a running revision as cancelling so the UI reflects it at once.

        The worker thread persists the terminal ``cancelled`` state once it stops
        between regions; this only moves ``running`` -> ``cancelling``.
        """

        root = self.output_dir / "quality_revision"
        pointer = read_json(root / "latest_revision.json", {})
        manifest_path = Path(str(pointer.get("manifest_path") or ""))
        if not manifest_path.is_file():
            return None
        manifest = read_json(manifest_path, {})
        if str(manifest.get("status") or "") not in {"queued", "running"}:
            return manifest
        manifest.update({
            "status": "cancelling",
            "phase_label": "Cancelando revisão",
            "reason_code": "user_cancelled",
            "updated_at": utc_now(),
        })
        write_json(manifest_path, manifest)
        return manifest

    def mark_interrupted(self, reason_code: str = "revision_process_lost") -> dict[str, Any] | None:
        """Persist a lost in-flight revision as interrupted so it can be resumed.

        Idempotent: a revision that already reached a terminal state is returned
        untouched, and checkpoints are never discarded.
        """

        root = self.output_dir / "quality_revision"
        pointer = read_json(root / "latest_revision.json", {})
        manifest_path = Path(str(pointer.get("manifest_path") or ""))
        if not manifest_path.is_file():
            return None
        manifest = read_json(manifest_path, {})
        if str(manifest.get("status") or "") not in REVISION_IN_FLIGHT_STATUSES:
            return manifest
        manifest.update({
            "status": "interrupted",
            "phase": "interrupted",
            "phase_label": "Revisão interrompida",
            "reason_code": reason_code,
            "resumable": True,
            "updated_at": utc_now(),
        })
        write_json(manifest_path, manifest)
        return manifest

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
        # Read the previous checkpoint before this run claims the "latest" pointer,
        # so a resume can skip the regions that were already answered and paid for.
        resume_reviews = self.resume_reviews()
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
            return self._run(progress, quality, source_pdf, paths, manifest, resume_reviews=resume_reviews)
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
        resume_reviews: dict[str, dict[str, Any]] | None = None,
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
        contextual = self._review_translations(regions, glossary, manifest, paths, resume_reviews=resume_reviews)
        write_json(paths.contextual_review, contextual)

        if contextual.get("cancelled"):
            # Stop cleanly: no safe changes are applied and no PDF is produced,
            # but every answered region stays in the checkpoint for a resume.
            manifest.update({
                "status": "cancelled",
                "phase": "cancelled",
                "phase_label": "Revisão cancelada",
                "reason_code": "user_cancelled",
                "resumable": True,
                "safe_changes_applied": 0,
                "reviewed_pdf_path": "",
                "no_reviewed_pdf_reason": "revision_cancelled",
                "updated_at": utc_now(),
            })
            write_json(paths.manifest, manifest)
            return manifest

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
        visual_states = self._region_visual_states(
            contextual.get("reviews", []), changed_by_page, render_audit.get("records", []))
        render_audit["region_visual_states"] = visual_states
        render_audit["visual_state_summary"] = self._visual_state_summary(visual_states)
        write_json(paths.render_audit, render_audit)
        manifest["visual_state_summary"] = render_audit["visual_state_summary"]

        self._checkpoint(paths, manifest, "pdf_generation", "Gerando PDF revisado")
        semantic_hash = self._semantic_revision_hash(changed_by_page)
        previous_version = self._latest_pdf_version()
        manifest["semantic_revision_hash"] = semantic_hash
        equivalent = bool(
            changed_by_page
            and previous_version
            and str(previous_version.get("semantic_revision_hash") or "") == semantic_hash
        )
        if equivalent:
            # The same corrections on the same regions produce the same chapter.
            # A PDF would differ only by metadata/recompression, so keep the one
            # we already have instead of growing a pile of equivalent versions.
            manifest.update({
                "reviewed_pdf_path": str(previous_version.get("pdf_path") or ""),
                "reviewed_pdf_sha256": str(previous_version.get("pdf_sha256") or ""),
                "no_reviewed_pdf_reason": "no_new_material_changes",
                "materially_equivalent_to": str(previous_version.get("pdf_path") or ""),
                "contains_new_material_changes": False,
            })
            visual = {
                "pdf_path": str(previous_version.get("pdf_path") or ""),
                "pdf_exists": True,
                "pages_inspected": 0,
                "expected_pages": len(pages),
                "reason_code": "no_new_material_changes",
                "created_at": utc_now(),
            }
        elif changed_by_page:
            reviewed_pdf = self._next_reviewed_pdf_path(source_pdf)
            generate_pdf(rendered_images, str(reviewed_pdf))
            manifest["reviewed_pdf_path"] = str(reviewed_pdf)
            manifest["reviewed_pdf_sha256"] = self._sha256(reviewed_pdf)
            manifest["contains_new_material_changes"] = True
            self._record_pdf_version(
                pdf_path=reviewed_pdf,
                manifest=manifest,
                semantic_hash=semantic_hash,
                changed_by_page=changed_by_page,
                parent=previous_version,
                page_count=len(pages),
            )
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

        summary = self._review_summary(
            contextual.get("reviews", []), visual_states, render_audit.get("records", []),
            changed_by_page, pages)
        manifest["review_summary"] = summary
        # Finished only means nothing is left for a human: a region the gate
        # refused or flagged for manual review keeps the whole revision in
        # review_required even after every NVIDIA request has returned.
        blocking = summary["rejected_visual_gate"] + summary["manual_review"] + summary["pending"] + summary["failed"]
        status = "review_required" if blocking or not contextual.get("quality_passed") else "finished"
        manifest.update({
            "status": status,
            "phase": "finalized",
            "phase_label": "Finalizado" if status == "finished" else "Revisão humana necessária",
            "revision_iteration": 1,
            "safe_changes_applied": summary["applied"],
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
        resume_reviews: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        reviewable = [
            self._review_record(regions, idx)
            for idx, region in enumerate(regions)
            if self._is_reviewable(region)
        ]
        if canary:
            reviewable = self._select_canary_records(reviewable, max_regions=max_regions or 10)
        skipped_unchanged: list[dict[str, Any]] = []
        resumed: list[dict[str, Any]] = []
        if not canary:
            reviewable, skipped = self._partition_suspicious(reviewable)
            skipped_unchanged = [self._unchanged_review(record) for record in skipped]
            manifest["suspicious_regions"] = len(reviewable)
            manifest["skipped_unchanged_regions"] = len(skipped)
            if resume_reviews:
                # Regions already answered by the cancelled/interrupted run are
                # carried over verbatim: no second request, no duplicate cost.
                pending = [r for r in reviewable if str(r.get("region_id")) not in resume_reviews]
                resumed = [resume_reviews[str(r["region_id"])] for r in reviewable if str(r.get("region_id")) in resume_reviews]
                reviewable = pending
            manifest["resumed_regions"] = len(resumed)
        reviewer = self.reviewer_factory()
        model = getattr(reviewer, "model", "unknown")
        manifest["model"] = model
        health_check: dict[str, Any] | None = None
        if canary and hasattr(reviewer, "health_check"):
            health_check = reviewer.health_check(
                raw_response_dir=paths.raw_responses,
                batch_id="health-check",
            )
            manifest["requests"] = int(getattr(reviewer, "requests", manifest.get("requests", 0) or 0))
            manifest["connectivity_check"] = health_check
            if not health_check.get("ok"):
                return {
                    "model": model,
                    "requests": manifest["requests"],
                    "reviewed_regions": 0,
                    "reviews": [],
                    "manual_review": len(reviewable),
                    "source_language_residual": 0,
                    "valid_response_batches": 0,
                    "repaired_batches": 0,
                    "fallback_individual_requests": 0,
                    "invalid_response_batches": 1,
                    "connectivity_check": health_check,
                    "quality_passed": False,
                    "created_at": utc_now(),
                }
        reviews: list[dict[str, Any]] = []
        cancelled = False
        review_started = time.perf_counter()
        # The canary is a live provider diagnostic, so it never reads or writes the
        # cache; a full revision reuses answers whose inputs are byte-identical.
        cache: RevisionResponseCache | None = None
        if not canary and bool(getattr(config, "QUALITY_REVISION_CACHE", True)):
            cache = RevisionResponseCache(self.output_dir / "revision_request_cache.jsonl")
        pending_cache: dict[str, tuple[str, dict[str, str]]] = {}
        batch_size = self._revision_batch_size(reviewable, canary=canary)
        for start in range(0, len(reviewable), batch_size):
            # Stop before issuing another provider request; everything already
            # answered stays in the checkpoint so the revision can be resumed.
            if not canary and self.cancel_requested():
                cancelled = True
                break
            batch = reviewable[start:start + batch_size]
            batch_id = f"{manifest.get('revision_id', 'revision')}-batch-{(start // batch_size) + 1:03d}"
            # One region per request means the cache can answer a whole batch. A
            # cached answer is a completed region that costs no provider request.
            if cache is not None and len(batch) == 1:
                record = batch[0]
                # Key on exactly what the provider will see, so a prompt-shaping
                # change invalidates the entry instead of replaying a stale answer.
                prompt_record = record
                if hasattr(reviewer, "_prompt_record"):
                    try:
                        prompt_record = reviewer._prompt_record(record)
                    except Exception:  # noqa: BLE001 - fall back to the raw record
                        prompt_record = record
                cache_key, input_hashes = cache.build_key(
                    prompt_record,
                    provider="nvidia",
                    model=model,
                    endpoint=str(getattr(reviewer, "base_url", "") or ""),
                    glossary=ContextualNvidiaReviewer._compact_glossary(glossary, [record]),
                    ocr_text=str(record.get("source_text") or ""),
                )
                cached = cache.lookup(cache_key, str(record.get("region_id") or ""))
                if cached:
                    reviews.append({**cached, "contract_path": "cache"})
                    continue
                pending_cache[str(record.get("region_id") or "")] = (cache_key, input_hashes)
            try:
                raw = reviewer.review_batch(
                    batch,
                    glossary,
                    batch_id=batch_id,
                    raw_response_dir=paths.raw_responses,
                    request_budget=max(1, len(reviewable) * 3),
                    diagnostic_mode=canary,
                )
            except TypeError:
                raw = reviewer.review_batch(batch, glossary)
            normalized = self._normalize_reviews(batch, raw)
            if cache is not None:
                for item in normalized:
                    entry = pending_cache.pop(str(item.get("region_id") or ""), None)
                    if entry:
                        cache.store(entry[0], str(item.get("region_id") or ""), item, entry[1])
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
                # Persist the answers themselves so a resume continues from here
                # instead of paying for the same regions again.
                "completed_reviews": reviews,
                # Live counters: the manifest is only rewritten on phase changes,
                # so the UI reads progress from here while the revision runs.
                "regions_total": int(manifest.get("total_regions") or 0),
                "suspicious_regions": len(reviewable) + len(resumed),
                "skipped_unchanged_regions": len(skipped_unchanged),
                "resumed_regions": len(resumed),
                "regions_completed": len(reviews) + len(resumed),
                "regions_pending": max(0, len(reviewable) - len(reviews)),
                "applied_low": sum(
                    1 for item in reviews
                    if item.get("action") == "rewrite" and item.get("risk") == "low"
                ),
                "risk_counts": {
                    level: sum(1 for item in reviews if str(item.get("risk") or "") == level)
                    for level in ("low", "medium", "high")
                },
                "elapsed_ms": int((time.perf_counter() - review_started) * 1000),
                **(cache.stats() if cache is not None else {}),
                "updated_at": utc_now(),
            }
            write_json(paths.checkpoint, checkpoint)
        # Answers carried over from the interrupted run, plus the non-suspicious
        # regions that were never sent to the model. Recording both keeps every
        # region accounted for: none is silently omitted or treated as approved.
        reviews.extend(resumed)
        reviews.extend(skipped_unchanged)
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
            "suspicious_regions": len(reviewable) if not canary else None,
            "skipped_unchanged_regions": len(skipped_unchanged),
            "reviews": reviews,
            "manual_review": blocked,
            "source_language_residual": residual,
            "valid_response_batches": valid_batches,
            "repaired_batches": repaired_batches,
            "fallback_individual_requests": fallback_individual,
            "invalid_response_batches": invalid_batches,
            "quality_passed": blocked == 0 and residual == 0,
            "connectivity_check": health_check or {},
            "cancelled": cancelled,
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

    def _revision_batch_size(self, records: list[dict[str, Any]], *, canary: bool = False) -> int:
        # The reasoning model is 100% ID-complete with one region per request but
        # silently omits regions when several share a batch, which the batch-level
        # fail-closed parser then routes wholesale to manual review.  One region
        # per request is the only proven-complete strategy for both the canary and
        # the full revision.
        # ponytail: fixed bs=1; only re-enable batching if repeated tests prove the
        # provider returns 100% of the requested IDs per batch.
        return 1

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

    # A reviewed page is an edit of the translated page, never a rebuild of the
    # English source. Overlap is detected by how much darker a region became.
    RENDER_OVERLAP_INK_RATIO = 1.35
    # Cleanup must leave the region essentially blank. Anything above this share
    # of the previous ink means the old translation survived and the new text
    # would be drawn on top of it.
    CLEANUP_RESIDUAL_INK_RATIO = 0.05

    @staticmethod
    def _resolve_incremental_render_base(page: dict[str, Any]) -> dict[str, Any]:
        """Pick the image a reviewed page must be edited from.

        The original scan still carries the source-language text, so it can never
        be the base: rebuilding from it is what made earlier revisions show English
        again. Only the translated pipeline output qualifies, and when it is
        missing the page fails closed instead of silently falling back.
        """

        candidate = str(page.get("output_path") or "")
        if candidate and Path(candidate).is_file():
            return {
                "path": candidate,
                "base_kind": "translated_pipeline_output",
                "reason_code": "",
            }
        return {
            "path": "",
            "base_kind": "unavailable",
            "reason_code": "translated_render_base_unavailable",
        }

    @staticmethod
    def _changed_region_boxes(
        page: dict[str, Any],
        changes: list[dict[str, Any]],
        *,
        width: int,
        height: int,
    ) -> tuple[list[tuple[int, int, int, int]], list[str]]:
        """Clamped boxes for the regions this revision actually changed."""

        wanted = {str(change.get("region_id") or "") for change in changes}
        number = page_number(page)
        boxes: list[tuple[int, int, int, int]] = []
        rejected: list[str] = []
        for item in page.get("debug_data", {}).get("items") or []:
            if not isinstance(item, dict):
                continue
            if stable_region_key(number, item) not in wanted:
                continue
            # Region geometry is stored as (x, y, width, height); draw_box covers
            # the whole redraw area including padding, so it is the safe envelope.
            raw = (item.get("draw_box") or item.get("safe_area")
                   or item.get("allowed_modification_box") or item.get("bounding_box") or [])
            try:
                x, y, box_width, box_height = (int(v) for v in list(raw)[:4])
            except (TypeError, ValueError):
                rejected.append(stable_region_key(number, item))
                continue
            if box_width <= 0 or box_height <= 0:
                rejected.append(stable_region_key(number, item))
                continue
            left, top = max(0, x), max(0, y)
            right, bottom = min(width, x + box_width), min(height, y + box_height)
            if right - left < 2 or bottom - top < 2:
                rejected.append(stable_region_key(number, item))
                continue
            boxes.append((left, top, right, bottom))
        return boxes, rejected

    # Region geometry in the manifest is (x, y, width, height). Treating it as
    # (left, top, right, bottom) silently inverts the bottom of a page-bottom
    # balloon, so every conversion goes through these helpers.
    @staticmethod
    def _xywh_to_ltrb(box: Any) -> tuple[int, int, int, int] | None:
        try:
            x, y, width, height = (int(v) for v in list(box)[:4])
        except (TypeError, ValueError):
            return None
        if width <= 0 or height <= 0:
            return None
        return x, y, x + width, y + height

    @staticmethod
    def _clip_ltrb(rect: tuple[int, int, int, int], *, width: int, height: int) -> tuple[int, int, int, int] | None:
        left, top, right, bottom = rect
        left, top = max(0, left), max(0, top)
        right, bottom = min(width, right), min(height, bottom)
        if right - left < 2 or bottom - top < 2:
            return None
        return left, top, right, bottom

    # Glyphs are a minority of a balloon. A candidate side larger than this is
    # artwork or the background itself, not text.
    TEXT_POLARITY_MAX_SHARE = 0.45
    # One side must clearly dominate, otherwise the polarity is ambiguous.
    TEXT_POLARITY_DOMINANCE = 2.0
    TEXT_POLARITY_DELTA = 35

    @classmethod
    def _detect_text_polarity(cls, region: "np.ndarray") -> tuple[str, dict[str, Any]]:
        """Decide whether the drawn text is darker or lighter than its balloon.

        A dark balloon carries light glyphs, so masking "darker than background"
        would find nothing and the new text would land on top of the old one.
        The median alone is not enough, so both sides are measured and one has to
        clearly dominate; otherwise the region is refused rather than guessed.
        """

        grey = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY) if region.ndim == 3 else region
        background = int(np.median(grey))
        total = int(grey.size) or 1
        darker = int((grey <= background - cls.TEXT_POLARITY_DELTA).sum())
        lighter = int((grey >= background + cls.TEXT_POLARITY_DELTA).sum())
        evidence = {
            "background_level": background,
            "darker_pixels": darker,
            "lighter_pixels": lighter,
            "darker_share": round(darker / total, 4),
            "lighter_share": round(lighter / total, 4),
        }
        if darker == 0 and lighter == 0:
            # Nothing stands out from the background: the region is already blank.
            return "blank", evidence
        dark_ok = 0 < darker and darker / total <= cls.TEXT_POLARITY_MAX_SHARE
        light_ok = 0 < lighter and lighter / total <= cls.TEXT_POLARITY_MAX_SHARE
        if dark_ok and darker >= lighter * cls.TEXT_POLARITY_DOMINANCE:
            return "dark_text_on_light_background", evidence
        if light_ok and lighter >= darker * cls.TEXT_POLARITY_DOMINANCE:
            return "light_text_on_dark_background", evidence
        return "ambiguous", evidence

    @classmethod
    def _previous_text_mask(cls, region: "np.ndarray", polarity: str = "") -> "np.ndarray":
        """Mask the glyphs of the translation currently drawn in this region.

        The revision re-renders on top of the translated page, so the text to
        remove is the previous translation, not the source text. Only the side of
        the histogram the glyphs live on is taken, which keeps balloon borders and
        artwork out of the mask.
        """

        grey = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY) if region.ndim == 3 else region
        background = int(np.median(grey))
        if not polarity:
            polarity, _ = cls._detect_text_polarity(region)
        if polarity == "light_text_on_dark_background":
            mask = (grey >= background + cls.TEXT_POLARITY_DELTA).astype(np.uint8) * 255
        else:
            mask = (grey <= background - cls.TEXT_POLARITY_DELTA).astype(np.uint8) * 255
        if not mask.any():
            return mask
        # Close the antialiased halo without letting the mask bleed into the art.
        return cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=1)

    @staticmethod
    def _pixels_outside_boxes_changed(
        base: "np.ndarray",
        result: "np.ndarray",
        boxes: list[tuple[int, int, int, int]],
    ) -> int:
        """How many pixels changed outside the regions this revision may touch.

        Compositing already guarantees zero, so a non-zero count means something
        rewrote the page wholesale and the result must be rejected.
        """

        if base.shape != result.shape:
            return int(base.size)
        differing = np.any(base != result, axis=2) if base.ndim == 3 else base != result
        allowed = np.zeros(differing.shape, dtype=bool)
        for left, top, right, bottom in boxes:
            allowed[top:bottom, left:right] = True
        return int((differing & ~allowed).sum())

    # A cleanup mask that covers most of the region is not text: it is artwork.
    CLEANUP_MAX_MASK_RATIO = 0.60
    # A single blob this large is a panel or the page around a balloon, not a glyph.
    CLEANUP_MAX_COMPONENT_RATIO = 0.15
    # Below this median level the region is dark, so its text is light-on-dark
    # and the dark-glyph mask below would not find it.
    CLEANUP_MIN_BACKGROUND_LEVEL = 128

    def _clean_previous_translation(
        self,
        base: "np.ndarray",
        boxes: list[tuple[int, int, int, int]],
    ) -> tuple["np.ndarray | None", list[dict[str, Any]], str]:
        """Erase the previous translation inside the changed regions.

        Returns the cleaned page, per-region metrics and a reason code. The page
        is only cleaned where this revision is allowed to draw, so every other
        pixel is untouched. Fails closed when a mask looks like artwork rather
        than text instead of guessing a background.
        """

        cleaned = base.copy()
        metrics: list[dict[str, Any]] = []
        for left, top, right, bottom in boxes:
            region = cleaned[top:bottom, left:right]
            polarity, evidence = self._detect_text_polarity(region)
            if polarity == "ambiguous":
                # Neither side of the histogram looks like glyphs: refuse rather
                # than erase artwork or draw the new text over the old one.
                metrics.append({
                    "box": [left, top, right, bottom],
                    "polarity": polarity,
                    "polarity_evidence": evidence,
                    "reason_code": "ambiguous_text_polarity",
                })
                return None, metrics, "ambiguous_text_polarity"
            mask = self._previous_text_mask(region, polarity)
            area = int((mask > 0).sum())
            total = int(mask.size) or 1
            ratio = area / total
            entry = {
                "box": [left, top, right, bottom],
                "polarity": polarity,
                "polarity_evidence": evidence,
                "mask_area": area,
                "mask_ratio": round(ratio, 4),
                "pixels_removed": area,
                "pixels_preserved": total - area,
            }
            if area == 0:
                # Nothing to erase: the region is already blank background.
                entry["reason_code"] = "empty_previous_text_mask"
                metrics.append(entry)
                continue
            if ratio > self.CLEANUP_MAX_MASK_RATIO:
                entry["reason_code"] = "excessive_cleanup_mask"
                metrics.append(entry)
                return None, metrics, "excessive_cleanup_mask"
            # Glyphs are many small blobs. One blob covering a large part of the
            # region is artwork or the surrounding page, so erasing it would
            # destroy the picture rather than the previous translation.
            count, labels, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), 8)
            largest = int(stats[1:, cv2.CC_STAT_AREA].max()) if count > 1 else 0
            entry["components"] = max(0, count - 1)
            entry["largest_component_ratio"] = round(largest / total, 4)
            if largest / total > self.CLEANUP_MAX_COMPONENT_RATIO:
                entry["reason_code"] = "light_text_components_not_isolated"
                metrics.append(entry)
                return None, metrics, "light_text_components_not_isolated"
            # Reconstruct the background from the surrounding balloon pixels.
            repaired = cv2.inpaint(region, mask, 3, cv2.INPAINT_TELEA)
            region[mask > 0] = repaired[mask > 0]
            entry["reason_code"] = ""
            metrics.append(entry)
        return cleaned, metrics, ""

    @staticmethod
    def _ink(image: "np.ndarray", polarity: str = "dark_text_on_light_background") -> int:
        """Count text pixels: text drawn over text roughly doubles this.

        On a dark balloon the balloon itself is dark, so counting dark pixels
        would report the balloon as leftover text. Count in the direction the
        glyphs were actually drawn instead.
        """

        grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        if polarity == "light_text_on_dark_background":
            return int((grey > 127).sum())
        return int((grey < 128).sum())

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
            # The reviewed page is an edit of the translated page. Rebuilding from
            # the English scan is what previously made the source text reappear.
            base_info = self._resolve_incremental_render_base(page)
            base = cv2.imread(base_info["path"]) if base_info["path"] else None
            if base is None:
                rendered_paths.append(str(current_output))
                render_records.append({
                    "page": number,
                    "status": "rejected",
                    "reason_code": base_info["reason_code"] or "translated_render_base_unavailable",
                    "changed_regions": [item["region_id"] for item in changed_by_page[number]],
                })
                continue
            height, width = base.shape[:2]
            boxes, rejected_regions = self._changed_region_boxes(
                page, changed_by_page[number], width=width, height=height)
            if not boxes:
                rendered_paths.append(str(current_output))
                render_records.append({
                    "page": number,
                    "status": "rejected",
                    "reason_code": "unsafe_incremental_mask",
                    "rejected_regions": rejected_regions,
                })
                continue
            # Erase the previous translation inside the changed regions first, so
            # the renderer draws the new text on clean background instead of on
            # top of the text that is already there.
            cleaned, cleanup_metrics, cleanup_reason = self._clean_previous_translation(base, boxes)
            if cleaned is None:
                rendered_paths.append(str(current_output))
                render_records.append({
                    "page": number,
                    "status": "rejected",
                    "reason_code": cleanup_reason or "clean_region_background_unavailable",
                    "cleanup": cleanup_metrics,
                })
                continue
            groups = self._groups_from_page_items(page)
            full, debug_data = render_analyzed_image(
                cleaned,
                [],
                [],
                groups,
                font_path=config.FONT_PATH or None,
                page_index=number,
                image_path=str(image_path),
            )
            if full is None or full.shape[:2] != base.shape[:2]:
                rendered_paths.append(str(current_output))
                render_records.append({
                    "page": number,
                    "status": "rejected",
                    "reason_code": "render_dimension_mismatch",
                })
                continue
            # Copy only the changed regions onto the translated page, so every
            # untouched pixel stays byte-identical to the base by construction.
            final = base.copy()
            overlap = []
            polarity_by_box = {tuple(item.get("box") or ()): str(item.get("polarity") or "")
                               for item in cleanup_metrics}
            for left, top, right, bottom in boxes:
                patch = full[top:bottom, left:right]
                polarity = polarity_by_box.get((left, top, right, bottom)) \
                    or "dark_text_on_light_background"
                before_ink = self._ink(base[top:bottom, left:right], polarity)
                residual_ink = self._ink(cleaned[top:bottom, left:right], polarity)
                after_ink = self._ink(patch, polarity)
                # The safety property is that the previous translation was really
                # erased. Once the region is blank the new text cannot be drawn on
                # top of anything, so a longer translation legitimately adds ink.
                if before_ink and residual_ink > before_ink * self.CLEANUP_RESIDUAL_INK_RATIO:
                    overlap.append({"box": [left, top, right, bottom],
                                    "ink_before": before_ink,
                                    "ink_after_cleanup": residual_ink,
                                    "ink_final": after_ink,
                                    "polarity": polarity,
                                    "detail": "previous translation still present after cleanup"})
                    continue
                final[top:bottom, left:right] = patch
            if overlap:
                rendered_paths.append(str(current_output))
                render_records.append({
                    "page": number,
                    "status": "rejected",
                    "reason_code": "text_overlap_regression_detected",
                    "changed_regions": [item["region_id"] for item in changed_by_page[number]],
                    "overlap": overlap,
                })
                continue
            outside_changed = self._pixels_outside_boxes_changed(base, final, boxes)
            if outside_changed:
                rendered_paths.append(str(current_output))
                render_records.append({
                    "page": number,
                    "status": "rejected",
                    "reason_code": "unexpected_pixels_outside_changed_regions",
                    "outside_changed_pixels": outside_changed,
                })
                continue
            target = review_pages / f"page_{number:03d}.png"
            # Keep the image extension on the temp file: the encoder is chosen
            # from it, so a bare ".tmp" suffix cannot be written at all.
            tmp = review_pages / f"page_{number:03d}.tmp.png"
            if not cv2.imwrite(str(tmp), final):
                rendered_paths.append(str(current_output))
                render_records.append({
                    "page": number,
                    "status": "rejected",
                    "reason_code": "reviewed_page_write_failed",
                })
                continue
            os.replace(str(tmp), str(target))
            rendered_paths.append(str(target))
            render_records.append({
                "page": number,
                "status": "rendered",
                "base_kind": base_info["base_kind"],
                "base_path": base_info["path"],
                "changed_regions": [item["region_id"] for item in changed_by_page[number]],
                "changed_boxes": [list(b) for b in boxes],
                "rejected_regions": rejected_regions,
                "cleanup": cleanup_metrics,
                "redrawn_groups": debug_data.get("redrawn_group_count"),
            })
        return rendered_paths, {
            "pages_rerendered": len(changed_by_page),
            "regions_rerendered": sum(len(items) for items in changed_by_page.values()),
            "records": render_records,
            "created_at": utc_now(),
        }

    # A region ends in exactly one of these, so the UI never has to guess what
    # happened to a correction the reviewer proposed.
    VISUAL_STATES = ("applied", "rejected_visual_regression", "manual_review", "unchanged", "pending")

    @staticmethod
    def _region_visual_states(
        reviews: list[dict[str, Any]],
        changed_by_page: dict[int, list[dict[str, Any]]],
        render_records: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        states: dict[str, dict[str, Any]] = {}
        for item in reviews:
            region_id = str(item.get("region_id") or "")
            if not region_id:
                continue
            action = str(item.get("action") or "")
            if action == "manual_review":
                state, reason = "manual_review", str(item.get("reason_code") or "manual_review_required")
            elif action == "keep":
                state, reason = "unchanged", str(item.get("reason_code") or "")
            else:
                # Proposed but not yet seen by the renderer.
                state, reason = "pending", str(item.get("reason_code") or "")
            # Everything the review panel needs to explain the decision, so the
            # UI never has to reopen the raw review to describe one region.
            states[region_id] = {
                "region_id": region_id,
                "page_id": region_id.split(":", 1)[0],
                "state": state,
                "reason_code": reason,
                "risk": str(item.get("risk") or ""),
                "confidence": item.get("confidence"),
                "source_text": str(item.get("source_text") or item.get("original") or ""),
                "previous_translation": str(item.get("current_translation") or ""),
                "proposed_translation": str(item.get("revised_translation") or ""),
                "applied_translation": "",
                "cleanup_metrics": None,
                "pixel_diff": None,
                "comparison_artifact": "",
                "timestamp": utc_now(),
            }

        proposed_by_region = {str(change.get("region_id") or ""): str(change.get("revised_translation") or "")
                              for items in changed_by_page.values() for change in items}
        submitted = set(proposed_by_region)
        for region_id in submitted:
            states.setdefault(region_id, {"region_id": region_id, "state": "pending", "reason_code": ""})
            states[region_id]["state"] = "pending"

        for record in render_records:
            page = record.get("page")
            regions = [str(value) for value in (record.get("changed_regions") or [])]
            rejected = {str(value) for value in (record.get("rejected_regions") or [])}
            page_failed = str(record.get("status")) == "rejected"
            page_reason = str(record.get("reason_code") or "")
            cleanup = record.get("cleanup")
            for region_id in regions or sorted(submitted):
                entry = states.setdefault(region_id, {"region_id": region_id, "state": "pending", "reason_code": ""})
                if page is not None:
                    entry["page"] = page
                    entry.setdefault("page_id", f"p{int(page):03d}")
                if cleanup is not None:
                    entry["cleanup_metrics"] = cleanup
                if page_failed or region_id in rejected:
                    entry["state"] = "rejected_visual_regression"
                    entry["reason_code"] = page_reason or entry.get("reason_code") or "region_rejected"
                elif entry["state"] == "pending":
                    entry["state"] = "applied"
                    entry["applied_translation"] = proposed_by_region.get(region_id, "")
            if page_failed and not regions:
                # The whole page was refused before any region was attributed.
                for region_id in submitted:
                    entry = states.setdefault(region_id, {"region_id": region_id, "state": "pending", "reason_code": ""})
                    if entry["state"] == "pending":
                        entry.update({"state": "rejected_visual_regression", "reason_code": page_reason})
        return states

    @classmethod
    def _visual_state_summary(cls, states: dict[str, dict[str, Any]]) -> dict[str, int]:
        summary = {state: 0 for state in cls.VISUAL_STATES}
        for entry in states.values():
            state = str(entry.get("state") or "pending")
            summary[state] = summary.get(state, 0) + 1
        return summary

    @staticmethod
    def _review_summary(
        reviews: list[dict[str, Any]],
        states: dict[str, dict[str, Any]],
        render_records: list[dict[str, Any]],
        changed_by_page: dict[int, list[dict[str, Any]]],
        pages: list[dict[str, Any]],
    ) -> dict[str, int]:
        by_state: dict[str, int] = {}
        for entry in states.values():
            by_state[str(entry.get("state") or "pending")] = by_state.get(str(entry.get("state") or "pending"), 0) + 1
        rendered_pages = {int(r.get("page")) for r in render_records
                          if r.get("page") is not None and str(r.get("status")) == "rendered"}
        failed_pages = {int(r.get("page")) for r in render_records
                        if r.get("page") is not None and str(r.get("status")) == "rejected"}
        return {
            "total": len(reviews),
            "analyzed": sum(1 for item in reviews if str(item.get("action") or "")),
            "applied": by_state.get("applied", 0),
            "rejected_visual_gate": by_state.get("rejected_visual_regression", 0),
            "manual_review": by_state.get("manual_review", 0),
            "unchanged": by_state.get("unchanged", 0),
            "pending": by_state.get("pending", 0),
            "failed": sum(1 for r in render_records if str(r.get("status")) == "warning"),
            "pages_changed": len(rendered_pages),
            "pages_preserved": max(0, len(pages) - len(rendered_pages) - len(failed_pages)),
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

    @staticmethod
    def _word_tokens(text: str) -> list[str]:
        return [t for t in re.findall(r"[^\W\d_]+", str(text or "").casefold(), flags=re.UNICODE) if len(t) >= 2]

    def _suspicious_reasons(self, record: dict[str, Any]) -> list[str]:
        # General, work-agnostic signals that a translation may need model review.
        # No source phrases, translations, page numbers, or per-title rules here.
        reasons: list[str] = []
        source = str(record.get("source_text") or "").strip()
        current = str(record.get("current_translation") or "").strip()
        if record.get("quality_reasons"):
            reasons.append("already_flagged")
        try:
            confidence = float(record.get("ocr_confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        low_conf = float(getattr(config, "QUALITY_REVISION_LOW_OCR_CONFIDENCE", 0.75) or 0.75)
        if 0.0 < confidence < low_conf:
            reasons.append("low_ocr_confidence")
        if source and not current:
            reasons.append("empty_translation")
        if source and current and current.casefold() == source.casefold():
            reasons.append("untranslated_literal")
        # English residual / mixed language: a pt-BR translation should not reuse
        # most of the source's word tokens verbatim.
        source_tokens = set(self._word_tokens(source))
        current_tokens = self._word_tokens(current)
        if len(source_tokens) >= 2 and current_tokens:
            shared = sum(1 for t in current_tokens if t in source_tokens)
            if shared / len(current_tokens) >= 0.5:
                reasons.append("source_language_residual")
        # Length anomalies for multi-word source (truncation / overflow).
        if len(source.split()) >= 2 and current:
            ratio = len(current) / max(1, len(source))
            if ratio < 0.4:
                reasons.append("suspicious_truncation")
            elif ratio > 1.8:
                reasons.append("suspicious_overflow")
        return reasons

    def _partition_suspicious(self, reviewable: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not bool(getattr(config, "QUALITY_REVISION_SUSPICIOUS_ONLY", True)):
            return reviewable, []
        suspicious: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for record in reviewable:
            reasons = self._suspicious_reasons(record)
            if reasons:
                record = {**record, "suspicious_reasons": reasons}
                suspicious.append(record)
            else:
                skipped.append(record)
        return suspicious, skipped

    @staticmethod
    def _unchanged_review(record: dict[str, Any]) -> dict[str, Any]:
        # A region not sent to the model stays as-is; it is never "AI approved".
        return {
            "region_id": record["region_id"],
            "action": "keep",
            "revised_translation": "",
            "reason_code": "not_suspicious_unchanged",
            "confidence": 0.0,
            "risk": "low",
            "terminology": [],
            "contract_path": "not_reviewed",
        }

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

    PDF_VERSION_MANIFEST = "reviewed_pdf_version_manifest.json"
    RENDERER_VERSION = "1"

    @staticmethod
    def _semantic_revision_hash(changed_by_page: dict[int, list[dict[str, Any]]]) -> str:
        """Stable fingerprint of what a revision actually changes.

        Built only from the applied corrections (region, action and resulting
        text), never from timestamps, file order or PDF metadata, so two runs
        that reach the same chapter produce the same hash even though their PDFs
        would differ byte-for-byte after recompression.
        """

        entries = sorted(
            (
                str(item.get("region_id") or ""),
                str(item.get("action") or ""),
                str(item.get("revised_translation") or item.get("translation") or ""),
                str(item.get("source_text") or ""),
            )
            for items in (changed_by_page or {}).values()
            for item in items
        )
        payload = json.dumps(entries, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _pdf_version_manifest_path(self) -> Path:
        return self.output_dir / self.PDF_VERSION_MANIFEST

    def _latest_pdf_version(self) -> dict[str, Any] | None:
        versions = read_json(self._pdf_version_manifest_path(), {})
        entries = versions.get("versions") if isinstance(versions, dict) else None
        if not isinstance(entries, list) or not entries:
            return None
        # Only versions whose file still exists can be reused as "the current PDF".
        for entry in reversed(entries):
            if isinstance(entry, dict) and Path(str(entry.get("pdf_path") or "")).is_file():
                return entry
        return None

    def _record_pdf_version(
        self,
        *,
        pdf_path: Path,
        manifest: dict[str, Any],
        semantic_hash: str,
        changed_by_page: dict[int, list[dict[str, Any]]],
        parent: dict[str, Any] | None,
        page_count: int,
    ) -> None:
        path = self._pdf_version_manifest_path()
        document = read_json(path, {})
        entries = document.get("versions") if isinstance(document.get("versions"), list) else []
        entries.append({
            "pdf_path": str(pdf_path),
            "pdf_sha256": str(manifest.get("reviewed_pdf_sha256") or ""),
            "semantic_revision_hash": semantic_hash,
            "revision_id": str(manifest.get("revision_id") or ""),
            "parent_revision_id": str((parent or {}).get("revision_id") or ""),
            "page_count": page_count,
            "changed_region_ids": sorted(
                str(item.get("region_id") or "")
                for items in changed_by_page.values() for item in items
            ),
            "changed_page_ids": sorted(int(page) for page in changed_by_page),
            "renderer_version": self.RENDERER_VERSION,
            "contains_new_material_changes": True,
            "created_at": utc_now(),
        })
        write_json(path, {"versions": entries})

    @staticmethod
    def _sha256(path: Path) -> str:
        import hashlib

        h = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest().upper()
