"""OCR-owned, bounded authorized masks for the Gemini fallback."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import cv2
import numpy as np

try:
    from yomu.ocr import ocr_balloon as yomu_ocr
except ImportError:  # compatibility with the pre-package research checkout
    import ocr_balloon as yomu_ocr
try:
    from yomu.ocr.ocr_engine import OCREngine
except ImportError:  # compatibility with the pre-package research checkout
    from ocr_engine import OCREngine

ELIGIBLE_CLASSES = {"dialogue", "speech", "caption", "narration", "ordinary_text", "system", "thought"}
SFX_CLASSES = {"sfx"}
MIN_TEXT_GEOMETRY_COVERAGE = 0.98


def _line_geometry(group, shape, padding: int = 0) -> np.ndarray:
    """Rasterize only this group's OCR line polygons, with bounded padding."""
    geometry = np.zeros(shape[:2], dtype=np.uint8)
    lines = list(getattr(group, "lines", []) or [])
    for line in lines:
        polygon = np.asarray(line.polygon, dtype=np.int32).reshape(-1, 2)
        line_mask = np.zeros_like(geometry)
        cv2.fillPoly(line_mask, [polygon], 255)
        if padding > 0:
            line_mask = cv2.dilate(
                line_mask, np.ones((padding * 2 + 1, padding * 2 + 1), np.uint8), iterations=1
            )
        geometry = cv2.bitwise_or(geometry, line_mask)
    return geometry


def _complete_owned_glyph_mask(image, group, component_mask, *, protected_mask=None):
    """Add only bright outline pixels supported by owned dark glyph pixels.

    Unlike rectangular line-box filling or dilation of the whole glyph mask,
    this preserves nearby artwork: extra pixels must be bright, low-saturation,
    within a bounded, line-height-scaled fringe of an accepted component, and inside the
    OCR-owned line envelope. Protected OCR groups are hard exclusions.
    """
    image = np.asarray(image)
    image_shape = image.shape
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image.astype(np.uint8)
    saturation = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)[..., 1] if image.ndim == 3 else np.zeros_like(gray)
    line_heights = [int(line.box[3]) for line in list(getattr(group, "lines", []) or []) if int(line.box[3]) > 0]
    median_height = float(np.median(line_heights)) if line_heights else 1.0
    padding = max(1, min(4, int(round(median_height * 0.04))))
    fringe_radius = max(1, min(3, int(round(median_height * 0.05))))
    seed = (np.asarray(component_mask) > 0).astype(np.uint8) * 255
    extra = np.zeros(image_shape[:2], dtype=np.uint8)
    owned_envelope = np.zeros(image_shape[:2], dtype=np.uint8)
    line_evidence = []
    for index, line in enumerate(list(getattr(group, "lines", []) or [])):
        polygon = np.zeros(image_shape[:2], dtype=np.uint8)
        cv2.fillPoly(polygon, [np.asarray(line.polygon, dtype=np.int32).reshape(-1, 2)], 255)
        owner_radius = padding + fringe_radius
        owner_limit = cv2.dilate(polygon, np.ones((owner_radius * 2 + 1, owner_radius * 2 + 1), np.uint8), iterations=1) > 0
        owned_envelope[owner_limit] = 255
        dark_owned = (seed > 0) & owner_limit
        outline_radius = max(1, fringe_radius - 1)
        support = cv2.dilate(dark_owned.astype(np.uint8),
                             cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                                       (outline_radius * 2 + 1, outline_radius * 2 + 1)),
                             iterations=1) > 0
        bright_outline = (gray >= 238) & (saturation <= 55) & support & owner_limit
        extra[bright_outline] = 255
        glyph_evidence = ((seed > 0) & owner_limit) | bright_outline
        # Include the one-pixel antialias/shadow fringe around identified
        # glyph evidence, but never extend beyond this group's OCR envelope.
        fringe_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                                   (fringe_radius * 2 + 1, fringe_radius * 2 + 1))
        glyph_evidence = cv2.dilate(glyph_evidence.astype(np.uint8), fringe_kernel) > 0
        glyph_evidence &= owner_limit
        line_evidence.append((index, line, glyph_evidence))
    expected = seed.copy()
    expected[extra > 0] = 255
    seed_pixels = int(np.count_nonzero(seed))
    owned_seed_pixels = int(np.count_nonzero((seed > 0) & (owned_envelope > 0)))
    expected[owned_envelope == 0] = 0
    expanded = cv2.dilate(expected, cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                                              (fringe_radius * 2 + 1, fringe_radius * 2 + 1)))
    expanded[owned_envelope == 0] = 0
    clipped = False
    if protected_mask is not None:
        protected = np.asarray(protected_mask) > 0
        clipped = bool(np.any((expanded > 0) & protected))
        expanded[protected] = 0
    line_coverage = []
    for index, line, evidence in line_evidence:
        evidence = evidence > 0
        expected_count = int(np.count_nonzero(evidence))
        covered_count = int(np.count_nonzero((expanded > 0) & evidence))
        ratio = covered_count / float(max(1, expected_count))
        line_coverage.append({
            "line_index": index,
            "text": str(getattr(line, "text", "") or ""),
            "box": list(map(int, line.box)),
            "glyph_evidence_pixels": expected_count,
            "covered_glyph_evidence_pixels": covered_count,
            "coverage_ratio": round(ratio, 6),
            "status": "PASS" if ratio >= MIN_TEXT_GEOMETRY_COVERAGE else "FAIL",
        })
    seed_coverage = owned_seed_pixels / float(max(1, seed_pixels))
    ratio = min([seed_coverage, *(line["coverage_ratio"] for line in line_coverage)]) if line_coverage else 0.0
    return expanded, {
        "padding_px": padding,
        "stroke_outline_fringe_px": fringe_radius,
        "glyph_evidence_pixels": int(np.count_nonzero(expected)),
        "covered_glyph_evidence_pixels": int(np.count_nonzero(expanded)),
        "component_seed_pixels": seed_pixels,
        "owned_component_seed_pixels": owned_seed_pixels,
        "component_seed_coverage_ratio": round(seed_coverage, 6),
        "coverage_ratio": round(ratio, 6),
        "minimum_coverage_ratio": MIN_TEXT_GEOMETRY_COVERAGE,
        "status": "PASS" if (line_coverage and seed_coverage >= MIN_TEXT_GEOMETRY_COVERAGE
                               and all(line["status"] == "PASS" for line in line_coverage)
                               and not clipped) else "FAIL",
        "geometry_kind": "owned_components_plus_supported_light_outline",
        "lines": line_coverage,
        "clipped_by_safety_boundary": clipped,
    }


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


