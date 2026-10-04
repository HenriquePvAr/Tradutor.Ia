"""Deterministic, fail-closed visual envelopes for rasterized source glyphs.

The model is deliberately page-agnostic.  A confirmed text seed authorizes a
local search, but never authorizes a pixel by itself.  Layer evidence,
connectivity, observed stroke geometry and protected-art evidence decide the
result; disagreements remain uncertain.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import cv2
import numpy as np

SCHEMA_VERSION = "1"
IMPLEMENTATION_REVISION = "source-glyph-envelope-v1"


def _binary(value: Any, shape: tuple[int, int] | None = None) -> np.ndarray:
    array = np.asarray(value, dtype=np.uint8)
    if array.ndim != 2 or (shape is not None and array.shape != shape):
        raise ValueError("glyph_envelope_shape_mismatch")
    return (array > 0).astype(np.uint8) * 255


def _hash_mask(mask: np.ndarray) -> str:
    return hashlib.sha256(_binary(mask).tobytes()).hexdigest()


def _canonical_hash(value: Any) -> str:
    data = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _observed_search_radius(seed: np.ndarray) -> tuple[int, dict[str, float]]:
    distance = cv2.distanceTransform((seed > 0).astype(np.uint8), cv2.DIST_L2, 5)
    positive = distance[distance > 0]
    half_stroke = float(np.percentile(positive, 75)) if positive.size else 0.0
    # The search radius is an observed upper bound, never a classification.
    radius = max(1, int(np.ceil(half_stroke * 2.0)))
    return radius, {"observed_stroke_half_width": round(half_stroke, 4)}


def _connected_to_seed(candidate: np.ndarray, seed: np.ndarray) -> np.ndarray:
    labels_count, labels = cv2.connectedComponents(
        (candidate > 0).astype(np.uint8), connectivity=8)
    retained = np.zeros(candidate.shape, np.uint8)
    seed_labels = set(int(value) for value in np.unique(labels[seed > 0]))
    seed_labels.discard(0)
    for label in seed_labels:
        if label < labels_count:
            retained[labels == label] = 255
    return retained


def derive_source_glyph_visual_envelope(
    *,
    source_bgr: np.ndarray,
    confirmed_text_seed: np.ndarray,
    fill_evidence: np.ndarray,
    outline_evidence: np.ndarray,
    antialias_evidence: np.ndarray,
    protected_art: np.ndarray,
    shadow_evidence: np.ndarray | None = None,
    glow_evidence: np.ndarray | None = None,
    punctuation_evidence: np.ndarray | None = None,
    identity: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a content-addressed envelope from independent local evidence."""
    image = np.asarray(source_bgr)
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("glyph_envelope_source_must_be_bgr")
    shape = image.shape[:2]
    seed = _binary(confirmed_text_seed, shape)
    fill = _binary(fill_evidence, shape)
    outline = _binary(outline_evidence, shape)
    antialias = _binary(antialias_evidence, shape)
    protected = _binary(protected_art, shape)
    shadow = (_binary(shadow_evidence, shape) if shadow_evidence is not None
              else np.zeros(shape, np.uint8))
    glow = (_binary(glow_evidence, shape) if glow_evidence is not None
            else np.zeros(shape, np.uint8))
    punctuation = (
        _binary(punctuation_evidence, shape)
        if punctuation_evidence is not None else np.zeros(shape, np.uint8))
    if not np.any(seed):
        raise ValueError("glyph_envelope_seed_empty")

    radius, geometry = _observed_search_radius(seed)
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (radius * 2 + 1, radius * 2 + 1))
    search = cv2.dilate(seed, kernel, iterations=1)
    layer_union = fill | outline | antialias | shadow | glow | punctuation
    local_candidate = cv2.bitwise_and(layer_union, search)
    connected = _connected_to_seed(cv2.bitwise_or(local_candidate, seed), seed)
    # Protected edges that are graph-connected to confirmed text inside the
    # observed search area are not silently treated as art.  They are precisely
    # the unresolved case (outline versus structural line) that requires
    # additional evidence or human review.
    protected_neighborhood = cv2.bitwise_and(protected, search)
    protected_connected = _connected_to_seed(
        cv2.bitwise_or(protected_neighborhood, seed), seed)
    protected_connected = cv2.bitwise_and(
        protected_connected, cv2.bitwise_not(seed))

    evidence_count = np.zeros(shape, np.uint8)
    for layer in (fill, outline, antialias, shadow, glow, punctuation):
        evidence_count += (layer > 0).astype(np.uint8)
    known_seed = seed > 0
    supported = (connected > 0) & ((evidence_count >= 1) | known_seed)
    conflict = supported & (protected > 0) & ~known_seed
    confirmed = supported & ~conflict
    uncertain = conflict | (protected_connected > 0)

    def classified(layer: np.ndarray) -> np.ndarray:
        return ((layer > 0) & confirmed).astype(np.uint8) * 255

    fill_mask = classified(fill) | (known_seed.astype(np.uint8) * 255)
    inner_outline = classified(outline) & cv2.dilate(
        seed, np.ones((3, 3), np.uint8), iterations=1)
    outer_outline = classified(outline) & cv2.bitwise_not(inner_outline)
    antialias_mask = classified(antialias)
    shadow_mask = classified(shadow)
    glow_mask = classified(glow)
    punctuation_mask = classified(punctuation)
    transition = (
        confirmed
        & (evidence_count == 1)
        & ~(fill_mask > 0)
        & ~(inner_outline > 0)
        & ~(outer_outline > 0)
    ).astype(np.uint8) * 255
    complete = confirmed.astype(np.uint8) * 255
    unknown_count = int((uncertain > 0).sum())
    protected_overlap = int(((complete > 0) & (protected > 0)).sum())
    reason_codes: list[str] = []
    if unknown_count:
        reason_codes.append("glyph_art_evidence_conflict")
    if protected_overlap:
        reason_codes.append("glyph_envelope_protected_overlap")
    if np.any((layer_union > 0) & (search > 0) & ~(connected > 0)):
        reason_codes.append("unconnected_visual_layer_evidence")
    status = "confirmed" if not reason_codes else "blocked_pending_glyph_envelope_review"

    masks = {
        "fill_mask": fill_mask,
        "inner_outline_mask": inner_outline,
        "outer_outline_mask": outer_outline,
        "antialias_mask": antialias_mask,
        "shadow_mask": shadow_mask,
        "glow_mask": glow_mask,
        "punctuation_mask": punctuation_mask,
        "transition_mask": transition,
        "uncertain_mask": uncertain.astype(np.uint8) * 255,
        "complete_glyph_mask": complete,
        "protected_art_mask": protected,
        "search_area_mask": search,
    }
    hashes = {f"{name}_hash": _hash_mask(mask)
              for name, mask in masks.items()}
    identity_values = {
        str(key): str(value) for key, value in (identity or {}).items()}
    payload = {
        "schema_version": SCHEMA_VERSION,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "identity": identity_values,
        "source_hash": hashlib.sha256(image.tobytes()).hexdigest(),
        "text_seed_hash": _hash_mask(seed),
        "parameters": {
            "search_method": "observed_stroke_guided_local_connectivity",
            "classification_method": "layer_connectivity_protected_consensus",
            "search_radius": radius,
            **geometry,
        },
        "hashes": hashes,
        "protected_overlap": protected_overlap,
        "unknown_pixel_count": unknown_count,
        "complete_pixel_count": int((complete > 0).sum()),
        "reason_codes": reason_codes,
        "status": status,
    }
    return {
        **payload,
        "glyph_envelope_id": _canonical_hash(payload),
        **masks,
    }


