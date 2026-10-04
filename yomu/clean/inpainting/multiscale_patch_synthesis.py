"""Deterministic, resource-bounded patch synthesis from verified clean donors."""
from __future__ import annotations

import hashlib
import json
from typing import Any

import cv2
import numpy as np


SCHEMA_VERSION = "1"


def _hash_bytes(value: np.ndarray) -> str:
    return hashlib.sha256(np.asarray(value).tobytes()).hexdigest()


def _hash_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8")).hexdigest()


def analyze_texture_profile(
    image_bgr: np.ndarray,
    donor_eligibility_mask: np.ndarray,
) -> dict[str, Any]:
    image = np.asarray(image_bgr)
    eligible = np.asarray(donor_eligibility_mask) > 0
    if image.shape[:2] != eligible.shape or not np.any(eligible):
        return {"status": "blocked",
                "reason_codes": ["insufficient_clean_texture_context"]}
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1)
    magnitude = cv2.magnitude(gx, gy)
    angles = cv2.phase(gx, gy, angleInDegrees=True)
    values = gray[eligible].astype(np.float32)
    gradients = magnitude[eligible]
    orientation_histogram, _ = np.histogram(
        angles[eligible], bins=12, range=(0.0, 360.0),
        weights=gradients)
    local_mean = cv2.blur(gray.astype(np.float32), (3, 3))
    local_variance = cv2.blur(
        (gray.astype(np.float32) - local_mean) ** 2, (3, 3))
    edge_density = float((gradients > 24.0).mean())
    granularity = max(
        1.0, float(np.sqrt(max(1.0, np.median(local_variance[eligible])))))
    dominant_bin = int(np.argmax(orientation_histogram))
    profile = {
        "schema_version": SCHEMA_VERSION,
        "image_hash": _hash_bytes(image),
        "donor_mask_hash": _hash_bytes(
            eligible.astype(np.uint8) * 255),
        "eligible_sample_count": int(eligible.sum()),
        "luminance_mean": round(float(values.mean()), 6),
        "luminance_std": round(float(values.std()), 6),
        "gradient_mean": round(float(gradients.mean()), 6),
        "gradient_std": round(float(gradients.std()), 6),
        "edge_density": round(edge_density, 6),
        "granularity": round(granularity, 6),
        "dominant_orientation_degrees": dominant_bin * 30.0 + 15.0,
        "orientation_histogram": [
            round(float(value), 6) for value in orientation_histogram],
    }
    return {
        **profile,
        "texture_profile_id": _hash_json(profile),
        "status": "valid",
        "reason_codes": [],
    }