def _resolve_target_group(groups, target_group_index: int | None):
    candidates = [(index, group) for index, group in enumerate(groups)
                  if str(getattr(group, "classification", "unknown") or "unknown").lower() != "sfx"]
    if target_group_index is not None:
        if isinstance(target_group_index, bool) or not isinstance(target_group_index, int):
            return None, "INVALID_TARGET_GROUP_INDEX"
        if target_group_index < 0 or target_group_index >= len(groups):
            return None, "TARGET_GROUP_INDEX_OUT_OF_RANGE"
        group = groups[target_group_index]
        if str(getattr(group, "classification", "unknown") or "unknown").lower() == "sfx":
            return None, "SFX_TARGET_FORBIDDEN"
        return (target_group_index, group), None
    if len(candidates) == 1:
        return candidates[0], None
    if len(candidates) > 1:
        return None, "TARGET_GROUP_OWNERSHIP_REQUIRED"
    return None, "NO_OWNED_TEXT_GROUP"


def geometry_padding(group) -> int:
    """Small line-height-scaled halo for outlined story glyphs and neighbor guards."""
    heights = [int(line.box[3]) for line in list(getattr(group, "lines", []) or []) if int(line.box[3]) > 0]
    median_height = float(np.median(heights)) if heights else 1.0
    return max(1, min(3, int(round(median_height * 0.05))))


def _owned_group_id(image_sha256: str, index: int, group) -> str:
    text = " ".join(str(getattr(group, "text", "") or "").casefold().split())
    payload = f"{image_sha256}\0{index}\0{getattr(group, 'classification', '')}\0{text}\0{tuple(group.box)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _other_group_guard(groups, target_index: int, image_shape, padding: int = 0) -> np.ndarray:
    guard = np.zeros(image_shape[:2], dtype=np.uint8)
    for index, group in enumerate(groups):
        if index != target_index:
            guard = cv2.bitwise_or(guard, _line_geometry(group, image_shape, padding=padding))
    return guard


