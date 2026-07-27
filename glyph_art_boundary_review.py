"""Deterministic artifacts and fail-closed drafts for glyph/art boundaries."""
from __future__ import annotations

import hashlib
import json
from typing import Any

import cv2
import numpy as np

SCHEMA_VERSION = "1"
CLASSIFICATIONS = {
    "glyph_fill", "glyph_outline", "glyph_antialias", "glyph_shadow",
    "glyph_punctuation", "protected_art", "uncertain",
}


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def bitmap_hash(mask: np.ndarray) -> str:
    return hashlib.sha256(
        ((np.asarray(mask) > 0).astype(np.uint8) * 255).tobytes()).hexdigest()


def build_conflict_artifact(
    conflict_bitmap: np.ndarray, *, identity: dict[str, str],
    glyph_envelope_id: str, protected_art_hash: str,
    previous_mask_hash: str,
) -> dict[str, Any]:
    mask = (np.asarray(conflict_bitmap) > 0).astype(np.uint8) * 255
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(
        (mask > 0).astype(np.uint8), 8)
    segments = []
    for label in range(1, count):
        component = (labels == label).astype(np.uint8) * 255
        x, y, w, h, pixels = [int(value) for value in stats[label]]
        contour_data = cv2.findContours(
            component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]
        contour_bytes = b"".join(
            np.asarray(contour, np.int32).tobytes() for contour in contour_data)
        segment_payload = {
            "segment_bitmap_hash": bitmap_hash(component),
            "bounds": [x, y, x + w, y + h],
            "pixel_count": pixels,
            "centroid": [
                round(float(centroids[label][0]), 4),
                round(float(centroids[label][1]), 4),
            ],
            "contour_hash": hashlib.sha256(contour_bytes).hexdigest(),
            "suggested_classification": "uncertain",
            "suggested_confidence": 0.0,
            "reason_codes": ["glyph_art_evidence_conflict"],
        }
        segments.append({
            **segment_payload,
            "segment_id": _digest(segment_payload),
        })
    segments.sort(key=lambda item: item["segment_id"])
    manifest_hash = _digest([
        item["segment_id"] for item in segments])
    payload = {
        "schema_version": SCHEMA_VERSION,
        "identity": {str(k): str(v) for k, v in identity.items()},
        "glyph_envelope_id": str(glyph_envelope_id),
        "protected_art_hash": str(protected_art_hash),
        "previous_mask_hash": str(previous_mask_hash),
        "conflict_bitmap_hash": bitmap_hash(mask),
        "conflict_pixel_count": int((mask > 0).sum()),
        "component_manifest_hash": manifest_hash,
        "segment_manifest_hash": manifest_hash,
        "status": "pending_human_boundary_review",
    }
    return {
        **payload,
        "conflict_artifact_id": _digest(payload),
        "segments": segments,
        "conflict_bitmap": mask,
    }


def normalize_review_draft(
    *, conflict_artifact_id: str, owner: str,
    segment_decisions: dict[str, str] | None = None,
    brush_operations: list[dict[str, Any]] | None = None,
    segment_operations: list[dict[str, Any]] | None = None,
    view_state: dict[str, Any] | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    decisions = {
        str(segment): str(classification)
        for segment, classification in (segment_decisions or {}).items()}
    if any(value not in CLASSIFICATIONS for value in decisions.values()):
        raise ValueError("boundary_review_classification_invalid")
    operations = list(brush_operations or [])
    segment_ops = list(segment_operations or [])
    uncertain = sum(1 for value in decisions.values() if value == "uncertain")
    unresolved = sum(1 for value in decisions.values() if not value)
    if confirm and (uncertain or unresolved or not decisions):
        raise ValueError("boundary_review_uncertainty_remaining")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "owner": str(owner),
        "conflict_artifact_id": str(conflict_artifact_id),
        "segment_decisions": dict(sorted(decisions.items())),
        "brush_operations": operations,
        "segment_operations": segment_ops,
        "view_state": dict(view_state or {}),
        "status": "confirmed" if confirm else (
            "blocked_with_uncertainty" if uncertain else "draft"),
    }
    return {
        **payload,
        "decision_payload_hash": _digest(payload),
        "review_decision_id": _digest({
            "owner": owner,
            "conflict_artifact_id": conflict_artifact_id,
            "payload": payload,
        }),
    }
