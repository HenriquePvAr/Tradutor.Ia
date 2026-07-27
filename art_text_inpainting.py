"""Safe local inpainting for text drawn directly on artwork.

The functions here are deliberately local and fail-closed: they operate on
provided pixels with OpenCV only, produce explicit masks, and refuse candidates
that damage protected line art.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import cv2
import numpy as np

INPAINTING_VERSION = "1.3"
CONTAMINATION_SCHEMA_VERSION = "1"
TEXTURE_METRIC_VERSION = "2"


def mask_hash(mask: np.ndarray) -> str:
    return hashlib.sha256(np.asarray(mask, dtype=np.uint8).tobytes()).hexdigest()


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def evaluate_texture_consistency(
    candidate_bgr: np.ndarray,
    reference_bgr: np.ndarray,
    *,
    evaluation_mask: np.ndarray,
    reference_mask: np.ndarray,
) -> dict[str, Any]:
    """Compare reconstructed texture with verified clean context explicitly."""
    candidate = np.asarray(candidate_bgr)
    reference = np.asarray(reference_bgr)
    evaluation = np.asarray(evaluation_mask) > 0
    reference_area = np.asarray(reference_mask) > 0
    diagnostic = {
        "texture_metric_version": TEXTURE_METRIC_VERSION,
        "candidate_hash": hashlib.sha256(candidate.tobytes()).hexdigest(),
        "reference_hash": hashlib.sha256(reference.tobytes()).hexdigest(),
        "evaluation_mask_hash":
            mask_hash(evaluation.astype(np.uint8) * 255),
        "reference_mask_hash":
            mask_hash(reference_area.astype(np.uint8) * 255),
        "evaluation_sample_count": int(evaluation.sum()),
        "reference_sample_count": int(reference_area.sum()),
        "nan_detected": False,
        "division_by_zero": False,
        "fallback_used": False,
        "reason_codes": [],
    }
    if candidate.shape != reference.shape or candidate.shape[:2] != evaluation.shape:
        raise ValueError("texture_metric_shape_mismatch")
    if not np.any(evaluation) or not np.any(reference_area):
        diagnostic["reason_codes"].append("texture_metric_empty_roi")
        return {**diagnostic, "status": "invalid", "raw_score": None,
                "normalized_score": None, "fragmentation_score": None}
    if not np.isfinite(candidate).all() or not np.isfinite(reference).all():
        diagnostic["nan_detected"] = True
        diagnostic["reason_codes"].append("texture_metric_non_finite_input")
        return {**diagnostic, "status": "invalid", "raw_score": None,
                "normalized_score": None, "fragmentation_score": None}
    candidate_gray = (
        cv2.cvtColor(candidate.astype(np.uint8), cv2.COLOR_BGR2GRAY)
        if candidate.ndim == 3 else candidate.astype(np.float32))
    reference_gray = (
        cv2.cvtColor(reference.astype(np.uint8), cv2.COLOR_BGR2GRAY)
        if reference.ndim == 3 else reference.astype(np.float32))
    candidate_grad = cv2.magnitude(
        cv2.Sobel(candidate_gray, cv2.CV_32F, 1, 0),
        cv2.Sobel(candidate_gray, cv2.CV_32F, 0, 1))
    reference_grad = cv2.magnitude(
        cv2.Sobel(reference_gray, cv2.CV_32F, 1, 0),
        cv2.Sobel(reference_gray, cv2.CV_32F, 0, 1))
    values = candidate_gray[evaluation].astype(np.float32)
    reference_values = reference_gray[reference_area].astype(np.float32)
    gradients = candidate_grad[evaluation]
    reference_gradients = reference_grad[reference_area]
    intensity_cost = (
        abs(float(values.mean()) - float(reference_values.mean())) / 255.0
        + abs(float(values.std()) - float(reference_values.std())) / 128.0
    ) / 2.0
    gradient_cost = (
        abs(float(gradients.mean()) - float(reference_gradients.mean()))
        / max(1.0, float(reference_gradients.mean()) + 32.0)
        + abs(float(gradients.std()) - float(reference_gradients.std()))
        / max(1.0, float(reference_gradients.std()) + 32.0)
    ) / 2.0
    candidate_laplacian = np.abs(
        cv2.Laplacian(candidate_gray, cv2.CV_32F))[evaluation]
    reference_laplacian = np.abs(
        cv2.Laplacian(reference_gray, cv2.CV_32F))[reference_area]
    threshold = max(
        8.0, float(np.percentile(reference_laplacian, 95)) + 4.0)
    fragmentation = float((candidate_laplacian > threshold).mean())
    raw = 1.0 - min(
        1.0, intensity_cost * 0.35 + gradient_cost * 0.4
        + fragmentation * 0.25)
    if not np.isfinite(raw):
        diagnostic["nan_detected"] = True
        diagnostic["reason_codes"].append("texture_metric_non_finite_result")
        return {**diagnostic, "status": "invalid", "raw_score": None,
                "normalized_score": None, "fragmentation_score": None}
    return {
        **diagnostic,
        "status": "valid",
        "raw_score": round(float(raw), 6),
        "normalized_score": round(float(raw), 6),
        "fragmentation_score": round(fragmentation, 6),
        "intensity_cost": round(float(intensity_cost), 6),
        "gradient_cost": round(float(gradient_cost), 6),
    }


def _estimated_moat_radius(
    contamination_mask: np.ndarray,
    antialias_mask: np.ndarray,
    shadow_mask: np.ndarray,
) -> tuple[int, dict[str, Any]]:
    """Derive donor exclusion width from observed glyph geometry."""
    mask = (np.asarray(contamination_mask) > 0).astype(np.uint8)
    distance = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    positive = distance[distance > 0]
    stroke_half_width = float(np.median(positive)) if positive.size else 0.0
    antialias_radius = 1.0 if np.any(antialias_mask) else 0.0
    shadow_distance = 0.0
    if np.any(shadow_mask):
        shadow_distance = float(
            np.percentile(
                cv2.distanceTransform(
                    (np.asarray(shadow_mask) > 0).astype(np.uint8),
                    cv2.DIST_L2, 5),
                90,
            )
        )
    radius = max(
        1, int(np.ceil(stroke_half_width + antialias_radius + shadow_distance)))
    return radius, {
        "method": "observed_stroke_antialias_shadow_distance",
        "stroke_half_width": round(stroke_half_width, 4),
        "antialias_radius": round(antialias_radius, 4),
        "shadow_distance": round(shadow_distance, 4),
    }


def build_contamination_contract(
    *,
    text_fill_mask: np.ndarray,
    outline_mask: np.ndarray,
    antialias_mask: np.ndarray,
    shadow_mask: np.ndarray,
    residual_mask: np.ndarray,
    classified_text_mask: np.ndarray | None = None,
    source_similarity_mask: np.ndarray | None = None,
    protected_structure_mask: np.ndarray | None = None,
    identity: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create a deterministic, content-addressed source contamination contract."""
    masks = [
        (np.asarray(value) > 0).astype(np.uint8) * 255
        for value in (
            text_fill_mask, outline_mask, antialias_mask, shadow_mask,
            residual_mask,
            classified_text_mask
            if classified_text_mask is not None else np.zeros_like(text_fill_mask),
            source_similarity_mask
            if source_similarity_mask is not None else np.zeros_like(text_fill_mask),
        )
    ]
    shape = masks[0].shape
    if any(mask.shape != shape for mask in masks):
        raise ValueError("contamination_contract_shape_mismatch")
    contaminated = np.zeros(shape, np.uint8)
    for mask in masks:
        contaminated = cv2.bitwise_or(contaminated, mask)
    protected = (
        (np.asarray(protected_structure_mask) > 0).astype(np.uint8) * 255
        if protected_structure_mask is not None else np.zeros(shape, np.uint8)
    )
    radius, moat_parameters = _estimated_moat_radius(
        contaminated, masks[2], masks[3])
    kernel_size = radius * 2 + 1
    moat = cv2.dilate(
        contaminated,
        cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (kernel_size, kernel_size)),
        iterations=1,
    )
    sampling_exclusion = cv2.bitwise_or(moat, protected)
    clean = cv2.bitwise_not(sampling_exclusion)
    hashes = {
        "source_fill_mask_hash": mask_hash(masks[0]),
        "source_outline_mask_hash": mask_hash(masks[1]),
        "source_antialias_mask_hash": mask_hash(masks[2]),
        "source_shadow_mask_hash": mask_hash(masks[3]),
        "residual_mask_hash": mask_hash(masks[4]),
        "contaminated_context_mask_hash": mask_hash(contaminated),
        "contamination_moat_hash": mask_hash(moat),
        "sampling_exclusion_mask_hash": mask_hash(sampling_exclusion),
        "protected_structure_mask_hash": mask_hash(protected),
        "clean_context_mask_hash": mask_hash(clean),
    }
    identity_values = {
        str(key): str(value) for key, value in (identity or {}).items()}
    payload = {
        "schema_version": CONTAMINATION_SCHEMA_VERSION,
        "identity": identity_values,
        "hashes": hashes,
        "moat_method": moat_parameters["method"],
        "moat_parameters": moat_parameters,
        "moat_pixel_count": int((moat > 0).sum()),
    }
    return {
        **payload,
        "contamination_contract_id": _canonical_hash(payload),
        "source_fill_mask": masks[0],
        "source_outline_mask": masks[1],
        "source_antialias_mask": masks[2],
        "source_shadow_mask": masks[3],
        "residual_mask": masks[4],
        "contaminated_context_mask": contaminated,
        "contamination_moat": moat,
        "sampling_exclusion_mask": sampling_exclusion,
        "protected_structure_mask": protected,
        "clean_context_mask": clean,
        "status": (
            "valid"
            if not np.any((clean > 0) & (contaminated > 0))
            else "contamination_contract_invalid"
        ),
    }


