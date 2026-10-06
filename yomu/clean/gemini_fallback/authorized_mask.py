"""Conservative OCR-to-authorized-mask adapter for the Gemini fallback.

This adapter deliberately reuses Yomu's existing OCR grouping, classification,
and component segmentation. It does not treat an OCR rectangle as permission to
edit; the rectangle is only an ROI for the existing component mask routine.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from yomu.ocr import ocr_balloon as yomu_ocr
from yomu.ocr.ocr_engine import OCREngine

ELIGIBLE_CLASSES = {"dialogue", "speech", "caption", "narration", "ordinary_text", "system", "thought"}
SFX_CLASSES = {"sfx"}


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _component_count(mask: np.ndarray) -> tuple[int, list[int]]:
    count, _, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), 8)
    sizes = [int(stats[index, cv2.CC_STAT_AREA]) for index in range(1, count)]
    return len(sizes), sorted(sizes, reverse=True)


def generate_authorized_mask(image_path: str | Path, mask_path: str | Path, *, page_id: str = "1",
                             region_id: str | None = None, content_type: str | None = None,
                             version: str = "AUTHORIZED_MASK_GENERATOR_V1") -> dict[str, Any]:
    image_path = Path(image_path)
    image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("image_load_failed")
    height, width = image.shape[:2]
    lines = OCREngine("en").detect_lines(image)
    _, groups = yomu_ocr.analyze_image_array(image, lines)
    page_mask = np.zeros((height, width), dtype=np.uint8)
    sources: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for group in groups:
        classification = str(getattr(group, "classification", "unknown") or "unknown").lower()
        lines_for_group = list(getattr(group, "lines", []) or [])
        ocr_conf = min((float(getattr(line, "confidence", 0.0) or 0.0) for line in lines_for_group), default=0.0)
        record = {"text": str(getattr(group, "text", "") or ""), "classification": classification,
                  "ocr_confidence": round(ocr_conf, 6), "box": list(map(int, group.box))}
        if classification in SFX_CLASSES or classification not in ELIGIBLE_CLASSES:
            blocked.append({**record, "reason": "SFX_PROTECTED" if classification in SFX_CLASSES else "UNKNOWN_REVIEW"})
            continue
        # Existing Yomu component segmentation is constrained by this small
        # ROI. The OCR polygon/bbox is never used as the final authorization.
        roi = yomu_ocr._build_text_mask(image.shape, [group], padding=2)
        refined, refinement = yomu_ocr._component_text_mask(image, group, roi, strategy="conservative")
        if not np.any(refined):
            blocked.append({**record, "reason": "EMPTY_REFINED_MASK"})
            continue
        page_mask = cv2.bitwise_or(page_mask, refined)
        sources.append({**record, "refinement": refinement})

    # SFX wins over any overlapping eligible region. This is explicit even
    # though groups are normally disjoint after Yomu grouping.
    sfx_mask = np.zeros_like(page_mask)
    for group in groups:
        if str(getattr(group, "classification", "") or "").lower() in SFX_CLASSES:
            sfx_mask = cv2.bitwise_or(sfx_mask, yomu_ocr._build_text_mask(image.shape, [group], padding=2))
    page_mask[sfx_mask > 0] = 0
    pixels = int(np.count_nonzero(page_mask))
    coverage = pixels / float(width * height)
    component_count, component_sizes = _component_count(page_mask)
    eligible_confidence = min((item["ocr_confidence"] for item in sources), default=0.0)
    # Simple, auditable confidence: OCR confidence plus non-empty segmented
    # components, with a hard reject for no source evidence or excessive area.
    confidence = round(min(1.0, eligible_confidence * 0.85 + (0.15 if component_count else 0.0)), 6)
    status = "PASS" if sources and pixels and coverage <= 0.12 and confidence >= 0.65 and not any(
        item["reason"] == "UNKNOWN_REVIEW" for item in blocked
    ) else "REVIEW_REQUIRED_MASK_UNCERTAIN"
    mask_path = Path(mask_path)
    mask_path.parent.mkdir(parents=True, exist_ok=True)
    if status == "PASS":
        cv2.imwrite(str(mask_path), page_mask)
    else:
        mask_path = Path(mask_path)
    return {
        "status": status,
        "mask_generator_version": version,
        "mask_level": "B" if status == "PASS" else "C",
        "mask_source": "Yomu OCR geometry + existing _component_text_mask",
        "image_sha256": _sha256(image_path),
        "mask_sha256": _sha256(mask_path) if status == "PASS" else None,
        "mask_path": str(mask_path) if status == "PASS" else None,
        "mask_coordinate_space": "PAGE_SPACE",
        "width": width, "height": height, "offset_x": 0, "offset_y": 0,
        "raw_mask_pixels": pixels, "final_mask_pixels": pixels,
        "coverage_ratio": round(coverage, 8), "component_count": component_count,
        "component_sizes": component_sizes, "dilation_px": 1,
        "mask_confidence": confidence, "sfx_excluded": True,
        "content_type": content_type or "UNKNOWN", "page_id": page_id,
        "region_id": region_id or (sources[0]["classification"] if len(sources) == 1 else "multi-region"),
        "regions": sources, "blocked_regions": blocked,
    }


def generate_authorized_mask_v2(image_path: str | Path, mask_path: str | Path, *, page_id: str = "1",
                                region_id: str | None = None, content_type: str | None = None,
                                target_group_id: str | None = None) -> dict[str, Any]:
    """V2 conservative promotion for a narrow class of UNKNOWN regions.

    V1 remains unchanged. V2 only promotes an UNKNOWN group when OCR support,
    outlined-glyph evidence, gradient-like local background, low art-contact,
    and plausible coverage agree. Otherwise it returns a diagnostic review.
    """
    image_path = Path(image_path)
    image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("image_load_failed")
    height, width = image.shape[:2]
    lines = OCREngine("en").detect_lines(image)
    _, groups = yomu_ocr.analyze_image_array(image, lines)
    page_mask = np.zeros((height, width), dtype=np.uint8)
    core_mask = np.zeros_like(page_mask)
    sfx_mask = np.zeros_like(page_mask)
    sources: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    target_seen = target_group_id is None

    for group in groups:
        if target_group_id is not None and str(getattr(group, "group_id", "")) != str(target_group_id):
            blocked.append({
                "group_id": str(getattr(group, "group_id", "") or ""),
                "region_id": str(getattr(group, "region_id", "") or ""),
                "reason_codes": ["NON_TARGET_REGION_EXCLUDED"],
            })
            continue
        if target_group_id is not None:
            target_seen = True
        classification = str(getattr(group, "classification", "unknown") or "unknown").lower()
        lines_for_group = list(getattr(group, "lines", []) or [])
        ocr_support = min((float(getattr(line, "confidence", 0.0) or 0.0) for line in lines_for_group), default=0.0)
        roi = yomu_ocr._build_text_mask(image.shape, [group], padding=2)
        roi_bool = roi > 0
        roi_pixels = max(1, int(roi_bool.sum()))
        reason_codes: list[str] = []
        selected = None
        source_kind = ""
        if classification in SFX_CLASSES:
            sfx_mask = cv2.bitwise_or(sfx_mask, roi)
            blocked.append({"text": group.text, "classification": classification, "reason_codes": ["SFX_PROTECTED"]})
            continue
        if classification in ELIGIBLE_CLASSES:
            selected, refinement = yomu_ocr._component_text_mask(image, group, roi, strategy="conservative")
            source_kind = "V1_COMPONENT_MASK"
        elif classification == "unknown":
            background = getattr(group, "background_metrics", {}) or {}
            local_class = str(background.get("local_background_class") or "")
            background_type = str(getattr(group, "background_type", "") or "")
            long_line_count = int(background.get("long_line_count") or 0)
            background_edge_density = float(background.get("background_edge_density") or 0.0)
            token_count = len(str(getattr(group, "text", "") or "").split())
            outlined, outlined_metrics = yomu_ocr._outlined_light_text_mask(image, group, roi)
            # Recover saturated fill inside an outlined glyph only when it is
            # spatially supported by the existing outlined evidence. This is
            # local color support, not a global cyan/white threshold.
            hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
            color_candidate = (hsv[..., 1] >= 90) & (hsv[..., 2] >= 120) & roi_bool
            line_height = max(1, int(getattr(group, "box", (0, 0, 0, 1))[3] / max(1, len(lines_for_group))))
            support_radius = max(3, min(16, int(round(line_height * 0.08))))
            support_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (support_radius * 2 + 1, support_radius * 2 + 1))
            outlined_support = cv2.dilate((outlined > 0).astype(np.uint8), support_kernel, iterations=1) > 0
            selected = ((outlined > 0) | (color_candidate & outlined_support)).astype(np.uint8) * 255
            candidate_ratio = float(np.count_nonzero(selected)) / roi_pixels
            edges = cv2.Canny(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), 80, 160) > 0
            edge_ring = (cv2.dilate((outlined > 0).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0) & ~(outlined > 0)
            art_contact = float(edges[edge_ring].mean()) if np.any(edge_ring) else 1.0
            if ocr_support < 0.85:
                reason_codes.append("LOW_OCR_SUPPORT")
            if token_count < 3:
                reason_codes.append("SHORT_UNKNOWN_TEXT")
            if local_class not in {"gradient", "plain", "uniform"}:
                reason_codes.append("BACKGROUND_TEXTURE_COMPLEX")
            if background_type in {"speed_lines", "textured_art", "art"} or long_line_count > 2 or background_edge_density > 0.08:
                reason_codes.append("TEXT_TOUCHING_LINE_ART")
            if candidate_ratio < 0.04:
                reason_codes.append("GLYPH_MASK_LOW_SUPPORT")
            if candidate_ratio > 0.35:
                reason_codes.append("GLYPH_MASK_OVEREXPANSION")
            if art_contact > 0.12:
                reason_codes.append("TEXT_TOUCHING_LINE_ART")
            if not np.any(outlined):
                reason_codes.append("EMPTY_OUTLINED_MASK")
            if reason_codes:
                blocked.append({"text": group.text, "classification": classification,
                                "reason_codes": reason_codes, "ocr_support": round(ocr_support, 6),
                                "candidate_ratio": round(candidate_ratio, 6), "art_contact_ratio": round(art_contact, 6),
                                "background_class": local_class})
                continue
            refinement = {"outlined_glyph_mask": True, "accepted_text_components": outlined_metrics.get("accepted_text_components", 0)}
            source_kind = "V2_OUTLINED_GLYPH_MASK"
        else:
            blocked.append({"text": group.text, "classification": classification, "reason_codes": ["CLASSIFICATION_UNKNOWN"]})
            continue
        if selected is None or not np.any(selected):
            blocked.append({"text": group.text, "classification": classification, "reason_codes": ["EMPTY_REFINED_MASK"]})
            continue
        selected = (selected > 0).astype(np.uint8) * 255
        core = cv2.erode(selected, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
        if not np.any(core):
            blocked.append({"text": group.text, "classification": classification, "reason_codes": ["CORE_MASK_EMPTY"]})
            continue
        page_mask = cv2.bitwise_or(page_mask, selected)
        core_mask = cv2.bitwise_or(core_mask, core)
        sources.append({"text": group.text, "classification": classification, "ocr_support": round(ocr_support, 6),
                        "box": list(map(int, group.box)), "source_kind": source_kind,
                        "core_pixels": int(np.count_nonzero(core)), "effective_pixels": int(np.count_nonzero(selected)),
                        "fringe_pixels": int(np.count_nonzero(selected) - np.count_nonzero(core)),
                        "refinement": refinement})

    page_mask[sfx_mask > 0] = 0
    core_mask[sfx_mask > 0] = 0
    pixels = int(np.count_nonzero(page_mask)); core_pixels = int(np.count_nonzero(core_mask))
    coverage = pixels / float(width * height)
    component_count, component_sizes = _component_count(page_mask)
    if sources:
        confidence = round(min(item["ocr_support"] for item in sources) * 0.55 +
                           min(1.0, core_pixels / max(1, pixels)) * 0.2 +
                           min(1.0, component_count / 20.0) * 0.1 + 0.15, 6)
    else:
        confidence = 0.0
    status = "PASS" if target_seen and sources and pixels and coverage <= 0.12 and confidence >= 0.75 else "REVIEW_REQUIRED_MASK_UNCERTAIN"
    mask_path = Path(mask_path)
    mask_path.parent.mkdir(parents=True, exist_ok=True)
    if status == "PASS":
        cv2.imwrite(str(mask_path), page_mask)
    return {"status": status, "mask_generator_version": "AUTHORIZED_MASK_GENERATOR_V2",
            "mask_level": "B" if status == "PASS" else "C", "mask_source": "OCR geometry + outlined/component refinement",
            "image_sha256": _sha256(image_path), "mask_sha256": _sha256(mask_path) if status == "PASS" else None,
            "mask_path": str(mask_path) if status == "PASS" else None, "mask_coordinate_space": "PAGE_SPACE",
            "width": width, "height": height, "offset_x": 0, "offset_y": 0,
            "raw_mask_pixels": pixels, "core_pixels": core_pixels, "final_mask_pixels": pixels,
            "fringe_pixels": max(0, pixels - core_pixels), "expansion_ratio": round(pixels / max(1, core_pixels), 6),
            "coverage_ratio": round(coverage, 8), "component_count": component_count,
            "component_sizes": component_sizes, "mask_confidence": confidence, "sfx_excluded": True,
            "content_type": content_type or "UNKNOWN", "page_id": page_id,
            "region_id": region_id or "multi-region", "regions": sources, "blocked_regions": blocked}
