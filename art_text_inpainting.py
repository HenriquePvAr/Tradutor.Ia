"""Safe local inpainting for text drawn directly on artwork.

The functions here are deliberately local and fail-closed: they operate on
provided pixels with OpenCV only, produce explicit masks, and refuse candidates
that damage protected line art.
"""
from __future__ import annotations

import hashlib
from typing import Any

import cv2
import numpy as np

INPAINTING_VERSION = "1.1"


def mask_hash(mask: np.ndarray) -> str:
    return hashlib.sha256(np.asarray(mask, dtype=np.uint8).tobytes()).hexdigest()


def _component_stats(mask: np.ndarray) -> list[dict[str, int]]:
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        np.asarray(mask, dtype=np.uint8), 8)
    records: list[dict[str, int]] = []
    for label in range(1, count):
        records.append({
            "x": int(stats[label, cv2.CC_STAT_LEFT]),
            "y": int(stats[label, cv2.CC_STAT_TOP]),
            "w": int(stats[label, cv2.CC_STAT_WIDTH]),
            "h": int(stats[label, cv2.CC_STAT_HEIGHT]),
            "area": int(stats[label, cv2.CC_STAT_AREA]),
        })
    return records


def _glyph_like_components(mask: np.ndarray, *, region_area: int) -> np.ndarray:
    """Keep compact connected components that look like glyph pieces.

    This is generic geometry filtering, not a crop/page exception.  It prevents
    background line art from turning an entire bounding box into an erase mask.
    """
    source = (np.asarray(mask) > 0).astype(np.uint8)
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(source, 8)
    kept = np.zeros(source.shape, dtype=np.uint8)
    max_component_area = max(12, int(region_area * 0.18))
    min_component_area = max(2, int(region_area * 0.00012))
    for label in range(1, count):
        area = int(stats[label, cv2.CC_STAT_AREA])
        w = int(stats[label, cv2.CC_STAT_WIDTH])
        h = int(stats[label, cv2.CC_STAT_HEIGHT])
        if area < min_component_area or area > max_component_area:
            continue
        aspect = w / max(1, h)
        fill = area / max(1, w * h)
        elongated_line = (aspect > 10.0 or aspect < 0.08) and fill < 0.38
        too_sparse = fill < 0.06
        if elongated_line or too_sparse:
            continue
        kept[labels == label] = 255
    return kept