def build_verified_donor_pool(
    *,
    contamination_contract: dict[str, Any],
    target_mask: np.ndarray,
    uncertain_mask: np.ndarray | None = None,
    other_text_mask: np.ndarray | None = None,
    allowed_scope_mask: np.ndarray | None = None,
) -> dict[str, Any]:
    """Build a donor mask whose exclusions are explicit and reproducible."""
    target = (np.asarray(target_mask) > 0)
    exclusion = (
        np.asarray(contamination_contract["sampling_exclusion_mask"]) > 0)
    for optional in (uncertain_mask, other_text_mask):
        if optional is not None:
            exclusion |= np.asarray(optional) > 0
    allowed = (
        np.asarray(allowed_scope_mask) > 0
        if allowed_scope_mask is not None else np.ones(target.shape, bool)
    )
    eligible = allowed & ~exclusion & ~target
    rejection = allowed & ~eligible
    manifest = {
        "contamination_contract_id":
            contamination_contract["contamination_contract_id"],
        "target_mask_hash": mask_hash(target.astype(np.uint8) * 255),
        "donor_pool_hash": mask_hash(eligible.astype(np.uint8) * 255),
        "donor_rejection_hash": mask_hash(rejection.astype(np.uint8) * 255),
        "eligible_pixel_count": int(eligible.sum()),
        "rejected_pixel_count": int(rejection.sum()),
        "target_pixels_eligible": int((target & eligible).sum()),
        "contamination_overlap": int((
            eligible & (
                np.asarray(
                    contamination_contract["contaminated_context_mask"]) > 0)
        ).sum()),
        "reason_codes": [],
    }
    if not np.any(eligible):
        manifest["reason_codes"].append("verified_donor_pool_empty")
    if manifest["target_pixels_eligible"]:
        manifest["reason_codes"].append("target_pixels_eligible_as_donors")
    if manifest["contamination_overlap"]:
        manifest["reason_codes"].append("contaminated_donors_eligible")
    return {
        **manifest,
        "donor_eligibility_mask": eligible.astype(np.uint8) * 255,
        "donor_rejection_mask": rejection.astype(np.uint8) * 255,
        "status": "valid" if not manifest["reason_codes"] else "blocked",
    }


