"""Version 1 index joining Professional QC items to their page and PSD artifacts.

The manifest is intentionally a small index, not a second quality report or translation
store. Quality diagnostics remain owned by Quality Report; edits remain owned by Review's
persisted revision store; layer names come only from the PSD writer's returned provenance.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

from yomu.review.review_item import ReviewItem, ReviewStatus, from_quality_review_item

REVIEW_MANIFEST_VERSION = 1
_CANDIDATE_COLLECTIONS = (
    "translation_terminal_items", "text_overflow_items",
    "visual_validation_failures", "suspicious_groups",
)
_SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|cookie|password|service[_-]?role|deepl[_-]?key)", re.I)
_VALID_STATUSES = {ReviewStatus.PENDING, ReviewStatus.APPROVED, ReviewStatus.RESOLVED}


def _page_and_items(report: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    """Select the same report-backed review population as UiBridge, without copying QC data."""
    result: dict[int, list[dict[str, Any]]] = {}
    for page in report.get("pages", []) or []:
        if not isinstance(page, dict):
            continue
        try:
            number = int(page.get("index") or page.get("sequence_index") or 0)
        except (TypeError, ValueError):
            continue
        if number <= 0:
            continue
        seen: set[str] = set()
        selected = []
        for collection in _CANDIDATE_COLLECTIONS:
            for raw in page.get(collection, []) or []:
                if not isinstance(raw, dict):
                    continue
                if not (raw.get("manual_review_required") or raw.get("preserved_original")
                        or raw.get("text_overflow_ratio") or raw.get("quality_reasons")
                        or not (raw.get("visual_validation") or {}).get("visual_validation_passed", True)):
                    continue
                identity = str(raw.get("region_id") or raw.get("id") or "")
                dedupe = identity or json.dumps(
                    [raw.get("classification"), raw.get("bounding_box"), raw.get("text")],
                    ensure_ascii=False, sort_keys=True, default=str)
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                selected.append(raw)
        result[number] = selected
    return result


def _safe_relative(value: object) -> str | None:
    if value in (None, ""):
        return None
    raw = str(value)
    if "\\" in raw or raw.startswith(("/", "//")) or re.match(r"^[A-Za-z]:", raw):
        raise ValueError("review_manifest_path_invalid")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("review_manifest_path_invalid")
    return path.as_posix()


def _relative_existing(path_value: object, root: Path) -> str | None:
    if not path_value:
        return None
    try:
        path = Path(str(path_value)).resolve()
        relative = path.relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return None
    return _safe_relative(relative)


def _warning_codes(raw: dict[str, Any], normalized: ReviewItem, psd_status: str = "") -> list[str]:
    """Carry only explicit diagnostic codes/conditions; never infer QC findings from prose."""
    warnings = list(normalized.warnings)
    for code in raw.get("warning_codes", []) or []:
        value = str(code or "").strip()
        if value and value.replace("_", "").isupper():
            warnings.append(value)
    reasons = raw.get("quality_reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    for reason in reasons:
        value = str(reason or "").strip().upper().replace("-", "_").replace(" ", "_")
        if value in {"OCR_LOW_CONFIDENCE", "OCR_INCORRECT", "ENGLISH_RESIDUAL",
                     "SEMANTIC_VALIDATION", "TERMINOLOGY_WARNING", "TRANSLATION_SKIPPED",
                     "SOURCE_FALLBACK", "VISUAL_REVIEW"}:
            warnings.append(value)
    if psd_status == "source_fallback":
        warnings.append("SOURCE_FALLBACK")
    return list(dict.fromkeys(warnings))


def _severity(warnings: list[str]) -> str:
    # M9 treats these as review findings, not structural failures.
    return "REVIEW" if warnings else "INFO"


def _status(raw: dict[str, Any], normalized: ReviewItem) -> str:
    explicit = str(raw.get("review_status") or raw.get("state") or normalized.status).casefold()
    if explicit in {"approved", "reviewed", "human_translation_approved"}:
        return ReviewStatus.APPROVED
    if explicit in {"resolved", "edited", "rejected", "preserved_original"}:
        return ReviewStatus.RESOLVED
    return ReviewStatus.PENDING


def _psd_maps(provenance: dict[int, dict[str, Any]] | None, root: Path) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for page, record in (provenance or {}).items():
        try:
            index = int(page)
        except (TypeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        rel = _relative_existing(record.get("path"), root)
        layers = record.get("text_layers") or []
        # Some exporters return count only; never synthesize a layer identity from a count.
        if isinstance(layers, int):
            layers = []
        result[index] = {"path": rel, "layers": [r for r in layers if isinstance(r, dict)]}
    return result


def _layer_for(raw: dict[str, Any], psd: dict[str, Any], used: set[int]) -> tuple[dict[str, Any] | None, str]:
    rid = str(raw.get("region_id") or raw.get("id") or "")
    if rid:
        for index, layer in enumerate(psd["layers"]):
            if index not in used and str(layer.get("region_id") or "") == rid:
                used.add(index)
                return layer, "region_id"
        return None, "region_id"
    # Legacy-only fallback: unique exact bbox + semantic type, and one-to-one assignment.
    box = raw.get("bounding_box")
    typ = str(raw.get("classification") or "")
    matches = [(i, layer) for i, layer in enumerate(psd["layers"])
               if i not in used and layer.get("bbox") == box
               and str(layer.get("semantic_type") or "") == typ]
    if len(matches) == 1:
        index, layer = matches[0]
        used.add(index)
        return layer, "legacy_exact_bbox_type"
    return None, "legacy_unlinked"


def build_review_manifest(*, quality_report: dict[str, Any] | None = None,
                          logical_page_count: int, job_outcome: str,
                          artifact_root: str | Path,
                          psd_provenance: dict[int, dict[str, Any]] | None = None,
                          effective_review_items: list[dict[str, Any]] | None = None,
                          page_artifacts: dict[int, dict[str, Any]] | None = None,
                          artifact_contract_version: int = 2) -> dict[str, Any]:
    """Build deterministic V1. ``effective_review_items`` overlays persisted Review values."""
    count = int(logical_page_count)
    if count <= 0:
        raise ValueError("review_manifest_page_count_invalid")
    root = Path(artifact_root).resolve()
    by_page = _page_and_items(quality_report or {})
    overlays: dict[tuple[int, str], dict[str, Any]] = {}
    for row in effective_review_items or []:
        if not isinstance(row, dict):
            continue
        try:
            page = int(row.get("page") or row.get("page_index") or 0)
        except (TypeError, ValueError):
            continue
        rid = str(row.get("region_id") or "")
        rid = rid.split(":", 1)[1] if re.match(r"^p\d{3}:", rid) else rid
        if page > 0 and rid:
            overlays[(page, rid)] = row

    psd_by_page = _psd_maps(psd_provenance, root)
    pages = []
    all_items: list[dict[str, Any]] = []
    for page_number in range(1, count + 1):
        psd = psd_by_page.get(page_number, {"path": None, "layers": []})
        used_layers: set[int] = set()
        raw_rows = list(by_page.get(page_number, []))
        # Re-export can provide the current review population even when a legacy QC report
        # did not contain the newer collections.
        represented_ids = {str(r.get("region_id") or r.get("id") or "") for r in raw_rows}
        for (overlay_page, overlay_id), overlay in overlays.items():
            if overlay_page == page_number and overlay_id not in represented_ids:
                raw_rows.append({"region_id": overlay_id, "id": overlay_id,
                                 "text": overlay.get("source_text_original") or overlay.get("source_text_effective") or "",
                                 "translation": overlay.get("target_text_original") or "",
                                 "preserved_original": bool(overlay.get("preserved_original")),
                                 "classification": overlay.get("classification") or "unknown",
                                 "bounding_box": overlay.get("bounding_box_effective") or overlay.get("bounding_box"),
                                 "manual_review_required": True})
        item_rows = []
        # Stable order: physical y, x, then canonical region identity; legacy tie order last.
        def order_key(raw: dict[str, Any]):
            box = raw.get("bounding_box") or []
            try:
                y, x = float(box[1]), float(box[0])
            except (TypeError, ValueError, IndexError):
                y, x = float("inf"), float("inf")
            rid = str(raw.get("region_id") or raw.get("id") or "")
            return y, x, rid
        raw_rows.sort(key=order_key)
        fallback_ordinals: dict[str, int] = {}
        for index, raw in enumerate(raw_rows):
            rid = str(raw.get("region_id") or "")
            item_id = str(raw.get("id") or rid or "")
            overlay = overlays.get((page_number, rid), {}) if rid else {}
            adapter_input = {
                **raw,
                "page": page_number,
                "region_id": rid,
                "source_text_original": overlay.get("source_text_original", raw.get("text") or ""),
                "source_text_effective": overlay.get("source_text_effective", raw.get("text") or ""),
                "target_text_original": raw.get("translation") or raw.get("translation_candidate") or "",
                "target_text_effective": overlay.get("target_text_effective", overlay.get("translation", raw.get("translation") or raw.get("translation_candidate") or "")),
                "state": overlay.get("state") or overlay.get("review_status") or "pending",
            }
            normalized = from_quality_review_item(adapter_input)
            psd_layer, link_mode = _layer_for(raw, psd, used_layers)
            legacy_id = None
            if not rid:
                box = raw.get("bounding_box")
                signature = json.dumps([page_number, index, box, raw.get("classification")],
                                       ensure_ascii=False, separators=(",", ":"), default=str)
                legacy_id = "legacy:" + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]
                link_mode = link_mode if psd_layer else "legacy_order_bbox_fallback"
            warnings = _warning_codes(raw, normalized,
                                      str(psd_layer.get("translation_status") or "") if psd_layer else "")
            source = str(adapter_input["source_text_effective"] or "")
            original_target = str(adapter_input["target_text_original"] or "")
            effective_target = str(adapter_input["target_text_effective"] or "")
            writer_status = str((psd_layer or {}).get("translation_status") or "").casefold()
            if writer_status == "source_fallback" or overlay.get("translation_status") == "SOURCE_FALLBACK" or (
                    raw.get("preserved_original")
                    and str(raw.get("translation_final_state") or "").casefold() != "translated"
                    and "target_text_effective" not in overlay):
                effective_target = source
                writer_status = "source_fallback"
            raw_reasons = raw.get("quality_reasons") or []
            if isinstance(raw_reasons, str):
                raw_reasons = [raw_reasons]
            bbox = raw.get("bounding_box")
            item = {
                "review_item_id": f"p{page_number:03d}:{rid}" if rid else legacy_id,
                "logical_page": page_number,
                "region_id": rid or None,
                "legacy_item_id": legacy_id,
                "semantic_type": normalized.semantic_type,
                "source_text": source,
                "provider_translation": str(raw.get("translation") or raw.get("translation_candidate") or "") or None,
                "translated_text_effective": effective_target or None,
                "translation_status": ("SOURCE_FALLBACK" if writer_status == "source_fallback"
                                       else writer_status.upper() if writer_status
                                       else str(raw.get("translation_final_state") or "UNKNOWN").upper()),
                "warnings": warnings,
                "quality_severity": _severity(warnings),
                "confidence": normalized.confidence,
                "review_status": _status(adapter_input, normalized).upper(),
                "bbox": list(bbox) if isinstance(bbox, (list, tuple)) else None,
                "position": {"x": bbox[0], "y": bbox[1]} if isinstance(bbox, (list, tuple)) and len(bbox) >= 2 else None,
                "typography_sources": dict((psd_layer or {}).get("typography_sources") or {}),
                "psd_artifact": psd["path"] if psd_layer else None,
                "psd_layer_name": str(psd_layer.get("name") or "") if psd_layer else None,
                "provenance": {"region_link_strategy": link_mode,
                               "quality_report_item_id": item_id or None,
                               "human_revision_version": int(overlay.get("revision_version") or overlay.get("version") or 0)},
            }
            if item["review_status"] not in {"PENDING", "APPROVED", "RESOLVED"}:
                raise ValueError("review_manifest_status_invalid")
            item_rows.append(item)
            all_items.append(item)
        artifacts = (page_artifacts or {}).get(page_number, {})
        page = {"logical_page": page_number, "review_items": item_rows}
        for field, value in artifacts.items():
            if field not in {"source_artifact", "translated_artifact", "clean_artifact", "editable_psd_artifact"}:
                continue
            safe = _safe_relative(value)
            if safe:
                page[field] = safe
        if psd.get("path"):
            page["editable_psd_artifact"] = psd["path"]
        pages.append(page)

    counts = {status: sum(item["review_status"] == status for item in all_items)
              for status in ("PENDING", "APPROVED", "RESOLVED")}
    body = {
        "review_manifest_version": REVIEW_MANIFEST_VERSION,
        "artifact_contract_version": int(artifact_contract_version),
        "job_outcome": str(job_outcome or ""),
        "review_required": str(job_outcome or "").casefold() == "review_required",
        "summary": {"logical_pages": count, "pages_represented": len(pages),
                    "items_total": len(all_items), "items_pending": counts["PENDING"],
                    "items_approved": counts["APPROVED"], "items_resolved": counts["RESOLVED"]},
        "pages": pages,
        "region_link_strategy": "region_id; legacy exact bbox+semantic type only when uniquely provable",
        "legacy_link_mode": "opaque legacy item ID; region_id remains null when source has none",
    }
    validate_review_manifest(body, artifact_root=root)
    return body


def validate_review_manifest(manifest: dict[str, Any], *, artifact_root: str | Path | None = None) -> None:
    if not isinstance(manifest, dict) or manifest.get("review_manifest_version") != 1 \
            or manifest.get("artifact_contract_version") != 2:
        raise ValueError("review_manifest_version_invalid")
    pages = manifest.get("pages")
    summary = manifest.get("summary")
    if not isinstance(pages, list) or not isinstance(summary, dict):
        raise ValueError("review_manifest_structure_invalid")
    expected = int(summary.get("logical_pages") or 0)
    numbers = [int(page.get("logical_page") or 0) for page in pages if isinstance(page, dict)]
    if expected <= 0 or len(numbers) != len(pages) or numbers != list(range(1, expected + 1)):
        raise ValueError("review_manifest_page_coverage_invalid")
    ids: set[str] = set()
    item_total = 0
    root = Path(artifact_root).resolve() if artifact_root is not None else None
    for page in pages:
        for field in ("source_artifact", "translated_artifact", "clean_artifact", "editable_psd_artifact"):
            if page.get(field):
                safe = _safe_relative(page[field])
                if safe != page[field]:
                    raise ValueError("review_manifest_path_invalid")
                if root is not None:
                    resolved = (root / safe).resolve()
                    if root not in resolved.parents:
                        raise ValueError("review_manifest_path_invalid")
                    if not resolved.is_file():
                        raise ValueError(f"review_manifest_artifact_missing:{safe}")
        for item in page.get("review_items", []):
            if not isinstance(item, dict) or int(item.get("logical_page") or 0) != page["logical_page"]:
                raise ValueError("review_manifest_item_invalid")
            item_id = str(item.get("review_item_id") or "")
            if not item_id or item_id in ids:
                raise ValueError("review_manifest_duplicate_item_id")
            ids.add(item_id)
            if item.get("review_status") not in {"PENDING", "APPROVED", "RESOLVED"}:
                raise ValueError("review_manifest_status_invalid")
            if item.get("psd_layer_name") and not item.get("psd_artifact"):
                raise ValueError("review_manifest_psd_link_invalid")
            if item.get("psd_artifact"):
                if _safe_relative(item["psd_artifact"]) != item["psd_artifact"]:
                    raise ValueError("review_manifest_path_invalid")
                if root is not None:
                    resolved = (root / item["psd_artifact"]).resolve()
                    if root not in resolved.parents:
                        raise ValueError("review_manifest_path_invalid")
                    if not resolved.is_file():
                        raise ValueError(f"review_manifest_artifact_missing:{item['psd_artifact']}")
            item_total += 1
    if item_total != int(summary.get("items_total", -1)) \
            or len(pages) != int(summary.get("pages_represented", -1)):
        raise ValueError("review_manifest_summary_invalid")
    for status, field in (("PENDING", "items_pending"), ("APPROVED", "items_approved"),
                          ("RESOLVED", "items_resolved")):
        if sum(1 for page in pages for item in page.get("review_items", [])
               if item.get("review_status") == status) != int(summary.get(field) or 0):
            raise ValueError("review_manifest_summary_invalid")
    _scan_sensitive_keys(manifest)


def _scan_sensitive_keys(value: Any) -> None:
    if isinstance(value, dict):
        if any(_SENSITIVE_KEY.search(str(key)) for key in value):
            raise ValueError("review_manifest_sensitive_field")
        for child in value.values():
            _scan_sensitive_keys(child)
    elif isinstance(value, list):
        for child in value:
            _scan_sensitive_keys(child)
    elif isinstance(value, str) and re.search(r"(?i)(?:[A-Z]:\\|\\\\[^\\]+\\[^\\]+)", value):
        raise ValueError("review_manifest_absolute_path")


def write_review_manifest(path: str | Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate and atomically publish UTF-8 JSON; PRODUCED must be set only after this returns."""
    started = time.perf_counter()
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    root = destination.parent.parent if destination.parent.name == "qc" else destination.parent
    validate_review_manifest(manifest, artifact_root=root)
    fd, temporary = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp",
                                     dir=str(destination.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        parsed = json.loads(Path(temporary).read_text(encoding="utf-8"))
        validate_review_manifest(parsed, artifact_root=root)
        os.replace(temporary, destination)
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass
    return {"write_seconds": round(time.perf_counter() - started, 6),
            "size_bytes": destination.stat().st_size}