def segment_text_layers(region_bgr: np.ndarray) -> dict[str, Any]:
    region = np.asarray(region_bgr)
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY) if region.ndim == 3 else region.copy()
    hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV) if region.ndim == 3 else None
    border = np.concatenate([
        gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1],
    ])
    background = int(np.median(border)) if border.size else int(np.median(gray))
    region_area = int(gray.size) or 1
    delta = np.abs(gray.astype(np.int16) - background).astype(np.uint8)
    dark_candidate = (gray <= min(background - 32, 115)).astype(np.uint8) * 255
    light_candidate = (gray >= max(background + 28, 205)).astype(np.uint8) * 255
    if background >= 170:
        core_raw = dark_candidate
        outline_raw = light_candidate
    else:
        core_raw = (delta >= 50).astype(np.uint8) * 255
        outline_raw = (delta >= 24).astype(np.uint8) * 255
    if hsv is not None:
        saturation = hsv[:, :, 1]
        # Very colorful pixels are more likely to be art than monochrome text
        # fill/outline.  Keep them only when connected to a glyph core later.
        low_chroma = (saturation <= 72).astype(np.uint8) * 255
        core_raw = cv2.bitwise_and(core_raw, low_chroma)
    raw_candidate = cv2.bitwise_or(core_raw, outline_raw)
    raw_ratio = float((raw_candidate > 0).sum()) / region_area
    core = _glyph_like_components(core_raw, region_area=region_area)
    near_core = cv2.dilate(core, np.ones((5, 5), np.uint8), iterations=1)
    outline = cv2.bitwise_and(outline_raw, near_core)
    outline = cv2.bitwise_and(outline, cv2.bitwise_not(core))
    outline = _glyph_like_components(cv2.bitwise_or(outline, core), region_area=region_area)
    outline = cv2.bitwise_and(outline, cv2.bitwise_not(core))
    antialias = (((delta >= 12) & (delta < 48)).astype(np.uint8) * 255)
    antialias = cv2.bitwise_and(antialias, cv2.dilate(cv2.bitwise_or(core, outline), np.ones((3, 3), np.uint8), iterations=1))
    combined = cv2.bitwise_or(cv2.bitwise_or(core, outline), antialias)
    combined = cv2.morphologyEx(combined, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8), iterations=1)
    combined = _glyph_like_components(combined, region_area=region_area)
    validation_halo = cv2.dilate(combined, np.ones((5, 5), np.uint8), iterations=1)
    edges = cv2.Canny(gray, 70, 150)
    protected = cv2.bitwise_and(edges, cv2.bitwise_not(cv2.dilate(combined, np.ones((3, 3), np.uint8), iterations=1)))
    background_art = cv2.bitwise_or(
        cv2.bitwise_and(edges, cv2.bitwise_not(validation_halo)),
        cv2.bitwise_and((delta > 20).astype(np.uint8) * 255, cv2.bitwise_not(validation_halo)),
    )
    total = int(combined.size) or 1
    ratio = float((combined > 0).sum()) / total
    protected_overlap = int(((protected > 0) & (combined > 0)).sum())
    protected_count = int((protected > 0).sum())
    ambiguity = protected_overlap / max(1, protected_count)
    core_pixels = int((core > 0).sum())
    combined_pixels = int((combined > 0).sum())
    precision = 0.0 if combined_pixels == 0 else core_pixels / max(1, combined_pixels)
    reason_codes: list[str] = []
    if raw_ratio >= 0.32:
        reason_codes.append("text_mask_too_large")
    if ratio <= 0.002 and raw_ratio < 0.32:
        reason_codes.append("text_mask_too_small")
    if ratio >= 0.32 and "text_mask_too_large" not in reason_codes:
        reason_codes.append("text_mask_too_large")
    if ambiguity > 0.08:
        reason_codes.append("segmentation_unsafe")
    if precision < 0.18 and combined_pixels:
        reason_codes.append("mask_precision_low")
    components = _component_stats(combined)
    return {
        "inpainting_version": INPAINTING_VERSION,
        "background_level": background,
        "text_core_mask": core,
        "outline_mask": outline,
        "antialias_mask": antialias,
        "shadow_mask": np.zeros_like(core),
        "combined_inpainting_mask": combined,
        "validation_halo": validation_halo,
        "protected_edge_mask": protected,
        "background_art_mask": background_art,
        "mask_ratio": round(ratio, 4),
        "raw_mask_ratio": round(raw_ratio, 4),
        "mask_hash": mask_hash(combined),
        "protected_edges_before": int((protected > 0).sum()),
        "protected_edge_pixels": protected_count,
        "protected_edge_overlap_with_text_mask": protected_overlap,
        "mask_precision": round(float(precision), 4),
        "mask_recall_estimate": round(min(1.0, ratio / 0.08), 4),
        "mask_ambiguity": round(float(ambiguity), 4),
        "false_positive_risk": round(max(float(ambiguity), 1.0 - min(1.0, precision / 0.45)), 4),
        "connected_components": components,
        "component_count": len(components),
        "text_mask_too_large": ratio >= 0.32,
        "segmentation_unsafe": ambiguity > 0.08,
        "line_art_risk": "high" if ambiguity > 0.08 else "acceptable",
        "reason_codes": reason_codes,
        "valid": not reason_codes,
    }


def build_text_masks(region_bgr: np.ndarray) -> dict[str, Any]:
    return segment_text_layers(region_bgr)


def _edge_continuity(before_gray: np.ndarray, after_gray: np.ndarray,
                     protected_edges: np.ndarray) -> tuple[float, int]:
    before = cv2.Canny(before_gray, 70, 150)
    after = cv2.Canny(after_gray, 70, 150)
    protected = protected_edges > 0
    before_count = int((before[protected] > 0).sum())
    if before_count == 0:
        return 1.0, 0
    after_count = int((after[protected] > 0).sum())
    missing = max(0, before_count - after_count)
    return round(after_count / max(1, before_count), 3), missing


