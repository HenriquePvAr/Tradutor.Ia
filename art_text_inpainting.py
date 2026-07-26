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

INPAINTING_VERSION = "1.0"


def mask_hash(mask: np.ndarray) -> str:
    return hashlib.sha256(np.asarray(mask, dtype=np.uint8).tobytes()).hexdigest()


def build_text_masks(region_bgr: np.ndarray) -> dict[str, Any]:
    region = np.asarray(region_bgr)
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY) if region.ndim == 3 else region.copy()
    border = np.concatenate([
        gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1],
    ])
    background = int(np.median(border)) if border.size else int(np.median(gray))
    delta = np.abs(gray.astype(np.int16) - background)
    core = (delta >= 42).astype(np.uint8) * 255
    core = cv2.morphologyEx(core, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8), iterations=1)
    outline = cv2.dilate(core, np.ones((3, 3), np.uint8), iterations=1)
    antialias = (((delta >= 18) & (delta < 42)).astype(np.uint8) * 255)
    antialias = cv2.bitwise_and(antialias, cv2.dilate(outline, np.ones((3, 3), np.uint8), iterations=1))
    combined = cv2.bitwise_or(outline, antialias)
    combined = cv2.morphologyEx(combined, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)
    halo = cv2.dilate(combined, np.ones((5, 5), np.uint8), iterations=1)
    edges = cv2.Canny(gray, 70, 150)
    protected = cv2.bitwise_and(edges, cv2.bitwise_not(cv2.dilate(combined, np.ones((3, 3), np.uint8), iterations=1)))
    total = int(combined.size) or 1
    ratio = float((combined > 0).sum()) / total
    reason_codes: list[str] = []
    if ratio <= 0.002:
        reason_codes.append("text_mask_too_small")
    if ratio >= 0.38:
        reason_codes.append("text_mask_too_large")
    return {
        "inpainting_version": INPAINTING_VERSION,
        "background_level": background,
        "text_core_mask": core,
        "outline_mask": outline,
        "antialias_mask": antialias,
        "combined_inpainting_mask": combined,
        "validation_halo": halo,
        "protected_edge_mask": protected,
        "mask_ratio": round(ratio, 4),
        "mask_hash": mask_hash(combined),
        "protected_edges_before": int((protected > 0).sum()),
        "reason_codes": reason_codes,
        "valid": not reason_codes,
    }


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
    if best.get("changed_pixels_outside_change_mask"):
        reasons.append("changed_pixels_outside_change_mask")
    if float(best.get("edge_continuity_score") or 0.0) < 0.82:
        reasons.append("edge_continuity_low")
    if float(best.get("texture_consistency_score") or 0.0) < 0.55:
        reasons.append("texture_consistency_low")
    if float(best.get("seam_score") or 999.0) > 36.0:
        reasons.append("seam_score_high")
    if float(best.get("artifact_score") or 999.0) > 0.55:
        reasons.append("artifact_score_high")
    status = "passed" if not reasons else "needs_review"
    return {k: v for k, v in best.items() if k != "image"} | {
        "status": status,
        "reason_codes": reasons,
        "inpainting_version": INPAINTING_VERSION,
    }