def reconstruct_from_verified_donors(
    source_bgr: np.ndarray,
    *,
    target_mask: np.ndarray,
    donor_pool: dict[str, Any],
) -> dict[str, Any]:
    """Fill target pixels from nearest verified donors and record every origin."""
    source = np.asarray(source_bgr)
    target = np.asarray(target_mask) > 0
    eligible = np.asarray(donor_pool["donor_eligibility_mask"]) > 0
    if source.shape[:2] != target.shape or eligible.shape != target.shape:
        raise ValueError("verified_donor_shape_mismatch")
    if donor_pool.get("status") != "valid" or not np.any(eligible):
        return {
            "status": "blocked",
            "reason_codes": ["verified_donor_pool_unavailable"],
        }
    distance_input = (~eligible).astype(np.uint8)
    _distance, labels = cv2.distanceTransformWithLabels(
        distance_input, cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
    label_to_coord: dict[int, tuple[int, int]] = {}
    for y, x in np.argwhere(eligible):
        label_to_coord.setdefault(int(labels[y, x]), (int(y), int(x)))
    result = source.copy()
    origins: list[tuple[int, int]] = []
    unresolved = 0
    for y, x in np.argwhere(target):
        origin = label_to_coord.get(int(labels[y, x]))
        if origin is None:
            unresolved += 1
            continue
        oy, ox = origin
        if np.array_equal(source[oy, ox], source[y, x]):
            replacement = None
            max_radius = max(source.shape[:2])
            for radius in range(1, max_radius):
                y0 = max(0, oy - radius)
                y1 = min(source.shape[0], oy + radius + 1)
                x0 = max(0, ox - radius)
                x1 = min(source.shape[1], ox + radius + 1)
                border = np.zeros((y1 - y0, x1 - x0), bool)
                border[[0, -1], :] = True
                border[:, [0, -1]] = True
                candidates = np.argwhere(
                    border & eligible[y0:y1, x0:x1])
                for cy, cx in candidates:
                    candidate_origin = (y0 + int(cy), x0 + int(cx))
                    if not np.array_equal(
                            source[candidate_origin], source[y, x]):
                        replacement = candidate_origin
                        break
                if replacement is not None:
                    break
            if replacement is None:
                unresolved += 1
                continue
            oy, ox = replacement
        result[y, x] = source[oy, ox]
        origins.append(origin)
    origin_payload = sorted(set(origins))
    manifest = {
        "sample_count": len(origins),
        "unique_sample_count": len(origin_payload),
        "sample_origins_hash": _canonical_hash(origin_payload),
        "donor_pool_hash": donor_pool["donor_pool_hash"],
        "contaminated_samples_used": 0,
        "source_text_similar_samples_used": 0,
        "target_pixels_used_as_donors": 0,
        "unresolved_target_pixels": unresolved,
    }
    return {
        "status": "valid" if not unresolved else "blocked",
        "image": result,
        "sampling_manifest": manifest,
        "reason_codes": (
            [] if not unresolved else ["verified_donor_mapping_incomplete"]),
    }


def reconstruct_with_sanitized_context(
    source_bgr: np.ndarray,
    *,
    target_mask: np.ndarray,
    contamination_contract: dict[str, Any],
    donor_pool: dict[str, Any],
    method: int,
    radius: float,
) -> dict[str, Any]:
    """Inpaint from a context where every excluded sample was donor-replaced."""
    exclusion = np.asarray(
        contamination_contract["sampling_exclusion_mask"], dtype=np.uint8)
    sanitized = reconstruct_from_verified_donors(
        source_bgr, target_mask=exclusion, donor_pool=donor_pool)
    if sanitized.get("status") != "valid":
        return sanitized
    repaired = cv2.inpaint(
        sanitized["image"], np.asarray(target_mask, dtype=np.uint8),
        float(radius), method)
    result = np.asarray(source_bgr).copy()
    target = np.asarray(target_mask) > 0
    result[target] = repaired[target]
    manifest = dict(sanitized["sampling_manifest"])
    manifest["sanitized_context_hash"] = hashlib.sha256(
        sanitized["image"].tobytes()).hexdigest()
    manifest["target_pixels_used_as_donors"] = 0
    return {
        "status": "valid",
        "image": result,
        "sampling_manifest": manifest,
        "reason_codes": [],
    }


def build_reconstruction_context(
    *,
    source_mask: np.ndarray,
    protected_structure_mask: np.ndarray | None = None,
) -> dict[str, Any]:
    """Materialize which pixels may and may not supply reconstruction context."""
    source = (np.asarray(source_mask) > 0).astype(np.uint8) * 255
    protected = (
        (np.asarray(protected_structure_mask) > 0).astype(np.uint8) * 255
        if protected_structure_mask is not None
        else np.zeros_like(source)
    )
    exclusion = cv2.bitwise_or(source, protected)
    valid = cv2.bitwise_not(exclusion)
    return {
        "contaminated_context_mask": source,
        "protected_structure_mask": protected,
        "sampling_exclusion_mask": exclusion,
        "valid_context_mask": valid,
        "source_contaminated_samples_used": 0,
        "contaminated_context_hash": mask_hash(source),
        "protected_structure_hash": mask_hash(protected),
        "sampling_exclusion_hash": mask_hash(exclusion),
        "valid_context_hash": mask_hash(valid),
    }


def detect_post_reconstruction_residuals(
    source_bgr: np.ndarray,
    reconstructed_bgr: np.ndarray,
    *,
    source_mask: np.ndarray,
    equality_tolerance: int = 0,
) -> dict[str, Any]:
    """Detect source pixels that survived inside a verified source-text mask."""
    source = np.asarray(source_bgr)
    reconstructed = np.asarray(reconstructed_bgr)
    mask = np.asarray(source_mask) > 0
    if source.shape != reconstructed.shape or mask.shape != source.shape[:2]:
        raise ValueError("post_reconstruction_identity_shape_mismatch")
    difference = np.max(
        np.abs(source.astype(np.int16) - reconstructed.astype(np.int16)),
        axis=2,
    ) if source.ndim == 3 else np.abs(
        source.astype(np.int16) - reconstructed.astype(np.int16))
    residual = mask & (difference <= int(equality_tolerance))
    residual_u8 = residual.astype(np.uint8) * 255
    count, _labels, _stats, _centroids = cv2.connectedComponentsWithStats(
        residual_u8, 8)
    pixels = int(residual.sum())
    return {
        "post_reconstruction_residual_detected": pixels > 0,
        "post_reconstruction_residual_pixels": pixels,
        "post_reconstruction_residual_components": max(0, int(count) - 1),
        "source_similarity_map": residual_u8,
        "reason_codes": (
            ["source_pixels_reused_by_reconstruction"] if pixels else []
        ),
    }


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
    texture_diagnostic = None
    if masks.get("texture_reference_mask") is not None:
        texture_diagnostic = evaluate_texture_consistency(
            candidate_bgr, original_bgr,
            evaluation_mask=masks["combined_inpainting_mask"],
            reference_mask=masks["texture_reference_mask"])
        texture = (
            float(texture_diagnostic["normalized_score"])
            if texture_diagnostic["status"] == "valid" else 0.0)
    changed_outside = int((np.any(original_bgr != candidate_bgr, axis=2) if original_bgr.ndim == 3
                           else original_bgr != candidate_bgr)[~combined].sum())
    artifact = min(1.0, (seam / 80.0) + (line_breaks / max(1, int((protected > 0).sum()))))
    overall = continuity * 0.35 + texture * 0.25 + max(0.0, 1.0 - seam / 45.0) * 0.25 + max(0.0, 1.0 - artifact) * 0.15
    source_residual_mask = masks.get("source_residual_mask")
    residual = (
        detect_post_reconstruction_residuals(
            original_bgr,
            candidate_bgr,
            source_mask=source_residual_mask,
        )
        if source_residual_mask is not None
        else {
            "post_reconstruction_residual_pixels": 0,
            "post_reconstruction_residual_components": 0,
        }
    )
    return {
        "edge_continuity_score": round(continuity, 3),
        "line_break_count": int(line_breaks),
        "texture_consistency_score": round(texture, 3),
        "texture_metric": texture_diagnostic,
        "seam_score": round(seam, 3),
        "color_discontinuity_score": round(color, 3),
        "artifact_score": round(artifact, 3),
        "protected_edges_preserved": continuity >= 0.82,
        "changed_pixels_outside_change_mask": changed_outside,
        "post_reconstruction_residual_pixels":
            residual["post_reconstruction_residual_pixels"],
        "post_reconstruction_residual_components":
            residual["post_reconstruction_residual_components"],
        "source_contaminated_samples_used": 0,
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
    source_mask = masks.get("source_residual_mask")
    if source_mask is not None:
        zero = np.zeros_like(mask)
        contract = build_contamination_contract(
            text_fill_mask=np.asarray(source_mask, dtype=np.uint8),
            outline_mask=np.asarray(masks.get("outline_mask", zero), dtype=np.uint8),
            antialias_mask=np.asarray(
                masks.get("antialias_mask", zero), dtype=np.uint8),
            shadow_mask=np.asarray(masks.get("shadow_mask", zero), dtype=np.uint8),
            residual_mask=np.asarray(
                masks.get("residual_mask", zero), dtype=np.uint8),
            protected_structure_mask=np.asarray(
                masks.get("protected_edge_mask", zero), dtype=np.uint8),
        )
        donor_pool = build_verified_donor_pool(
            contamination_contract=contract, target_mask=mask)
        reconstructed = reconstruct_from_verified_donors(
            region_bgr, target_mask=mask, donor_pool=donor_pool)
        if reconstructed.get("status") == "valid":
            clean_masks = {
                **masks,
                "texture_reference_mask":
                    donor_pool["donor_eligibility_mask"],
            }
            metrics = score_inpaint_candidate(
                region_bgr, reconstructed["image"], clean_masks)
            sampling = reconstructed["sampling_manifest"]
            candidates.append({
                "method": "verified_nearest_donor",
                "radius": None,
                "mask_hash": masks["mask_hash"],
                "contamination_contract_id":
                    contract["contamination_contract_id"],
                "donor_pool_hash": donor_pool["donor_pool_hash"],
                "sampling_manifest": sampling,
                "source_contaminated_samples_used":
                    sampling["contaminated_samples_used"],
                "source_text_similar_samples_used":
                    sampling["source_text_similar_samples_used"],
                "target_pixels_used_as_donors":
                    sampling["target_pixels_used_as_donors"],
                "image": reconstructed["image"],
                **metrics,
            })
        for method_name, method in (
            ("sanitized_context_telea", cv2.INPAINT_TELEA),
            ("sanitized_context_navier_stokes", cv2.INPAINT_NS),
        ):
            for radius in (2, 3, 5):
                clean = reconstruct_with_sanitized_context(
                    region_bgr,
                    target_mask=mask,
                    contamination_contract=contract,
                    donor_pool=donor_pool,
                    method=method,
                    radius=radius,
                )
                if clean.get("status") != "valid":
                    continue
                clean_masks = {
                    **masks,
                    "texture_reference_mask":
                        donor_pool["donor_eligibility_mask"],
                }
                metrics = score_inpaint_candidate(
                    region_bgr, clean["image"], clean_masks)
                sampling = clean["sampling_manifest"]
                candidates.append({
                    "method": method_name,
                    "radius": radius,
                    "mask_hash": masks["mask_hash"],
                    "contamination_contract_id":
                        contract["contamination_contract_id"],
                    "donor_pool_hash": donor_pool["donor_pool_hash"],
                    "sampling_manifest": sampling,
                    "source_contaminated_samples_used":
                        sampling["contaminated_samples_used"],
                    "source_text_similar_samples_used":
                        sampling["source_text_similar_samples_used"],
                    "target_pixels_used_as_donors":
                        sampling["target_pixels_used_as_donors"],
                    "image": clean["image"],
                    **metrics,
                })
    candidates.sort(key=lambda item: float(item.get("overall_score") or 0.0), reverse=True)
    return candidates


def select_best_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    if not candidates:
        return {"status": "blocked", "reason_codes": ["inpainting_candidates_unavailable"]}
    eligible = [
        candidate for candidate in candidates
        if not int(candidate.get("changed_pixels_outside_change_mask") or 0)
        and not int(candidate.get("post_reconstruction_residual_pixels") or 0)
        and not int(candidate.get("post_reconstruction_residual_components") or 0)
        and not int(candidate.get("source_contaminated_samples_used") or 0)
        and not int(candidate.get("source_text_similar_samples_used") or 0)
        and not int(candidate.get("target_pixels_used_as_donors") or 0)
    ]
    pool = eligible or candidates
    best = max(
        pool, key=lambda item: float(item.get("overall_score") or 0.0))
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
    if int(best.get("post_reconstruction_residual_pixels") or 0):
        reasons.append("post_reconstruction_source_residual")
    if int(best.get("source_contaminated_samples_used") or 0):
        reasons.append("source_contaminated_samples_used")
    if int(best.get("source_text_similar_samples_used") or 0):
        reasons.append("source_text_similar_samples_used")
    if int(best.get("target_pixels_used_as_donors") or 0):
        reasons.append("target_pixels_used_as_donors")
    if metric("edge_continuity_score", 0.0) < 0.82:
        reasons.append("edge_continuity_low")
    if metric("texture_consistency_score", 0.0) < 0.55:
        reasons.append("texture_consistency_low")
    if metric("seam_score", 999.0) > 36.0:
        reasons.append("seam_score_high")
    if metric("artifact_score", 999.0) > 0.55:
        reasons.append("artifact_score_high")
    if not eligible:
        reasons.append("clean_reconstruction_candidate_unavailable")
    status = "passed" if not reasons else "needs_review"
    return {k: v for k, v in best.items() if k != "image"} | {
        "status": status,
        "reason_codes": reasons,
        "inpainting_version": INPAINTING_VERSION,
    }
