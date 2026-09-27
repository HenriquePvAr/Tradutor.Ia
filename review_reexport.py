"""Render a new output generation from an immutable human Review snapshot.

This path consumes persisted page/OCR/translation artifacts only.  It deliberately
has no downloader, OCR engine, provider client, or wallet dependency.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline_cache import atomic_write_json


class ReviewReexportError(ValueError):
    def __init__(self, code: str, blockers: list[dict[str, Any]] | None = None):
        super().__init__(code)
        self.code = code
        self.blockers = blockers or []


def _region_key(page_index: int, region_id: str) -> str:
    return f"p{page_index:03d}:{region_id}"


def _psd_regions_for_page(snapshot: dict[str, Any], page_index: int) -> list[dict[str, Any]]:
    """Region specs for the professional PSD exporter, from the frozen snapshot.

    A region gets a translated raster layer only when it is active, effectively set to
    ``translate`` and carries a non-empty target (IGNORE/empty never produces a layer).
    """
    regions: list[dict[str, Any]] = []
    for region in snapshot.get("regions", []) or []:
        if int(region.get("page_index") or 0) != page_index:
            continue
        regions.append({
            "region_id": str(region.get("region_id") or ""),
            "bbox": region.get("effective_bounding_box"),
            "source_text": str(region.get("effective_source_text") or ""),
            "target": str(region.get("effective_target_text") or ""),
            "region_type": str(region.get("effective_region_type") or ""),
            "translate_mode": str(region.get("translate_effective") or "translate"),
            "manual": bool(region.get("is_manual_region")),
            "active": bool(region.get("active", True)),
        })
    return regions


def build_review_snapshot(*, job: dict[str, Any], report: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    """Freeze all render-relevant Review values and original page artifacts."""
    pages: list[dict[str, Any]] = []
    for page in report.get("pages", []) or []:
        if not isinstance(page, dict):
            continue
        try:
            index = int(page.get("index") or page.get("sequence_index") or 0)
        except (TypeError, ValueError):
            continue
        if index <= 0:
            continue
        pages.append({"page_index": index, "source_path": str(page.get("image_path") or ""),
                      "translated_path": str(page.get("output_path") or ""),
                      "page_data": copy.deepcopy(page)})
    pages.sort(key=lambda p: p["page_index"])
    if not pages:
        raise ReviewReexportError("review_pages_missing")

    effective: dict[str, dict[str, Any]] = {}
    for row in review.get("items", []) or []:
        if not isinstance(row, dict) or row.get("type") != "region":
            continue
        page_index = int(row.get("page_index") or 0)
        region_id = str(row.get("region_id") or "")
        if page_index <= 0 or not region_id:
            continue
        key = region_id.split(":", 1)[1] if region_id.startswith("p") and ":" in region_id else region_id
        effective[_region_key(page_index, key)] = {
            "page_index": page_index, "region_id": key,
            "effective_source_text": str(row.get("source_text_effective") or ""),
            "effective_target_text": str(row.get("target_text_effective") or ""),
            "effective_translate_mode": str(row.get("translate_mode_effective") or "auto"),
            "effective_region_type": str(row.get("region_type_effective") or row.get("classification") or "unknown"),
            "effective_bounding_box": row.get("bounding_box_effective") or row.get("bounding_box"),
            "is_manual_region": bool(row.get("is_manual_region")),
            "active": not bool(row.get("is_inactive")),
            "revision_version": int(row.get("revision_version") or 0),
            "region_control_version": int(row.get("region_control_version") or 0),
            "translation_stale": bool(row.get("translation_stale")),
            "translate_effective": str(row.get("translate_effective") or "ignore"),
        }
    body = {
        "schema_version": 1, "job_id": str(job["id"]),
        "run_id": str(job.get("run_id") or ""), "pages": pages,
        "regions": sorted(effective.values(), key=lambda r: (r["page_index"], r["region_id"])),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provider_policy": {"allow_provider": False, "cache_only": True},
        "additional_yk_required": 0,
    }
    identity = {
        "schema_version": body["schema_version"], "job_id": body["job_id"], "run_id": body["run_id"],
        "pages": [{key: page[key] for key in ("page_index", "source_path", "translated_path")} for page in pages],
        "regions": body["regions"],
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    body["snapshot_id"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return body


def validate_review_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    pages = {int(p["page_index"]): p for p in snapshot.get("pages", [])}
    for region in snapshot.get("regions", []):
        if not region.get("active", True):
            continue
        page = int(region.get("page_index") or 0)
        ident = str(region.get("region_id") or "")
        def block(reason: str) -> None:
            blockers.append({"page_index": page, "region_id": ident, "reason": reason})
        if page not in pages:
            block("review_page_missing")
            continue
        if region.get("translate_effective") == "translate" and region.get("translation_stale"):
            block("review_translation_stale")
        if region.get("translate_effective") == "translate" and not str(region.get("effective_target_text") or "").strip():
            block("review_target_required")
        box = region.get("effective_bounding_box")
        if box is not None:
            try:
                x, y, w, h = [float(v) for v in box]
                if x < 0 or y < 0 or w <= 0 or h <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                block("review_bbox_invalid")
    return blockers


def _apply_snapshot(page: dict[str, Any], page_index: int, snapshot: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(page)
    debug = result.setdefault("debug_data", {})
    items = debug.setdefault("items", [])
    controls = {r["region_id"]: r for r in snapshot.get("regions", [])
                if int(r.get("page_index") or 0) == page_index and r.get("active", True)}
    present: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("region_id") or item.get("id") or "")
        control = controls.get(rid)
        if not control:
            continue
        present.add(rid)
        item["text"] = control["effective_source_text"]
        item["clean_text"] = control["effective_source_text"]
        item["repaired_text"] = control["effective_source_text"]
        item["translation"] = control["effective_target_text"]
        item["translation_candidate"] = control["effective_target_text"]
        item["translation_valid"] = True
        item["translation_final_state"] = "translated" if control["effective_target_text"] else ""
        item["translation_final_reason"] = "review_snapshot"
        item["classification"] = control["effective_region_type"]
        item["review_translate_override"] = control["effective_translate_mode"]
        box = control.get("effective_bounding_box")
        if box:
            x, y, w, h = [int(round(float(v))) for v in box]
            item["bounding_box"] = [x, y, w, h]
            item["draw_box"] = [x, y, w, h]
            item["translation_box"] = [x, y, x + w, y + h]
        if control["effective_translate_mode"] == "translate":
            item["sent_to_translation"] = True
            item["sent_to_nvidia"] = True
            item["preserved_original"] = False
            item["ignored"] = False
            item["manual_review_required"] = False
    for rid, control in controls.items():
        if not control.get("is_manual_region") or rid in present:
            continue
        box = control.get("effective_bounding_box")
        if not box:
            continue
        x, y, w, h = [int(round(float(v))) for v in box]
        text = control["effective_source_text"]
        items.append({"id": rid, "region_id": rid, "text": text, "clean_text": text,
                      "repaired_text": text, "translation": control["effective_target_text"],
                      "translation_candidate": control["effective_target_text"],
                      "classification": control["effective_region_type"],
                      "bounding_box": [x, y, w, h], "draw_box": [x, y, w, h],
                      "translation_box": [x, y, x + w, y + h], "confidence": 1.0,
                      "translation_valid": True,
                      "translation_final_state": "translated" if control["effective_target_text"] else "",
                      "sent_to_translation": control["effective_translate_mode"] == "translate",
                      "sent_to_nvidia": control["effective_translate_mode"] == "translate",
                      "review_translate_override": control["effective_translate_mode"],
                      "manual_review_required": False, "preserved_original": False})
    return result


def render_review_snapshot(*, snapshot: dict[str, Any], output_root: str | Path,
                           output_format: str, chapter_name: str,
                           staging_root: str | Path | None = None,
                           progress: Any = None, typesetting_mode: str = "on") -> dict[str, Any]:
    """Render/export one generation using persisted page data; no OCR/provider calls."""
    blockers = validate_review_snapshot(snapshot)
    if blockers:
        raise ReviewReexportError("review_blocked", blockers)
    import cv2
    from chapter_quality_revision import ChapterQualityRevision
    from ocr_balloon import render_analyzed_image
    from pdf import generate_pdf

    root = Path(output_root).resolve()
    generation = f"{snapshot['snapshot_id'][:16]}-{uuid.uuid4().hex[:12]}"
    parent = Path(staging_root).resolve() if staging_root is not None else root / "review_reexports"
    parent.mkdir(parents=True, exist_ok=True)
    dest = parent / generation
    staging = Path(tempfile.mkdtemp(prefix=".review-reexport-", dir=str(parent)))
    page_pairs: list[tuple[Path, Path]] = []
    page_paths: list[Path] = []
    psd_pages: list[dict[str, Any]] = []  # per-page arrays for the professional PSD exporter
    engine = object.__new__(ChapterQualityRevision)
    try:
        for offset, entry in enumerate(snapshot["pages"], 1):
            page_index = int(entry["page_index"])
            if callable(progress):
                progress("reconstructing", offset, len(snapshot["pages"]), page_index)
            source = Path(entry["source_path"])
            if not source.is_absolute():
                source = root / source
            source = source.resolve()
            if not source.is_file() or not _within(source, root):
                raise ReviewReexportError("review_source_page_unavailable", [{"page_index": page_index, "region_id": "", "reason": "review_source_page_unavailable"}])
            image = cv2.imread(str(source), cv2.IMREAD_COLOR)
            if image is None:
                raise ReviewReexportError("review_source_page_unavailable")
            for region in snapshot.get("regions", []):
                if int(region.get("page_index") or 0) != page_index or not region.get("active", True):
                    continue
                box = region.get("effective_bounding_box")
                if box is not None:
                    try:
                        x, y, w, h = [float(v) for v in box]
                    except (TypeError, ValueError):
                        raise ReviewReexportError("review_bbox_invalid", [{"page_index": page_index, "region_id": str(region.get("region_id") or ""), "reason": "review_bbox_invalid"}]) from None
                    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > image.shape[1] or y + h > image.shape[0]:
                        raise ReviewReexportError("review_bbox_invalid", [{"page_index": page_index, "region_id": str(region.get("region_id") or ""), "reason": "review_bbox_out_of_bounds"}])
            page = _apply_snapshot(entry["page_data"], page_index, snapshot)
            groups = ChapterQualityRevision._groups_from_page_items(engine, page)
            capture: dict[str, Any] = {}
            rendered, _debug = render_analyzed_image(image, [], [], groups, page_index=page_index,
                                                     image_path=str(source), capture=capture)
            if rendered is None:
                raise ReviewReexportError("review_render_failed")
            target = staging / f"page_{page_index:04d}.png"
            if not cv2.imwrite(str(target), rendered):
                raise ReviewReexportError("review_output_write_failed")
            page_paths.append(target)
            page_pairs.append((source, target))
            if str(output_format or "").casefold() == "psd":
                cleaned = capture.get("cleaned_bgr")
                if cleaned is None:
                    cleaned = image  # no inpaint occurred (nothing to remove): cleaned == source
                psd_pages.append({
                    "page_index": page_index, "original_bgr": image,
                    "cleaned_bgr": cleaned, "translated_bgr": rendered,
                    "regions": _psd_regions_for_page(snapshot, page_index),
                })
        fmt = str(output_format or "pdf").casefold()
        if fmt not in {"png", "pdf", "psd"}:
            raise ReviewReexportError("review_output_format_unsupported")
        if callable(progress):
            progress("exporting", len(page_paths), len(page_paths), 0)
        if fmt == "png":
            artifact = staging
        elif fmt == "pdf":
            artifact = staging / f"{_safe_name(chapter_name)}.pdf"
            generate_pdf([str(p) for p in page_paths], str(artifact))
        else:
            from professional_psd import export_professional_psd
            artifact = export_professional_psd(psd_pages, staging, chapter_name,
                                               typesetting_mode=typesetting_mode)
        lineage = {"schema_version": 1, "source_job_id": snapshot["job_id"],
                   "source_run_id": snapshot["run_id"], "review_snapshot_id": snapshot["snapshot_id"],
                   "output_generation": generation, "output_format": fmt,
                   "typesetting_mode": str(typesetting_mode or "on").casefold() if fmt == "psd" else "",
                   "created_at": datetime.now(timezone.utc).isoformat(),
                   "provider_calls_added": 0, "ocr_calls_added": 0, "additional_yk_required": 0,
                   "artifact": str(artifact.relative_to(staging)) if artifact.is_relative_to(staging) else artifact.name,
                   "pages": [p.name for p in page_paths]}
        atomic_write_json(staging / "review_snapshot.json", snapshot)
        atomic_write_json(staging / "reconstruction_manifest.json", lineage)
        os.replace(staging, dest)
        artifact = dest if fmt == "png" else dest / lineage["artifact"]
        return {"snapshot_id": snapshot["snapshot_id"], "output_dir": str(dest), "artifact_path": str(artifact),
                "output_format": fmt, "page_count": len(page_paths), "lineage": lineage}
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_name(value: str) -> str:
    name = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(value or "chapter"))
    return name.strip("._") or "chapter"
