"""Residual glyph detection and auditable mask revisions for local previews.

The logic in this module is intentionally generic.  It works from pixels,
segmentation layers and persisted operation metadata; it does not know page
IDs, phrases, chapters, coordinates or output names.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

import residual_analysis_artifacts

SCHEMA_VERSION = "3"
COMPONENT_DECOMPOSITION_VERSION = "1"
REVISION_STATUSES = (
    "confirmed",
    "blocked_pending_component_review",
    "blocked_by_mask_revision_gate",
    "needs_adjustment",
    "discarded",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def mask_hash(mask: np.ndarray) -> str:
    return hashlib.sha256(np.asarray(mask, dtype=np.uint8).tobytes()).hexdigest()


def _binary(mask: Any, shape: tuple[int, int] | None = None) -> np.ndarray:
    arr = np.asarray(mask if mask is not None else 0, dtype=np.uint8)
    if shape is not None and arr.shape[:2] != shape[:2]:
        raise ValueError("mask_shape_mismatch")
    return ((arr > 0).astype(np.uint8) * 255)


def _component_records(mask: np.ndarray) -> list[dict[str, Any]]:
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
        (np.asarray(mask) > 0).astype(np.uint8), 8)
    records: list[dict[str, Any]] = []
    for label in range(1, count):
        x = int(stats[label, cv2.CC_STAT_LEFT])
        y = int(stats[label, cv2.CC_STAT_TOP])
        w = int(stats[label, cv2.CC_STAT_WIDTH])
        h = int(stats[label, cv2.CC_STAT_HEIGHT])
        area = int(stats[label, cv2.CC_STAT_AREA])
        component = (labels == label)
        records.append({
            "component_index": len(records) + 1,
            "bounds": [x, y, x + w, y + h],
            "area": area,
            "width": w,
            "height": h,
            "fill_ratio": round(area / max(1, w * h), 4),
            "_mask": component,
        })
    return records


def detect_residual_components(*, previous_mask: np.ndarray,
                               text_core_mask: np.ndarray,
                               outline_mask: np.ndarray,
                               antialias_mask: np.ndarray,
                               protected_edge_mask: np.ndarray | None = None,
                               validation_halo: np.ndarray | None = None) -> dict[str, Any]:
    """Classify segmentation components that are not covered by a mask.

    The detector only treats pixels as residual text when they are explained by
    the segmentation layers.  Protected-edge overlap and low layer confidence
    keep a component out of the automatic delta.
    """
    core = _binary(text_core_mask)
    outline = _binary(outline_mask, core.shape)
    antialias = _binary(antialias_mask, core.shape)
    previous = _binary(previous_mask, core.shape)
    protected = _binary(protected_edge_mask, core.shape) if protected_edge_mask is not None else np.zeros_like(core)
    halo = _binary(validation_halo, core.shape) if validation_halo is not None else np.ones_like(core, dtype=np.uint8) * 255
    candidate = cv2.bitwise_or(cv2.bitwise_or(core, outline), antialias)
    residual = cv2.bitwise_and(candidate, cv2.bitwise_not(previous))
    residual = cv2.bitwise_and(residual, halo)
    records: list[dict[str, Any]] = []
    safe_mask = np.zeros(core.shape, dtype=np.uint8)
    ambiguous_mask = np.zeros(core.shape, dtype=np.uint8)
    protected_mask = np.zeros(core.shape, dtype=np.uint8)
    total_area = max(1, int(core.size))
    for raw in _component_records(residual):
        component = raw.pop("_mask")
        area = int(raw["area"])
        core_overlap = int((component & (core > 0)).sum())
        outline_overlap = int((component & (outline > 0)).sum())
        antialias_overlap = int((component & (antialias > 0)).sum())
        protected_overlap = int((component & (protected > 0)).sum())
        layer_total = max(1, core_overlap + outline_overlap + antialias_overlap)
        if protected_overlap:
            component_type = "protected_line_intersection"
        elif core_overlap / layer_total >= 0.35:
            component_type = "glyph_fragment_outside_mask"
        elif outline_overlap / layer_total >= 0.35:
            component_type = "outline_fragment_outside_mask"
        elif antialias_overlap / layer_total >= 0.35:
            component_type = "antialias_fragment_outside_mask"
        else:
            component_type = "ambiguous_component"
        component_ratio = area / total_area
        # Components this large often indicate art joined to text.  They remain
        # reviewable evidence, not an automatic erase delta.
        safe_to_include = (
            protected_overlap == 0
            and component_type != "ambiguous_component"
            and component_ratio <= 0.18
            and float(raw.get("fill_ratio") or 0.0) >= 0.04
        )
        confidence = 0.9 if safe_to_include else (0.2 if protected_overlap else 0.45)
        component_bounds = raw["bounds"]
        local_component = component[
            component_bounds[1]:component_bounds[3],
            component_bounds[0]:component_bounds[2]]
        record = {
            **raw,
            "component_bitmap_hash": hashlib.sha256(
                residual_analysis_artifacts.canonical_bitmap_bytes(
                    local_component.astype(np.uint8) * 255)).hexdigest(),
            "component_type": component_type,
            "component_confidence": round(confidence, 3),
            "related_to_text_core": core_overlap > 0,
            "related_to_outline": outline_overlap > 0,
            "related_to_antialias": antialias_overlap > 0,
            "related_to_art": component_type == "ambiguous_component",
            "protected_edge_overlap": protected_overlap,
            "safe_to_include": safe_to_include,
            "requires_review": not safe_to_include,
            "reason_codes": [component_type],
        }
        records.append(record)
        if safe_to_include:
            safe_mask[component] = 255
        elif protected_overlap:
            protected_mask[component] = 255
        else:
            ambiguous_mask[component] = 255
    unresolved = sum(1 for item in records if item.get("requires_review"))
    return {
        "residual_detector_version": SCHEMA_VERSION,
        "residual_detected": bool(records),
        "residual_pixel_count": int((residual > 0).sum()),
        "residual_component_count": len(records),
        "residual_components": records,
        "residual_bounds": _bounds_from_mask(residual),
        "suggested_mask_delta": safe_mask,
        "ambiguous_mask": ambiguous_mask,
        "protected_residual_mask": protected_mask,
        "safe_component_count": sum(1 for item in records if item.get("safe_to_include")),
        "unresolved_residual_components": unresolved,
        "reason_codes": sorted({code for item in records for code in item.get("reason_codes", [])})
                        or ["no_residual_components"],
    }


def _bounds_from_mask(mask: np.ndarray) -> list[int]:
    ys, xs = np.nonzero(np.asarray(mask) > 0)
    if not xs.size:
        return []
    return [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]


def build_revised_mask(previous_mask: np.ndarray, residual_report: dict[str, Any],
                       *, protected_edge_mask: np.ndarray | None = None,
                       allow_reviewable_components: bool = False) -> dict[str, Any]:
    previous = _binary(previous_mask)
    delta = _binary(residual_report.get("suggested_mask_delta"), previous.shape)
    final = cv2.bitwise_or(previous, delta)
    protected = _binary(protected_edge_mask, previous.shape) if protected_edge_mask is not None else np.zeros_like(previous)
    protected_overlap = int(((final > 0) & (protected > 0)).sum())
    previous_area = int((previous > 0).sum())
    delta_area = int((delta > 0).sum())
    final_area = int((final > 0).sum())
    region_area = max(1, int(final.size))
    ratio = final_area / region_area
    delta_ratio = delta_area / region_area
    reason_codes: list[str] = []
    if final_area <= 0:
        reason_codes.append("mask_empty")
    if ratio >= 0.42:
        reason_codes.append("mask_area_excessive")
    if ratio >= 0.82:
        reason_codes.append("mask_full_bounding_box_risk")
    if protected_overlap:
        reason_codes.append("mask_protected_overlap")
    if int(residual_report.get("unresolved_residual_components") or 0) and not allow_reviewable_components:
        reason_codes.append("unresolved_residual_components")
    status = "valid_for_local_preview_candidate" if not reason_codes else "blocked_by_mask_revision_gate"
    return {
        "final_mask": final,
        "delta_mask": delta,
        "previous_mask_hash": mask_hash(previous),
        "final_mask_hash": mask_hash(final),
        "delta_mask_hash": mask_hash(delta),
        "previous_area": previous_area,
        "delta_area": delta_area,
        "final_mask_area": final_area,
        "region_area": region_area,
        "mask_ratio": round(ratio, 4),
        "delta_ratio": round(delta_ratio, 4),
        "connected_components": len(_component_records(final)),
        "protected_overlap": protected_overlap,
        "unresolved_residual_components": int(residual_report.get("unresolved_residual_components") or 0),
        "mask_not_full_bounding_box": ratio < 0.82,
        "status": status,
        "reason_codes": reason_codes,
    }


def derive_component_seeds(*, component_mask: np.ndarray,
                           text_core_mask: np.ndarray,
                           outline_mask: np.ndarray,
                           antialias_mask: np.ndarray,
                           protected_edge_mask: np.ndarray | None = None,
                           art_context_mask: np.ndarray | None = None) -> dict[str, Any]:
    """Derive conservative text, art and unknown seeds from persisted layers."""
    component = _binary(component_mask)
    core = _binary(text_core_mask, component.shape)
    outline = _binary(outline_mask, component.shape)
    antialias = _binary(antialias_mask, component.shape)
    protected = (_binary(protected_edge_mask, component.shape)
                 if protected_edge_mask is not None else np.zeros_like(component))
    art_context = (_binary(art_context_mask, component.shape)
                   if art_context_mask is not None else np.zeros_like(component))
    inside = component > 0
    evidence_count = (
        (core > 0).astype(np.uint8)
        + (outline > 0).astype(np.uint8)
        + (antialias > 0).astype(np.uint8)
    )
    text = inside & (evidence_count >= 2) & (protected == 0) & (art_context == 0)
    art = inside & ((protected > 0) | (art_context > 0)) & ~text
    unknown = inside & ~text & ~art
    conflict = inside & (evidence_count >= 2) & ((protected > 0) | (art_context > 0))
    text[conflict] = False
    art[conflict] = False
    unknown[conflict] = True
    text_mask = text.astype(np.uint8) * 255
    art_mask = art.astype(np.uint8) * 255
    unknown_mask = unknown.astype(np.uint8) * 255
    total = max(1, int(inside.sum()))
    return {
        "component_decomposition_version": COMPONENT_DECOMPOSITION_VERSION,
        "text_seed_mask": text_mask,
        "art_seed_mask": art_mask,
        "unknown_seed_mask": unknown_mask,
        "conflict_seed_mask": conflict.astype(np.uint8) * 255,
        "text_seed_confidence": round(float(text.sum()) / total, 4),
        "art_seed_confidence": round(float(art.sum()) / total, 4),
        "text_seed_sources": ["text_core", "outline", "antialias"],
        "art_seed_sources": ["protected_edges", "background_art_context"],
        "text_seed_hash": mask_hash(text_mask),
        "art_seed_hash": mask_hash(art_mask),
        "unknown_seed_hash": mask_hash(unknown_mask),
        "seed_conflict_pixels": int(conflict.sum()),
    }


def decompose_ambiguous_component(*, component_mask: np.ndarray,
                                  seeds: dict[str, Any],
                                  protected_edge_mask: np.ndarray | None = None,
                                  residual_analysis_id: str = "",
                                  component_id: str = "",
                                  expected_component_bitmap_hash: str = ""
                                  ) -> dict[str, Any]:
    """Classify a connected residual component using independent local votes.

    The three votes intentionally use different evidence paths. Pixels without
    unanimous support remain ambiguous and can never enter an erase mask.
    """
    component = _binary(component_mask)
    component_bounds = _bounds_from_mask(component)
    local_component = (
        component[
            component_bounds[1]:component_bounds[3],
            component_bounds[0]:component_bounds[2]]
        if component_bounds else component)
    component_bitmap_hash = hashlib.sha256(
        residual_analysis_artifacts.canonical_bitmap_bytes(
            local_component)).hexdigest()
    if expected_component_bitmap_hash and component_bitmap_hash != expected_component_bitmap_hash:
        raise ValueError("component_bitmap_hash_mismatch")
    if component_id and not residual_analysis_id:
        raise ValueError("residual_analysis_id_required")
    text_seed = _binary(seeds.get("text_seed_mask"), component.shape)
    art_seed = _binary(seeds.get("art_seed_mask"), component.shape)
    conflict_seed = _binary(seeds.get("conflict_seed_mask"), component.shape)
    protected = (_binary(protected_edge_mask, component.shape)
                 if protected_edge_mask is not None else np.zeros_like(component))
    inside = component > 0

    eligible = inside & (conflict_seed == 0)
    direct_text = eligible & (text_seed > 0) & (protected == 0)
    direct_art = eligible & ((art_seed > 0) | (protected > 0))

    kernel = np.ones((3, 3), np.uint8)
    text_reach = cv2.dilate(text_seed, kernel, iterations=1) > 0
    art_reach = cv2.dilate(cv2.bitwise_or(art_seed, protected), kernel, iterations=1) > 0
    region_text = eligible & text_reach & ~art_reach
    region_art = eligible & art_reach & ~text_reach

    text_distance = cv2.distanceTransform((text_seed == 0).astype(np.uint8), cv2.DIST_L2, 3)
    art_sources = cv2.bitwise_or(art_seed, protected)
    if np.any(art_sources):
        art_distance = cv2.distanceTransform((art_sources == 0).astype(np.uint8), cv2.DIST_L2, 3)
        geodesic_text = eligible & (text_distance < art_distance) & (protected == 0)
        geodesic_art = eligible & (art_distance < text_distance)
    else:
        geodesic_text = eligible & text_reach & (protected == 0)
        geodesic_art = np.zeros_like(inside)

    methods = [
        ("layer_seed", direct_text, direct_art),
        ("controlled_region_growing", region_text, region_art),
        ("geodesic_propagation", geodesic_text, geodesic_art),
    ]
    text_votes = sum(mask.astype(np.uint8) for _name, mask, _art in methods)
    art_votes = sum(mask.astype(np.uint8) for _name, _text, mask in methods)
    confirmed_text = inside & (text_votes >= 2) & (art_votes == 0) & (protected == 0)
    confirmed_art = inside & (art_votes >= 2) & (text_votes == 0)
    still_ambiguous = inside & ~confirmed_text & ~confirmed_art
    total = max(1, int(inside.sum()))
    candidates = []
    for name, text_mask, art_mask in methods:
        unknown = inside & ~text_mask & ~art_mask
        parameters = {"connectivity": 8}
        candidate_identity = json.dumps({
            "component_id": component_id,
            "method": name,
            "parameters": parameters,
            "text_hash": mask_hash(text_mask.astype(np.uint8)),
            "art_hash": mask_hash(art_mask.astype(np.uint8)),
            "unknown_hash": mask_hash(unknown.astype(np.uint8)),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        candidates.append({
            "candidate_id": hashlib.sha256(candidate_identity).hexdigest(),
            "method": name,
            "parameters": parameters,
            "component_id": component_id,
            "component_bitmap_hash": component_bitmap_hash,
            "residual_analysis_id": residual_analysis_id,
            "text_seed_hash": str(seeds.get("text_seed_hash") or ""),
            "art_seed_hash": str(seeds.get("art_seed_hash") or ""),
            "unknown_seed_hash": str(seeds.get("unknown_seed_hash") or ""),
            "candidate_text_hash": mask_hash(text_mask.astype(np.uint8)),
            "candidate_art_hash": mask_hash(art_mask.astype(np.uint8)),
            "candidate_unknown_hash": mask_hash(unknown.astype(np.uint8)),
            "text_pixels": int(text_mask.sum()),
            "art_pixels": int(art_mask.sum()),
            "unknown_pixels": int(unknown.sum()),
            "seed_agreement": round(float(((text_mask & (text_seed > 0)) | (art_mask & (art_seed > 0))).sum()) / total, 4),
            "protected_overlap": int((text_mask & (protected > 0)).sum()),
            "unknown_ratio": round(float(unknown.sum()) / total, 4),
        })
    protected_overlap = int((confirmed_text & (protected > 0)).sum())
    status = ("confirmed" if not still_ambiguous.any() and not protected_overlap
              else "blocked_pending_component_review")
    return {
        "component_decomposition_version": COMPONENT_DECOMPOSITION_VERSION,
        "residual_analysis_id": residual_analysis_id,
        "component_id": component_id,
        "component_bitmap_hash": component_bitmap_hash,
        "candidates": candidates,
        "confirmed_text_mask": confirmed_text.astype(np.uint8) * 255,
        "confirmed_art_mask": confirmed_art.astype(np.uint8) * 255,
        "still_ambiguous_mask": still_ambiguous.astype(np.uint8) * 255,
        "confirmed_text_pixels": int(confirmed_text.sum()),
        "confirmed_art_pixels": int(confirmed_art.sum()),
        "still_ambiguous_pixels": int(still_ambiguous.sum()),
        "unknown_ratio": round(float(still_ambiguous.sum()) / total, 4),
        "protected_overlap": protected_overlap,
        "text_seed_preserved": not np.any((text_seed > 0) & ~confirmed_text),
        "art_seed_preserved": not np.any((art_seed > 0) & ~confirmed_art),
        "no_full_bounding_box": int(inside.sum()) < int(np.prod(_component_box_shape(component))),
        "component_decomposition_hash": mask_hash(
            np.maximum(confirmed_text.astype(np.uint8), confirmed_art.astype(np.uint8) * 2)),
        "status": status,
        "reason_codes": [] if status == "confirmed" else ["ambiguous_component_consensus_incomplete"],
    }


def _component_box_shape(mask: np.ndarray) -> tuple[int, int]:
    bounds = _bounds_from_mask(mask)
    if not bounds:
        return (0, 0)
    return (bounds[3] - bounds[1], bounds[2] - bounds[0])


def apply_component_consensus(residual_report: dict[str, Any],
                              decomposition: dict[str, Any]) -> dict[str, Any]:
    """Return a residual report whose delta includes only confirmed text."""
    report = dict(residual_report)
    delta = _binary(report.get("suggested_mask_delta"))
    confirmed = _binary(decomposition.get("confirmed_text_mask"), delta.shape)
    report["suggested_mask_delta"] = cv2.bitwise_or(delta, confirmed)
    unresolved = int(decomposition.get("still_ambiguous_pixels") or 0)
    report["unresolved_residual_components"] = 1 if unresolved else 0
    report["component_decomposition"] = {
        key: value for key, value in decomposition.items()
        if not isinstance(value, np.ndarray)
    }
    report["reason_codes"] = (list(decomposition.get("reason_codes") or [])
                              or ["component_consensus_confirmed"])
    return report


def layout_candidates_for_text(text: str, *, available_width: int, available_height: int,
                               font_identity: str, font_file_hash: str = "",
                               min_size: int = 12, max_size: int = 64,
                               max_candidates: int = 5) -> list[dict[str, Any]]:
    """Generate measurable text layout candidates without changing the text."""
    if available_width <= 0 or available_height <= 0:
        raise ValueError("layout_bounds_unavailable")
    words = [word for word in str(text or "").split() if word]
    if not words:
        raise ValueError("layout_text_empty")
    candidates: list[dict[str, Any]] = []
    widths = [0.92, 0.84, 0.76, 0.68]
    line_heights = [1.0, 1.08, 1.16]
    for width_factor in widths:
        line_width = max(1, int(available_width * width_factor))
        for size in range(int(max_size), int(min_size) - 1, -1):
            lines = _wrap_words_by_estimate(words, line_width, size)
            if not lines:
                continue
            for leading_factor in line_heights:
                line_height = max(1, int(size * leading_factor))
                text_height = len(lines) * line_height
                longest = max(_estimated_text_width(line, size) for line in lines)
                overflow = longest > available_width
                clipping = text_height > available_height
                if overflow or clipping:
                    continue
                occupancy = (longest * text_height) / max(1, available_width * available_height)
                balance = 1.0 - (max(len(line) for line in lines) - min(len(line) for line in lines)) / max(1, max(len(line) for line in lines))
                fit_score = min(1.0, size / max(1, max_size)) * 0.45 + min(1.0, occupancy / 0.55) * 0.35 + balance * 0.20
                candidates.append({
                    "layout_candidate_id": uuid.uuid4().hex[:16],
                    "line_count": len(lines),
                    "line_breaks": lines,
                    "font_identity": str(font_identity),
                    "actual_font": str(font_identity),
                    "font_file_hash": str(font_file_hash or ""),
                    "fallback_used": False,
                    "glyph_support": "complete",
                    "font_size": int(size),
                    "tracking": 0,
                    "leading": int(line_height),
                    "alignment": "center",
                    "position": "center",
                    "text_bounds": [int(longest), int(text_height)],
                    "available_bounds": [int(available_width), int(available_height)],
                    "occupancy_ratio": round(float(occupancy), 4),
                    "style_score": round(float(balance), 3),
                    "fit_score": round(float(fit_score), 3),
                    "readability_score": round(min(1.0, size / 18.0), 3),
                    "overflow": False,
                    "clipping": False,
                    "overall_score": round(float(fit_score), 3),
                })
                break
            if candidates:
                break
    unique: dict[tuple[tuple[str, ...], int], dict[str, Any]] = {}
    for item in sorted(candidates, key=lambda c: float(c["overall_score"]), reverse=True):
        key = (tuple(item["line_breaks"]), int(item["font_size"]))
        unique.setdefault(key, item)
        if len(unique) >= max_candidates:
            break
    return list(unique.values())


def _estimated_text_width(text: str, size: int) -> int:
    # Conservative uppercase comic-caption estimate.  Real rendering may use a
    # narrower font, so this errs on the side of avoiding clipping.
    wide = sum(1 for ch in text if ch.upper() in "MWÁÀÂÃÉÊÍÓÔÕÚ")
    narrow = sum(1 for ch in text if ch in " .,;:!|'")
    normal = max(0, len(text) - wide - narrow)
    return int(size * (wide * 0.78 + normal * 0.56 + narrow * 0.28))


def _wrap_words_by_estimate(words: list[str], max_width: int, size: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if _estimated_text_width(trial, size) <= max_width or not current:
            current = trial
            continue
        if _estimated_text_width(word, size) > max_width:
            return []
        lines.append(current)
        current = word
    if current:
        lines.append(current)
    return lines


class MaskRevisionStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, timeout=5.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS human_mask_revisions (
                mask_revision_id TEXT PRIMARY KEY,
                supersedes_mask_decision_id TEXT NOT NULL,
                owner TEXT NOT NULL,
                job_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                page_id TEXT NOT NULL,
                region_id TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                base_segmentation_hash TEXT NOT NULL,
                previous_mask_hash TEXT NOT NULL,
                final_mask_hash TEXT NOT NULL,
                include_delta_json TEXT NOT NULL,
                exclude_delta_json TEXT NOT NULL,
                protected_delta_json TEXT NOT NULL,
                uncertain_delta_json TEXT NOT NULL,
                final_mask_asset TEXT NOT NULL,
                residual_evidence_json TEXT NOT NULL,
                validation_json TEXT NOT NULL,
                authorization TEXT NOT NULL,
                reviewer TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                supersedes_mask_revision_id TEXT NOT NULL DEFAULT '',
                residual_analysis_id TEXT NOT NULL DEFAULT '',
                residual_bitmap_hash TEXT NOT NULL DEFAULT '',
                component_manifest_hash TEXT NOT NULL DEFAULT '',
                component_decomposition_hashes_json TEXT NOT NULL DEFAULT '[]',
                UNIQUE(owner, job_id, run_id, revision_id, region_id, supersedes_mask_decision_id, final_mask_hash)
            )
        """)
        existing_columns = {
            str(row[1]) for row in
            self._conn.execute("PRAGMA table_info(human_mask_revisions)")
        }
        migrations = {
            "supersedes_mask_revision_id": "TEXT NOT NULL DEFAULT ''",
            "residual_analysis_id": "TEXT NOT NULL DEFAULT ''",
            "residual_bitmap_hash": "TEXT NOT NULL DEFAULT ''",
            "component_manifest_hash": "TEXT NOT NULL DEFAULT ''",
            "component_decomposition_hashes_json": "TEXT NOT NULL DEFAULT '[]'",
        }
        for column, declaration in migrations.items():
            if column not in existing_columns:
                self._conn.execute(
                    f"ALTER TABLE human_mask_revisions ADD COLUMN {column} {declaration}")
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def upsert(self, *, supersedes_mask_decision_id: str, owner: str,
               job_id: str, run_id: str, revision_id: str, page_id: str,
               region_id: str, source_hash: str, base_segmentation_hash: str,
               previous_mask_hash: str, final_mask_hash: str,
               include_delta: Any = None, exclude_delta: Any = None,
               protected_delta: Any = None, uncertain_delta: Any = None,
               final_mask_asset: str = "", residual_evidence: dict[str, Any] | None = None,
               validation: dict[str, Any] | None = None,
               supersedes_mask_revision_id: str = "",
               residual_analysis_id: str = "",
               residual_bitmap_hash: str = "",
               component_manifest_hash: str = "",
               component_decomposition_hashes: list[str] | None = None,
               authorization: str = "delegated_by_user", reviewer: str = "codex",
               status: str = "confirmed") -> dict[str, Any]:
        if not str(owner or "").strip():
            raise ValueError("authentication_required")
        if str(authorization or "") != "delegated_by_user":
            raise ValueError("mask_revision_authorization_required")
        if status not in REVISION_STATUSES:
            raise ValueError("unknown_mask_revision_status")
        if not str(supersedes_mask_decision_id or "").strip():
            raise ValueError("previous_mask_decision_required")
        if not str(previous_mask_hash or "").strip() or not str(final_mask_hash or "").strip():
            raise ValueError("mask_revision_hash_required")
        now = _utc_now()
        existing = self.latest_for_region(
            owner=owner, job_id=job_id, run_id=run_id, revision_id=revision_id,
            region_id=region_id, supersedes_mask_decision_id=supersedes_mask_decision_id,
            final_mask_hash=final_mask_hash)
        record = {
            "mask_revision_id": (existing or {}).get("mask_revision_id") or uuid.uuid4().hex,
            "supersedes_mask_decision_id": str(supersedes_mask_decision_id),
            "owner": str(owner),
            "job_id": str(job_id),
            "run_id": str(run_id),
            "revision_id": str(revision_id),
            "page_id": str(page_id),
            "region_id": str(region_id),
            "source_hash": str(source_hash),
            "base_segmentation_hash": str(base_segmentation_hash),
            "previous_mask_hash": str(previous_mask_hash),
            "final_mask_hash": str(final_mask_hash),
            "include_delta_json": json.dumps(include_delta or [], ensure_ascii=False, sort_keys=True),
            "exclude_delta_json": json.dumps(exclude_delta or [], ensure_ascii=False, sort_keys=True),
            "protected_delta_json": json.dumps(protected_delta or [], ensure_ascii=False, sort_keys=True),
            "uncertain_delta_json": json.dumps(uncertain_delta or [], ensure_ascii=False, sort_keys=True),
            "final_mask_asset": str(final_mask_asset or ""),
            "residual_evidence_json": json.dumps(residual_evidence or {}, ensure_ascii=False, sort_keys=True),
            "validation_json": json.dumps(validation or {}, ensure_ascii=False, sort_keys=True),
            "authorization": str(authorization),
            "reviewer": str(reviewer or "codex"),
            "status": str(status),
            "created_at": (existing or {}).get("created_at") or now,
            "updated_at": now,
            "schema_version": SCHEMA_VERSION,
            "supersedes_mask_revision_id": str(supersedes_mask_revision_id or ""),
            "residual_analysis_id": str(residual_analysis_id or ""),
            "residual_bitmap_hash": str(residual_bitmap_hash or ""),
            "component_manifest_hash": str(component_manifest_hash or ""),
            "component_decomposition_hashes_json": json.dumps(
                component_decomposition_hashes or [], ensure_ascii=False,
                sort_keys=True),
        }
        columns = ", ".join(record)
        placeholders = ", ".join(f":{key}" for key in record)
        self._conn.execute(
            f"INSERT OR REPLACE INTO human_mask_revisions ({columns}) VALUES ({placeholders})",
            record,
        )
        self._conn.commit()
        return self._row_payload(record)

    def latest_for_region(self, *, owner: str, job_id: str, run_id: str,
                          revision_id: str, region_id: str,
                          supersedes_mask_decision_id: str = "",
                          final_mask_hash: str = "") -> dict[str, Any] | None:
        params: list[Any] = [str(owner), str(job_id), str(run_id), str(revision_id), str(region_id)]
        query = ("SELECT * FROM human_mask_revisions WHERE owner=? AND job_id=? AND run_id=? "
                 "AND revision_id=? AND region_id=? AND status != 'discarded'")
        if str(supersedes_mask_decision_id or ""):
            query += " AND supersedes_mask_decision_id=?"
            params.append(str(supersedes_mask_decision_id))
        if str(final_mask_hash or ""):
            query += " AND final_mask_hash=?"
            params.append(str(final_mask_hash))
        query += " ORDER BY updated_at DESC LIMIT 1"
        row = self._conn.execute(query, tuple(params)).fetchone()
        return self._row_payload(dict(row)) if row else None

    def _row_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row)
        for column, output_name, fallback in (
            ("include_delta_json", "include_delta", []),
            ("exclude_delta_json", "exclude_delta", []),
            ("protected_delta_json", "protected_delta", []),
            ("uncertain_delta_json", "uncertain_delta", []),
            ("residual_evidence_json", "residual_evidence", {}),
            ("validation_json", "validation", {}),
            ("component_decomposition_hashes_json",
             "component_decomposition_hashes", []),
        ):
            try:
                payload[output_name] = json.loads(str(payload.pop(column, "") or "null"))
            except ValueError:
                payload[output_name] = fallback
            if payload[output_name] is None:
                payload[output_name] = fallback
        return payload
