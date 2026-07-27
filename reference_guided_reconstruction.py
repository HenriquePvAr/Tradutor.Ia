"""Offline, fail-closed reference-first artwork reconstruction primitives."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import cv2
import numpy as np

SCHEMA_VERSION = "1"


def _hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inventory_authorized_references(
    assets: Iterable[dict[str, Any]], *, owner: str,
    source_hash: str,
) -> dict[str, Any]:
    """Inventory only explicit, owner-scoped local assets."""
    candidates = []
    rejected = []
    for raw in assets:
        path = Path(str(raw.get("path") or ""))
        reasons = []
        if str(raw.get("owner") or "") != owner:
            reasons.append("reference_owner_mismatch")
        if not bool(raw.get("authorized")):
            reasons.append("reference_not_authorized")
        if not path.is_file():
            reasons.append("reference_asset_missing")
        image = cv2.imread(str(path)) if not reasons else None
        if image is None and not reasons:
            reasons.append("reference_asset_unreadable")
        record = {
            "source_type": str(raw.get("source_type") or "unknown"),
            "asset_role": str(raw.get("asset_role") or "unknown"),
            "lineage": dict(raw.get("lineage") or {}),
            "authorization_scope": str(
                raw.get("authorization_scope") or ""),
            "candidate_reason_codes": reasons,
        }
        if reasons:
            rejected.append(record)
            continue
        content_hash = _file_hash(path)
        payload = {
            **record,
            "owner": owner,
            "content_hash": content_hash,
            "dimensions": [int(image.shape[1]), int(image.shape[0])],
            "color_space": "BGR",
            "relationship_to_source": (
                "byte_identical_source" if content_hash == source_hash
                else str(raw.get("relationship_to_source") or "unverified")),
        }
        candidates.append({
            **payload,
            "reference_candidate_id": _hash(payload),
            "_path": str(path),
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "candidates": candidates,
        "rejected": rejected,
        "usable_candidate_count": len(candidates),
    }


def align_reference(
    source_bgr: np.ndarray,
    reference_bgr: np.ndarray,
    *,
    text_exclusion_mask: np.ndarray | None = None,
) -> dict[str, Any]:
    """Align using non-text ORB landmarks, preferring the simplest transform."""
    source = np.asarray(source_bgr, np.uint8)
    reference = np.asarray(reference_bgr, np.uint8)
    if source.ndim != 3 or reference.ndim != 3:
        raise ValueError("reference_alignment_requires_bgr")
    gray_source = cv2.cvtColor(source, cv2.COLOR_BGR2GRAY)
    gray_reference = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)
    mask = None
    if text_exclusion_mask is not None:
        exclusion = np.asarray(text_exclusion_mask) > 0
        if exclusion.shape != gray_source.shape:
            raise ValueError("reference_alignment_mask_shape_mismatch")
        mask = (~exclusion).astype(np.uint8) * 255
    orb = cv2.ORB_create(nfeatures=1200)
    source_points, source_desc = orb.detectAndCompute(gray_source, mask)
    reference_points, reference_desc = orb.detectAndCompute(
        gray_reference, None)
    reasons = []
    if source_desc is None or reference_desc is None:
        reasons.append("reference_landmarks_insufficient")
        return {"status": "reference_alignment_failed",
                "reason_codes": reasons}
    matches = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(
        reference_desc, source_desc, k=2)
    good = [first for pair in matches if len(pair) == 2
            for first, second in [pair] if first.distance < 0.75 * second.distance]
    if len(good) < 6:
        return {"status": "reference_alignment_failed",
                "reason_codes": ["reference_landmarks_insufficient"]}
    src = np.float32(
        [reference_points[item.queryIdx].pt for item in good]).reshape(-1, 1, 2)
    dst = np.float32(
        [source_points[item.trainIdx].pt for item in good]).reshape(-1, 1, 2)
    matrix, inliers = cv2.estimateAffinePartial2D(
        src, dst, method=cv2.RANSAC, ransacReprojThreshold=3.0)
    if matrix is None or abs(float(np.linalg.det(matrix[:, :2]))) < 1e-4:
        return {"status": "reference_alignment_failed",
                "reason_codes": ["reference_transform_degenerate"]}
    aligned = cv2.warpAffine(
        reference, matrix, (source.shape[1], source.shape[0]),
        flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
    validity = cv2.warpAffine(
        np.ones(reference.shape[:2], np.uint8) * 255, matrix,
        (source.shape[1], source.shape[0]), flags=cv2.INTER_NEAREST)
    selected = inliers.reshape(-1).astype(bool)
    projected = cv2.transform(src, matrix)
    errors = np.linalg.norm(projected - dst, axis=2).reshape(-1)
    reprojection = float(errors[selected].mean()) if np.any(selected) else 999.0
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_asset_hash": hashlib.sha256(source.tobytes()).hexdigest(),
        "reference_asset_hash":
            hashlib.sha256(reference.tobytes()).hexdigest(),
        "alignment_method": "similarity_affine",
        "transform_matrix": np.round(matrix, 8).tolist(),
        "inlier_count": int(selected.sum()),
        "match_count": len(good),
        "reprojection_error": round(reprojection, 6),
        "text_region_excluded": text_exclusion_mask is not None,
        "aligned_reference_hash": hashlib.sha256(aligned.tobytes()).hexdigest(),
        "validity_mask_hash": hashlib.sha256(validity.tobytes()).hexdigest(),
    }
    valid = int(selected.sum()) >= 4 and reprojection <= 3.0
    return {
        **payload,
        "alignment_id": _hash(payload),
        "aligned_reference": aligned,
        "validity_mask": validity,
        "status": "valid" if valid else "reference_alignment_failed",
        "reason_codes": [] if valid else ["reference_reprojection_error_high"],
    }


def transfer_observable_art(
    source_bgr: np.ndarray,
    aligned_reference_bgr: np.ndarray,
    *,
    authorized_change_mask: np.ndarray,
    observable_art_mask: np.ndarray,
    reference_text_mask: np.ndarray,
    uncertainty_mask: np.ndarray,
) -> dict[str, Any]:
    """Transfer only verified observable art inside the authorized mask."""
    source = np.asarray(source_bgr, np.uint8)
    reference = np.asarray(aligned_reference_bgr, np.uint8)
    shape = source.shape[:2]
    for value in (authorized_change_mask, observable_art_mask,
                  reference_text_mask, uncertainty_mask):
        if np.asarray(value).shape != shape:
            raise ValueError("reference_transfer_mask_shape_mismatch")
    transfer = ((np.asarray(authorized_change_mask) > 0)
                & (np.asarray(observable_art_mask) > 0)
                & ~(np.asarray(reference_text_mask) > 0)
                & ~(np.asarray(uncertainty_mask) > 0))
    output = source.copy()
    output[transfer] = reference[transfer]
    changed = np.any(output != source, axis=2)
    outside = int((changed & ~(np.asarray(authorized_change_mask) > 0)).sum())
    remaining = ((np.asarray(authorized_change_mask) > 0) & ~transfer)
    return {
        "image": output,
        "reference_transfer_mask": transfer.astype(np.uint8) * 255,
        "remaining_unobserved_mask": remaining.astype(np.uint8) * 255,
        "observed_background_pixels": int(transfer.sum()),
        "outside_mask_changes": outside,
        "reference_text_transferred": int(
            (transfer & (np.asarray(reference_text_mask) > 0)).sum()),
        "status": "valid" if outside == 0 else "reference_transfer_failed",
    }


def structural_diff(
    original_bgr: np.ndarray,
    translated_bgr: np.ndarray,
    *, expected_text_mask: np.ndarray,
) -> dict[str, Any]:
    original = np.asarray(original_bgr, np.uint8)
    translated = np.asarray(translated_bgr, np.uint8)
    changed = np.any(original != translated, axis=2)
    expected = np.asarray(expected_text_mask) > 0
    unexpected = changed & ~expected
    return {
        "expected_text_change_pixels": int((changed & expected).sum()),
        "unexpected_art_change_pixels": int(unexpected.sum()),
        "structural_diff_mask": unexpected.astype(np.uint8) * 255,
        "status": "passed" if not np.any(unexpected) else "blocked",
    }