def generate_authorized_mask(image_path: str | Path, mask_path: str | Path, *, page_id: str = "1",
                             region_id: str | None = None, content_type: str | None = None,
                             target_group_index: int | None = None,
                             version: str = "AUTHORIZED_MASK_GENERATOR_V1_FULL_TEXT_COVERAGE") -> dict[str, Any]:
    image_path = Path(image_path)
    image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("image_load_failed")
    height, width = image.shape[:2]
    lines = OCREngine("en").detect_lines(image)
    _, groups = yomu_ocr.analyze_image_array(image, lines)
    image_sha = _sha256(image_path)
    selected_group, selection_error = _resolve_target_group(groups, target_group_index)
    page_mask = np.zeros((height, width), dtype=np.uint8)
    sfx_mask = np.zeros_like(page_mask)
    for group in groups:
        if str(getattr(group, "classification", "") or "").lower() in SFX_CLASSES:
            sfx_mask = cv2.bitwise_or(sfx_mask, yomu_ocr._build_text_mask(image.shape, [group], padding=2))
    sources: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    coverage_records: list[dict[str, Any]] = []
    if selection_error:
        blocked.append({"reason": selection_error, "candidate_group_count": sum(
            str(getattr(group, "classification", "unknown") or "unknown").lower() != "sfx" for group in groups
        )})
    for group_index, group in ([selected_group] if selected_group else []):
        classification = str(getattr(group, "classification", "unknown") or "unknown").lower()
        lines_for_group = list(getattr(group, "lines", []) or [])
        ocr_conf = min((float(getattr(line, "confidence", 0.0) or 0.0) for line in lines_for_group), default=0.0)
        record = {"text": str(getattr(group, "text", "") or ""), "classification": classification,
                  "ocr_confidence": round(ocr_conf, 6), "box": list(map(int, group.box)),
                  "group_index": group_index, "owned_group_id": _owned_group_id(image_sha, group_index, group)}
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
        protected = cv2.bitwise_or(sfx_mask, _other_group_guard(groups, group_index, image.shape,
                                                                 padding=geometry_padding(group)))
        completed, geometry = _complete_owned_glyph_mask(image, group, refined, protected_mask=protected)
        coverage_records.append({"text": record["text"], "classification": classification, **geometry})
        if geometry["status"] != "PASS":
            blocked.append({**record, "reason": "INCOMPLETE_TEXT_GEOMETRY_COVERAGE", "mask_coverage": geometry})
            continue
        page_mask = cv2.bitwise_or(page_mask, completed)
        sources.append({**record, "refinement": refinement, "mask_coverage": geometry})

    # SFX wins over any overlapping eligible region. This is explicit even
    # though groups are normally disjoint after Yomu grouping.
    page_mask[sfx_mask > 0] = 0
    pixels = int(np.count_nonzero(page_mask))
    coverage = pixels / float(width * height)
    component_count, component_sizes = _component_count(page_mask)
    eligible_confidence = min((item["ocr_confidence"] for item in sources), default=0.0)
    # Simple, auditable confidence: OCR confidence plus non-empty segmented
    # components, with a hard reject for no source evidence or excessive area.
    confidence = round(min(1.0, eligible_confidence * 0.85 + (0.15 if component_count else 0.0)), 6)
    coverage_pass = bool(coverage_records) and all(item["status"] == "PASS" for item in coverage_records)
    status = "PASS" if sources and coverage_pass and pixels and coverage <= 0.12 and confidence >= 0.65 and not any(
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
        "image_sha256": image_sha,
        "mask_sha256": _sha256(mask_path) if status == "PASS" else None,
        "mask_path": str(mask_path) if status == "PASS" else None,
        "mask_coordinate_space": "PAGE_SPACE",
        "width": width, "height": height, "offset_x": 0, "offset_y": 0,
        "raw_mask_pixels": pixels, "final_mask_pixels": pixels,
        "coverage_ratio": round(coverage, 8), "component_count": component_count,
        "component_sizes": component_sizes, "dilation_px": 1,
        "mask_confidence": confidence, "sfx_excluded": True,
        "mask_coverage": {"status": "PASS" if coverage_pass else "FAIL",
                          "geometry_kind": "owned_components_plus_supported_light_outline",
                          "coverage_ratio": round(min((item["coverage_ratio"] for item in coverage_records), default=0.0), 6),
                          "minimum_coverage_ratio": MIN_TEXT_GEOMETRY_COVERAGE,
                          "region_id": region_id or "unassigned",
                          "target_group_index": selected_group[0] if selected_group else None,
                          "owned_group_id": sources[0]["owned_group_id"] if sources else None,
                          "groups": coverage_records},
        "content_type": content_type or "UNKNOWN", "page_id": page_id,
        "region_id": region_id or (sources[0]["classification"] if len(sources) == 1 else "multi-region"),
        "regions": sources, "blocked_regions": blocked,
    }


