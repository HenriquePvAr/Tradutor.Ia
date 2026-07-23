"""Persisted, UI-driven full-chapter quality revision.

This module intentionally works from existing pipeline artifacts.  It never downloads
chapter pages, opens a browser, publishes anything, or mutates the original PDF.  A
revision run is a separate audit/review workspace tied to a parent job/run.
"""

from __future__ import annotations

import json
import re
import shutil
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
_NVIDIA_REVIEW_REQUEST_SCRIPT = r"""
import json
import sys
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
with urllib.request.urlopen(http_request, timeout=45) as response:
    sys.stdout.write(response.read().decode("utf-8"))
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

    def review_batch(self, records: list[dict[str, Any]], glossary: dict[str, Any]) -> list[dict[str, Any]]:
        if not records:
            return []
        if not self.configured:
            raise RuntimeError("nvidia_not_configured")
        payload = {
            "chapter": {"target_language": "pt-BR", "review_goal": "naturalidade e fidelidade sem hardcode"},
            "glossary": glossary,
            "regions": records,
        }
        started = time.perf_counter()
        request_payload = {
            "model": self.model,
            "temperature": 0.1,
            "max_tokens": 4096,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Revise traducoes de manhwa de ingles para portugues do Brasil. "
                        "Responda somente JSON. Use exclusivamente os region_id recebidos. "
                        "Nao invente texto fora do contexto. Nao use markdown. "
                        "Classifique action como keep, rewrite, preserve_original ou manual_review. "
                        "Use risk low, medium ou high. HIGH nunca deve ser aplicado automaticamente."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
        }
        self.requests += 1
        try:
            python_executable = str(getattr(sys, "_base_executable", "") or sys.executable)
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
                raise RuntimeError("nvidia_review_subprocess_failed")
            response_payload = json.loads(completed.stdout or "{}")
        except (subprocess.TimeoutExpired, OSError, ValueError, RuntimeError):
            return [
                {
                    "region_id": str(item["region_id"]),
                    "action": "manual_review",
                    "revised_translation": "",
                    "reason_code": "nvidia_review_request_failed",
                    "confidence": 0.0,
                    "risk": "high",
                    "terminology": [],
                }
                for item in records
            ]
        finally:
            self.duration_seconds += time.perf_counter() - started
        text = (((response_payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        try:
            parsed = TranslatorNvidiaBatch._loads_json(TranslatorNvidiaBatch._remove_markdown_fence(text))
        except ValueError:
            return [
                {
                    "region_id": str(item["region_id"]),
                    "action": "manual_review",
                    "revised_translation": "",
                    "reason_code": "invalid_json_review_response",
                    "confidence": 0.0,
                    "risk": "high",
                    "terminology": [],
                }
                for item in records
            ]
        items = self.review_items_from_parsed(parsed)
        expected = {str(item["region_id"]) for item in records}
        if not isinstance(items, list):
            return [
                {
                    "region_id": region_id,
                    "action": "manual_review",
                    "revised_translation": "",
                    "reason_code": "invalid_review_response_shape",
                    "confidence": 0.0,
                    "risk": "high",
                    "terminology": [],
                }
                for region_id in sorted(expected)
            ]
        result: list[dict[str, Any]] = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            region_id = str(entry.get("region_id") or "")
            if region_id not in expected:
                continue
            result.append({
                "region_id": region_id,
                "action": str(entry.get("action") or "manual_review"),
                "revised_translation": str(entry.get("revised_translation") or ""),
                "reason_code": str(entry.get("reason_code") or "model_review"),
                "confidence": float(entry.get("confidence") or 0.0),
                "risk": str(entry.get("risk") or "high").lower(),
                "terminology": entry.get("terminology") if isinstance(entry.get("terminology"), list) else [],
            })
        seen = {item["region_id"] for item in result}
        for missing in sorted(expected - seen):
            result.append({
                "region_id": missing,
                "action": "manual_review",
                "revised_translation": "",
                "reason_code": "missing_region_id_in_model_response",
                "confidence": 0.0,
                "risk": "high",
                "terminology": [],
            })
        return result

    def _chat_completions_url(self) -> str:
        base = str(self.base_url or "").rstrip("/")
        return base if base.endswith("/chat/completions") else f"{base}/chat/completions"

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
        contextual = self._review_translations(regions, glossary, manifest)
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
        reviewed_pdf = self._next_reviewed_pdf_path()
        if changed_by_page:
            generate_pdf(rendered_images, str(reviewed_pdf))
        else:
            shutil.copy2(source_pdf, reviewed_pdf)
        manifest["reviewed_pdf_path"] = str(reviewed_pdf)
        manifest["reviewed_pdf_sha256"] = self._sha256(reviewed_pdf)

        self._checkpoint(paths, manifest, "pdf_inspection", "Inspecionando o novo PDF")
        visual = self._inspect_pdf_pages(reviewed_pdf, rendered_images, expected_pages=len(pages))
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

    def _review_translations(self, regions: list[dict[str, Any]], glossary: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
        reviewable = [
            self._review_record(regions, idx)
            for idx, region in enumerate(regions)
            if self._is_reviewable(region)
        ]
        reviewer = self.reviewer_factory()
        model = getattr(reviewer, "model", "unknown")
        manifest["model"] = model
        reviews: list[dict[str, Any]] = []
        batch_size = 6
        for start in range(0, len(reviewable), batch_size):
            batch = reviewable[start:start + batch_size]
            raw = reviewer.review_batch(batch, glossary)
            normalized = self._normalize_reviews(batch, raw)
            reviews.extend(normalized)
            manifest["requests"] = int(getattr(reviewer, "requests", manifest.get("requests", 0) or 0))
        manifest["requests"] = int(getattr(reviewer, "requests", manifest.get("requests", 0) or 0))
        blocked = sum(1 for item in reviews if item.get("action") == "manual_review")
        residual = sum(1 for item in reviews if item.get("reason_code") == "source_language_residual")
        return {
            "model": model,
            "requests": manifest["requests"],
            "reviewed_regions": len(reviewable),
            "reviews": reviews,
            "manual_review": blocked,
            "source_language_residual": residual,
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
            "previous_context": previous_region.get("current_translation") or previous_region.get("source_text") or "",
            "next_context": next_region.get("current_translation") or next_region.get("source_text") or "",
            "quality_reasons": region.get("quality_reasons") or [],
        }

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

    def _next_reviewed_pdf_path(self) -> Path:
        base = self.output_dir / "shadow_slave_capitulo_shadow_slave_reviewed_v2.pdf"
        if not base.exists():
            return base
        idx = 3
        while True:
            candidate = self.output_dir / f"shadow_slave_capitulo_shadow_slave_reviewed_v{idx}.pdf"
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