def derive_patch_schedule(
    texture_profile: dict[str, Any],
    target_mask: np.ndarray,
) -> dict[str, Any]:
    target = np.asarray(target_mask) > 0
    if texture_profile.get("status") != "valid" or not np.any(target):
        return {"status": "blocked", "reason_codes": ["patch_schedule_input_invalid"]}
    ys, xs = np.where(target)
    height = int(ys.max() - ys.min() + 1)
    width = int(xs.max() - xs.min() + 1)
    minimum = max(3, min(height, width))
    granularity = float(texture_profile["granularity"])
    finest = max(3, int(round(granularity)) | 1)
    finest = min(finest, max(3, (minimum // 3) | 1))
    sizes = []
    size = finest
    while size <= minimum and len(sizes) < max(1, int(np.log2(minimum))):
        sizes.append(int(size))
        size = (size * 2 + 1) | 1
    sizes = sorted(set(sizes), reverse=True)
    levels = [
        {
            "level": index,
            "patch_size": patch_size,
            "stride": max(1, patch_size // 3),
            "overlap": patch_size - max(1, patch_size // 3),
            "candidate_limit": max(32, int(np.sqrt(target.sum()))),
        }
        for index, patch_size in enumerate(sizes)
    ]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "target_mask_hash": _hash_bytes(target.astype(np.uint8) * 255),
        "texture_profile_id": texture_profile["texture_profile_id"],
        "levels": levels,
        "derivation": "granularity_target_geometry_resource_bound",
    }
    return {**payload, "patch_schedule_id": _hash_json(payload),
            "status": "valid", "reason_codes": []}


def build_deterministic_pyramid(
    image_bgr: np.ndarray,
    target_mask: np.ndarray,
    donor_mask: np.ndarray,
    schedule: dict[str, Any],
) -> dict[str, Any]:
    image = np.asarray(image_bgr)
    target = np.asarray(target_mask, np.uint8)
    donor = np.asarray(donor_mask, np.uint8)
    level_count = max(1, len(schedule.get("levels") or []))
    images = [image.copy()]
    targets = [target.copy()]
    donors = [donor.copy()]
    while len(images) < level_count and min(images[-1].shape[:2]) >= 16:
        size = (
            max(1, images[-1].shape[1] // 2),
            max(1, images[-1].shape[0] // 2),
        )
        images.append(cv2.resize(
            images[-1], size, interpolation=cv2.INTER_AREA))
        targets.append(cv2.resize(
            targets[-1], size, interpolation=cv2.INTER_NEAREST))
        donors.append(cv2.resize(
            donors[-1], size, interpolation=cv2.INTER_NEAREST))
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "level_count": len(images),
        "level_dimensions": [
            [int(item.shape[1]), int(item.shape[0])] for item in images],
        "level_hashes": [_hash_bytes(item) for item in images],
        "downsample_method": "opencv_inter_area",
        "mask_resize_policy": "opencv_inter_nearest",
        "dtype": str(image.dtype),
    }
    return {
        **manifest,
        "pyramid_id": _hash_json(manifest),
        "images": images,
        "targets": targets,
        "donors": donors,
        "status": "valid",
    }


def synthesize_multiscale(
    source_bgr: np.ndarray,
    *,
    target_mask: np.ndarray,
    donor_eligibility_mask: np.ndarray,
    patch_schedule: dict[str, Any],
) -> dict[str, Any]:
    """Fill coarse-to-fine with coherent donor patches and weighted overlaps."""
    source = np.asarray(source_bgr)
    target = np.asarray(target_mask) > 0
    donor = np.asarray(donor_eligibility_mask) > 0
    pyramid = build_deterministic_pyramid(
        source, target.astype(np.uint8) * 255,
        donor.astype(np.uint8) * 255, patch_schedule)
    result = None
    manifests = []
    total_operations = 0
    for reverse_index, level in enumerate(
            range(len(pyramid["images"]) - 1, -1, -1)):
        image = pyramid["images"][level]
        level_target = pyramid["targets"][level] > 0
        level_donor = pyramid["donors"][level] > 0
        if result is None:
            result = image.astype(np.float32)
        else:
            result = cv2.resize(
                result, (image.shape[1], image.shape[0]),
                interpolation=cv2.INTER_LINEAR)
            result[~level_target] = image[~level_target]
        schedule_level = patch_schedule["levels"][
            min(reverse_index, len(patch_schedule["levels"]) - 1)]
        patch_size = min(
            int(schedule_level["patch_size"]),
            max(3, min(image.shape[:2]) // 2 * 2 - 1))
        radius = patch_size // 2
        kernel = np.ones((patch_size, patch_size), np.uint8)
        valid_centers = cv2.erode(
            level_donor.astype(np.uint8), kernel) > 0
        donor_centers = np.argwhere(valid_centers)
        limit = int(schedule_level["candidate_limit"])
        if len(donor_centers) > limit:
            indices = np.linspace(
                0, len(donor_centers) - 1, limit, dtype=int)
            donor_centers = donor_centers[indices]
        distance = cv2.distanceTransform(
            level_target.astype(np.uint8), cv2.DIST_L2, 5)
        target_centers = np.argwhere(level_target)
        target_centers = sorted(
            (tuple(map(int, point)) for point in target_centers),
            key=lambda point: (
                float(distance[point]), point[0], point[1]))
        stride = int(schedule_level["stride"])
        target_centers = target_centers[::stride]
        weights = np.zeros(level_target.shape, np.float32)
        accumulation = np.zeros_like(result, np.float32)
        reuse: dict[tuple[int, int], int] = {}
        selected_origins = []
        for cy, cx in target_centers:
            y0, y1 = max(0, cy - radius), min(image.shape[0], cy + radius + 1)
            x0, x1 = max(0, cx - radius), min(image.shape[1], cx + radius + 1)
            patch_target = level_target[y0:y1, x0:x1]
            known = ~patch_target
            best = None
            for dy, dx in donor_centers:
                sy0, sx0 = int(dy) - radius, int(dx) - radius
                sy1, sx1 = sy0 + (y1 - y0), sx0 + (x1 - x0)
                if sy0 < 0 or sx0 < 0 or sy1 > image.shape[0] or sx1 > image.shape[1]:
                    continue
                donor_patch = image[sy0:sy1, sx0:sx1].astype(np.float32)
                boundary_cost = (
                    float(np.mean(np.abs(
                        result[y0:y1, x0:x1][known] - donor_patch[known])))
                    if np.any(known) else 0.0)
                reuse_cost = reuse.get((int(dy), int(dx)), 0) * 2.0
                spatial_cost = np.hypot(cy - int(dy), cx - int(dx)) / max(
                    image.shape[:2])
                cost = boundary_cost + reuse_cost + spatial_cost
                key = (round(cost, 6), int(dy), int(dx))
                if best is None or key < best[0]:
                    best = (key, donor_patch, (int(dy), int(dx)))
                total_operations += 1
            if best is None:
                continue
            donor_patch, origin = best[1], best[2]
            reuse[origin] = reuse.get(origin, 0) + 1
            selected_origins.append(origin)
            yy, xx = np.mgrid[0:y1-y0, 0:x1-x0]
            feather = np.minimum.reduce([
                yy + 1, xx + 1, y1 - y0 - yy, x1 - x0 - xx
            ]).astype(np.float32)
            apply = patch_target
            accumulation[y0:y1, x0:x1][apply] += (
                donor_patch[apply] * feather[apply, None])
            weights[y0:y1, x0:x1][apply] += feather[apply]
        filled = weights > 0
        result[filled] = accumulation[filled] / weights[filled, None]
        manifests.append({
            "level": level,
            "patch_size": patch_size,
            "target_patch_count": len(target_centers),
            "donor_patch_count": len(donor_centers),
            "selected_origins_hash": _hash_json(selected_origins),
            "unique_donors_used": len(set(selected_origins)),
            "max_patch_reuse": max(reuse.values(), default=0),
            "filled_pixels": int(filled.sum()),
        })
    final = source.copy()
    if result is not None:
        converted = np.clip(result, 0, 255).astype(source.dtype)
        final[target] = converted[target]
    unresolved = int((target & np.all(final == source, axis=2)).sum())
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "patch_schedule_id": patch_schedule.get("patch_schedule_id"),
        "pyramid_id": pyramid["pyramid_id"],
        "levels": manifests,
        "actual_operations": total_operations,
        "target_pixels_used_as_donors": 0,
        "contaminated_samples_used": 0,
        "source_text_similar_samples_used": 0,
        "changed_pixels_outside_mask": int(
            np.any(final != source, axis=2)[~target].sum()),
        "unresolved_target_pixels": unresolved,
    }
    return {
        "status": "valid" if not unresolved else "blocked",
        "image": final,
        "pyramid": pyramid,
        "sampling_manifest": manifest,
        "reason_codes": (
            [] if not unresolved else ["multiscale_target_incomplete"]),
    }