def score_inpaint_candidate(original_bgr: np.ndarray, candidate_bgr: np.ndarray,
                            masks: dict[str, Any]) -> dict[str, Any]:
    gray_before = cv2.cvtColor(original_bgr, cv2.COLOR_BGR2GRAY) if original_bgr.ndim == 3 else original_bgr
    gray_after = cv2.cvtColor(candidate_bgr, cv2.COLOR_BGR2GRAY) if candidate_bgr.ndim == 3 else candidate_bgr
    combined = masks["combined_inpainting_mask"] > 0
    halo = (masks["validation_halo"] > 0) & ~combined
    protected = masks["protected_edge_mask"]
    continuity, line_breaks = _edge_continuity(gray_before, gray_after, protected)
    seam = float(np.mean(np.abs(gray_before[halo].astype(np.int16) - gray_after[halo].astype(np.int16)))) if np.any(halo) else 0.0
    color = float(np.mean(np.abs(original_bgr[halo].astype(np.int16) - candidate_bgr[halo].astype(np.int16)))) if original_bgr.ndim == 3 and np.any(halo) else seam
    lap_before = cv2.Laplacian(gray_before, cv2.CV_32F)
    lap_after = cv2.Laplacian(gray_after, cv2.CV_32F)
    texture = 1.0 - min(1.0, abs(float(lap_before[halo].std() if np.any(halo) else 0.0)
                                  - float(lap_after[halo].std() if np.any(halo) else 0.0)) / 80.0)
    changed_outside = int((np.any(original_bgr != candidate_bgr, axis=2) if original_bgr.ndim == 3
                           else original_bgr != candidate_bgr)[~combined].sum())
    artifact = min(1.0, (seam / 80.0) + (line_breaks / max(1, int((protected > 0).sum()))))
    overall = continuity * 0.35 + texture * 0.25 + max(0.0, 1.0 - seam / 45.0) * 0.25 + max(0.0, 1.0 - artifact) * 0.15
    return {
        "edge_continuity_score": round(continuity, 3),
        "line_break_count": int(line_breaks),
        "texture_consistency_score": round(texture, 3),
        "seam_score": round(seam, 3),
        "color_discontinuity_score": round(color, 3),
        "artifact_score": round(artifact, 3),
        "protected_edges_preserved": continuity >= 0.82,
        "changed_pixels_outside_change_mask": changed_outside,
        "overall_score": round(overall, 3),
    }


def generate_inpainting_candidates(region_bgr: np.ndarray, masks: dict[str, Any]) -> list[dict[str, Any]]:
    if not masks.get("valid"):
        return []
    candidates: list[dict[str, Any]] = []
    mask = masks["combined_inpainting_mask"]
    for method_name, method in (("telea", cv2.INPAINT_TELEA), ("navier_stokes", cv2.INPAINT_NS)):
        for radius in (2, 3, 5):
            repaired = cv2.inpaint(region_bgr, mask, radius, method)
            metrics = score_inpaint_candidate(region_bgr, repaired, masks)
            candidates.append({
                "method": method_name,
                "radius": radius,
                "mask_hash": masks["mask_hash"],
                "image": repaired,
                **metrics,
            })
    candidates.sort(key=lambda item: float(item.get("overall_score") or 0.0), reverse=True)
    return candidates


def select_best_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    if not candidates:
        return {"status": "blocked", "reason_codes": ["inpainting_candidates_unavailable"]}
    best = candidates[0]
    reasons: list[str] = []
    def metric(name: str, default: float) -> float:
        value = best.get(name)
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if best.get("changed_pixels_outside_change_mask"):
        reasons.append("changed_pixels_outside_change_mask")
    if metric("edge_continuity_score", 0.0) < 0.82:
        reasons.append("edge_continuity_low")
    if metric("texture_consistency_score", 0.0) < 0.55:
        reasons.append("texture_consistency_low")
    if metric("seam_score", 999.0) > 36.0:
        reasons.append("seam_score_high")
    if metric("artifact_score", 999.0) > 0.55:
        reasons.append("artifact_score_high")
    status = "passed" if not reasons else "needs_review"
    return {k: v for k, v in best.items() if k != "image"} | {
        "status": status,
        "reason_codes": reasons,
        "inpainting_version": INPAINTING_VERSION,
    }