def detect_retained_source_globally(
    source_bgr: np.ndarray,
    candidate_bgr: np.ndarray,
    *,
    change_mask: np.ndarray,
    glyph_envelope: np.ndarray,
    validation_halo: np.ndarray,
    fill_mask: np.ndarray | None = None,
    outline_mask: np.ndarray | None = None,
    antialias_mask: np.ndarray | None = None,
    shadow_mask: np.ndarray | None = None,
    punctuation_mask: np.ndarray | None = None,
    surrounding_source_evidence: np.ndarray | None = None,
) -> dict[str, Any]:
    """Find unchanged source glyph pixels across mask, envelope and halo."""
    source = np.asarray(source_bgr)
    candidate = np.asarray(candidate_bgr)
    if source.shape != candidate.shape or source.ndim != 3:
        raise ValueError("global_retained_source_shape_mismatch")
    shape = source.shape[:2]
    change = _binary(change_mask, shape) > 0
    envelope = _binary(glyph_envelope, shape) > 0
    halo = _binary(validation_halo, shape) > 0
    equal = np.all(source == candidate, axis=2)
    surrounding = (
        _binary(surrounding_source_evidence, shape) > 0
        if surrounding_source_evidence is not None else envelope)
    retained = equal & (envelope | (halo & surrounding))

    def layer_map(value: np.ndarray | None) -> np.ndarray:
        if value is None:
            return np.zeros(shape, np.uint8)
        return (retained & (_binary(value, shape) > 0)).astype(np.uint8) * 255

    inside_change = retained & change
    inside_envelope = retained & envelope
    outside_change = retained & envelope & ~change
    inside_halo = retained & halo & surrounding & ~envelope
    maps = {
        "retained_source_map": retained.astype(np.uint8) * 255,
        "retained_fill_map": layer_map(fill_mask),
        "retained_outline_map": layer_map(outline_mask),
        "retained_antialias_map": layer_map(antialias_mask),
        "retained_shadow_map": layer_map(shadow_mask),
        "retained_punctuation_map": layer_map(punctuation_mask),
    }
    counts = {
        "retained_source_inside_change_mask": int(inside_change.sum()),
        "retained_source_inside_visual_envelope": int(inside_envelope.sum()),
        "retained_source_outside_change_mask": int(outside_change.sum()),
        "retained_source_inside_halo": int(inside_halo.sum()),
        "retained_fill_pixels": int((maps["retained_fill_map"] > 0).sum()),
        "retained_outline_pixels": int((maps["retained_outline_map"] > 0).sum()),
        "retained_antialias_pixels": int((maps["retained_antialias_map"] > 0).sum()),
        "retained_shadow_pixels": int((maps["retained_shadow_map"] > 0).sum()),
        "retained_punctuation_pixels": int((maps["retained_punctuation_map"] > 0).sum()),
    }
    reasons = ([key for key, count in counts.items() if count]
               or ["no_retained_source_in_global_visual_scope"])
    return {
        "detector_version": "global-retained-source-v1",
        **counts,
        "evaluated_pixel_count": int((envelope | halo).sum()),
        "status": "blocked" if any(counts.values()) else "passed",
        "reason_codes": reasons,
        **maps,
    }