def generate_authorized_mask_v2(image_path: str | Path, mask_path: str | Path, *, page_id: str = "1",
                                region_id: str | None = None, content_type: str | None = None,
                                target_group_index: int | None = None) -> dict[str, Any]:
    """V2 conservative promotion for a narrow class of UNKNOWN regions.

    V2 only promotes an UNKNOWN group when OCR support,
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
    image_sha = _sha256(image_path)
    selected_group, selection_error = _resolve_target_group(groups, target_group_index)
    page_mask = np.zeros((height, width), dtype=np.uint8)
    core_mask = np.zeros_like(page_mask)
    sfx_mask = np.zeros_like(page_mask)
    for group in groups:
        if str(getattr(group, "classification", "") or "").lower() in SFX_CLASSES:
            sfx_mask = cv2.bitwise_or(sfx_mask, yomu_ocr._build_text_mask(image.shape, [group], padding=2))
    sources: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    coverage_records: list[dict[str, Any]] = []
    if selection_error:
        blocked.append({"reason_codes": [selection_error], "candidate_group_count": sum(
            str(getattr(group, "classification", "unknown") or "unknown").lower() != "sfx" for group in groups
        )})

    for group_index, group in ([selected_group] if selected_group else []):
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
        protected = cv2.bitwise_or(sfx_mask, _other_group_guard(groups, group_index, image.shape,
                                                                 padding=geometry_padding(group)))
        selected, geometry = _complete_owned_glyph_mask(image, group, selected, protected_mask=protected)
        coverage_records.append({"text": str(getattr(group, "text", "") or ""),
                                 "classification": classification, **geometry})
        if geometry["status"] != "PASS":
            blocked.append({"text": group.text, "classification": classification,
                            "reason_codes": ["INCOMPLETE_TEXT_GEOMETRY_COVERAGE"], "mask_coverage": geometry})
            continue
        core = cv2.erode(selected, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
        if not np.any(core):
            blocked.append({"text": group.text, "classification": classification, "reason_codes": ["CORE_MASK_EMPTY"]})
            continue
        page_mask = cv2.bitwise_or(page_mask, selected)
        core_mask = cv2.bitwise_or(core_mask, core)
        sources.append({"text": group.text, "classification": classification, "ocr_support": round(ocr_support, 6),
                        "box": list(map(int, group.box)), "source_kind": source_kind,
                        "group_index": group_index, "owned_group_id": _owned_group_id(image_sha, group_index, group),
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
    coverage_pass = bool(coverage_records) and all(item["status"] == "PASS" for item in coverage_records)
    status = "PASS" if sources and coverage_pass and pixels and coverage <= 0.12 and confidence >= 0.75 else "REVIEW_REQUIRED_MASK_UNCERTAIN"
    mask_path = Path(mask_path)
    mask_path.parent.mkdir(parents=True, exist_ok=True)
    if status == "PASS":
        cv2.imwrite(str(mask_path), page_mask)
    return {"status": status, "mask_generator_version": "AUTHORIZED_MASK_GENERATOR_V2_FULL_TEXT_COVERAGE",
            "mask_level": "B" if status == "PASS" else "C", "mask_source": "OCR geometry + outlined/component refinement",
            "image_sha256": image_sha, "mask_sha256": _sha256(mask_path) if status == "PASS" else None,
            "mask_path": str(mask_path) if status == "PASS" else None, "mask_coordinate_space": "PAGE_SPACE",
            "width": width, "height": height, "offset_x": 0, "offset_y": 0,
            "raw_mask_pixels": pixels, "core_pixels": core_pixels, "final_mask_pixels": pixels,
            "fringe_pixels": max(0, pixels - core_pixels), "expansion_ratio": round(pixels / max(1, core_pixels), 6),
            "coverage_ratio": round(coverage, 8), "component_count": component_count,
            "component_sizes": component_sizes, "mask_confidence": confidence, "sfx_excluded": True,
            "mask_coverage": {"status": "PASS" if coverage_pass else "FAIL",
                              "geometry_kind": "owned_components_plus_supported_light_outline",
                              "coverage_ratio": round(min((item["coverage_ratio"] for item in coverage_records), default=0.0), 6),
                              "minimum_coverage_ratio": MIN_TEXT_GEOMETRY_COVERAGE,
                              "region_id": region_id or "unassigned",
                              "target_group_index": selected_group[0] if selected_group else None,
                              "owned_group_id": sources[0]["owned_group_id"] if sources else None,
                              "groups": coverage_records},
            "content_type": content_type or "UNKNOWN", "page_id": page_id,
            "region_id": region_id or "multi-region", "regions": sources, "blocked_regions": blocked}
