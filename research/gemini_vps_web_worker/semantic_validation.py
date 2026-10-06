"""Region-scoped OCR evidence for source-text removal, fail-closed.

This is deliberately not a translation or image-quality oracle. It compares
the authorized target region before/after generation and only returns PASS
when the source is legible before generation and two independent preprocessing
views of the target contain no detected text. Any remaining non-source text,
OCR disagreement, or unavailable OCR is INCONCLUSIVE.
"""
from __future__ import annotations

import difflib
import re
import threading
import unicodedata
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np
from PIL import Image

_OCR = None
_OCR_LOCK = threading.Lock()
_STOP_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for",
    "from", "he", "her", "here", "hers", "him", "his", "i", "in",
    "into", "is", "it", "its", "me", "my", "of", "on", "or", "our",
    "she", "so", "that", "the", "their", "them", "there", "they",
    "this", "to", "was", "we", "were", "what", "when", "where", "who",
    "will", "with", "you", "your",
})


def _default_ocr(image: np.ndarray):
    global _OCR
    if _OCR is None:
        with _OCR_LOCK:
            if _OCR is None:
                from rapidocr_onnxruntime import RapidOCR
                _OCR = RapidOCR()
    return _OCR(image)


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).casefold()
    value = value.replace("’", "'").replace("`", "'")
    return " ".join(re.findall(r"[^\W_]+", value, flags=re.UNICODE))


def _content_tokens(text: str) -> list[str]:
    return [token for token in _normalize(text).split()
            if len(token) >= 3 and token not in _STOP_WORDS]


def _rapidocr_records(image: np.ndarray, engine: Callable | None) -> list[dict[str, Any]]:
    runner = engine or _default_ocr
    output = runner(image)
    records = output[0] if isinstance(output, tuple) and len(output) == 2 else output
    # RapidOCR represents a successful no-detection result as (None, None).
    # That means there are no text boxes; it is not an OCR execution failure.
    if isinstance(output, tuple) and len(output) == 2 and records is None:
        return []
    if records is None:
        raise ValueError("ocr_result_unavailable")
    result: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, (list, tuple)) or len(record) < 3:
            raise ValueError("ocr_result_malformed")
        text = str(record[1] or "").strip()
        score = float(record[2])
        if text:
            box = record[0]
            result.append({"text": text[:500], "confidence": round(score, 5),
                           "box": box.tolist() if hasattr(box, "tolist") else box})
    return result


def _roi_views(image_path: str | Path, mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    with Image.open(image_path) as image:
        rgb = np.asarray(image.convert("RGB"))
    if mask.shape != rgb.shape[:2]:
        raise ValueError("semantic_mask_shape_mismatch")
    active = mask >= 128
    ys, xs = np.where(active)
    if len(xs) == 0:
        raise ValueError("semantic_mask_empty")
    pad = max(4, int(round(min(rgb.shape[:2]) * 0.004)))
    x0, x1 = max(0, int(xs.min()) - pad), min(rgb.shape[1], int(xs.max()) + 1 + pad)
    y0, y1 = max(0, int(ys.min()) - pad), min(rgb.shape[0], int(ys.max()) + 1 + pad)
    crop = rgb[y0:y1, x0:x1].copy()
    if crop.size == 0 or min(crop.shape[:2]) < 12:
        raise ValueError("semantic_target_too_small")
    # The mask selects the ROI only. Applying its glyph-shaped pixels to the
    # image would isolate the old letter silhouettes and make clean pixels look
    # like source text to OCR.
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    scale = max(2, min(4, 180 // max(1, min(gray.shape))))
    enlarged = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    enhanced = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(enlarged)
    views = [cv2.cvtColor(crop, cv2.COLOR_RGB2BGR), cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)]
    return views[0], views[1]


def _text_for(records: list[dict[str, Any]]) -> str:
    return " ".join(record["text"] for record in records).strip()


def _similarity(left: str, right: str) -> float:
    return difflib.SequenceMatcher(None, _normalize(left), _normalize(right)).ratio()


def _matching_source_tokens(source: str, post: str) -> list[str]:
    source_tokens = list(dict.fromkeys(_content_tokens(source)))
    post_tokens = _content_tokens(post)
    matches = []
    for token in source_tokens:
        for candidate in post_tokens:
            if token == candidate:
                matches.append(token)
                break
            if min(len(token), len(candidate)) >= 5 and difflib.SequenceMatcher(None, token, candidate).ratio() >= 0.86:
                matches.append(token)
                break
    return matches


def _target_visual_delta(source_path: str | Path, final_path: str | Path, mask: np.ndarray) -> dict[str, Any]:
    with Image.open(source_path) as source_image, Image.open(final_path) as final_image:
        source = np.asarray(source_image.convert("RGB"), dtype=np.int16)
        final = np.asarray(final_image.convert("RGB"), dtype=np.int16)
    active = mask >= 128
    if source.shape != final.shape or active.shape != source.shape[:2] or not active.any():
        return {"status": "INCONCLUSIVE", "reason": "target_visual_geometry_invalid"}
    delta = np.max(np.abs(source - final), axis=2) > 2
    changed = int(np.count_nonzero(delta & active))
    area = int(np.count_nonzero(active))
    minimum = max(8, int(np.ceil(area * 0.001)))
    return {"status": "PASS" if changed >= minimum else "INCONCLUSIVE",
            "changed_pixels_inside_target": changed, "target_mask_pixels": area,
            "minimum_change_pixels": minimum,
            "changed_ratio": round(changed / max(1, area), 6),
            "reason": "target_pixels_changed" if changed >= minimum else "target_visual_change_unproven"}


def validate_text_removal(
    source_path: str | Path,
    final_path: str | Path,
    mask: np.ndarray,
    metadata: dict[str, Any] | None = None,
    *,
    ocr_engine: Callable | None = None,
) -> dict[str, Any]:
    metadata = metadata if isinstance(metadata, dict) else {}
    source_text = str(metadata.get("source_text") or "").strip()
    source_confidence = metadata.get("source_text_confidence")
    source_provenance = str(metadata.get("source_text_provenance") or "").strip()
    source_language = str(metadata.get("source_language") or "").strip() or None
    base = {
        "source_text": source_text[:1000] or None,
        "source_language": source_language,
        "source_text_provenance": source_provenance or None,
        "source_observations": [],
        "post_clean_observations": [],
        "residual_matches": [],
        "normalized_source": None,
        "normalized_post": None,
        "similarity": None,
        "confidence": None,
        "visual_residual_detector": "rapidocr_region_text_boxes_v1",
    }
    try:
        if not source_language:
            return {**base, "status": "INCONCLUSIVE", "reason": "source_language_provenance_missing"}
        source_views = _roi_views(source_path, mask)
        post_views = _roi_views(final_path, mask)
        source_runs = [_rapidocr_records(view, ocr_engine) for view in source_views]
        post_runs = [_rapidocr_records(view, ocr_engine) for view in post_views]
        visual_delta = _target_visual_delta(source_path, final_path, mask)
    except Exception as exc:
        return {**base, "status": "INCONCLUSIVE", "reason": f"ocr_unavailable_or_invalid:{type(exc).__name__}"}

    source_observations = [
        {"view": f"source_view_{index + 1}", "engine": "rapidocr_onnxruntime",
         "text": _text_for(records), "records": records}
        for index, records in enumerate(source_runs)
    ]
    post_observations = [
        {"view": f"post_clean_view_{index + 1}", "engine": "rapidocr_onnxruntime",
         "text": _text_for(records), "records": records}
        for index, records in enumerate(post_runs)
    ]
    vps_source_text = max((_text_for(records) for records in source_runs), key=len, default="")
    client_tokens = _content_tokens(source_text)
    vps_tokens = _content_tokens(vps_source_text)
    if source_text and vps_source_text and _similarity(source_text, vps_source_text) < 0.55:
        return {**base, "status": "INCONCLUSIVE", "reason": "source_baseline_disagreement",
                "source_observations": source_observations, "post_clean_observations": post_observations}
    if source_text and len(client_tokens) >= 1 and source_provenance and isinstance(source_confidence, (int, float)) and not isinstance(source_confidence, bool) and source_confidence >= 0.60:
        baseline = source_text
        baseline_provenance = source_provenance
        baseline_confidence = float(source_confidence)
    elif vps_source_text and len(vps_tokens) >= 1:
        best_records = max(source_runs, key=lambda records: max((r["confidence"] for r in records), default=0.0))
        best_confidence = max((r["confidence"] for r in best_records), default=0.0)
        if best_confidence < 0.60:
            return {**base, "status": "INCONCLUSIVE", "reason": "source_ocr_confidence_low",
                    "source_observations": source_observations, "post_clean_observations": post_observations}
        baseline = vps_source_text
        baseline_provenance = "rapidocr_onnxruntime_vps_source"
        baseline_confidence = best_confidence
    else:
        return {**base, "status": "INCONCLUSIVE", "reason": "source_text_baseline_missing",
                "source_observations": source_observations, "post_clean_observations": post_observations}

    post_texts = [_text_for(records) for records in post_runs]
    joined_post = " ".join(text for text in post_texts if text)
    matches = sorted(set(_matching_source_tokens(baseline, joined_post)))
    similarity = max((_similarity(baseline, text) for text in post_texts if text), default=0.0)
    confidence = round(min(1.0, baseline_confidence * (1.0 if all(isinstance(r, list) for r in post_runs) else 0.0)), 5)
    semantic = {
        **base,
        "source_text": baseline[:1000],
        "source_text_provenance": baseline_provenance,
        "source_observations": source_observations,
        "post_clean_observations": post_observations,
        "normalized_source": _normalize(baseline),
        "normalized_post": _normalize(joined_post),
        "similarity": round(similarity, 5),
        "residual_matches": matches,
        "confidence": confidence,
        "visual_residual_detector": {
            "name": "rapidocr_onnxruntime_region_text_boxes_v1",
            "views": len(post_runs),
            "post_text_boxes": sum(len(records) for records in post_runs),
            "status": "NO_TEXT_BOXES" if not any(post_runs) else "TEXT_BOXES_PRESENT",
        },
        "visual_delta": visual_delta,
    }
    if matches or similarity >= 0.35:
        return {**semantic, "status": "FAIL", "reason": "source_text_residual_detected"}
    if any(post_runs):
        return {**semantic, "status": "INCONCLUSIVE", "reason": "unmatched_text_in_authorized_target"}
    if visual_delta.get("status") != "PASS":
        return {**semantic, "status": "INCONCLUSIVE", "reason": "visual_target_change_not_proven"}
    return {**semantic, "status": "PASS", "reason": "source_baseline_present_and_two_target_views_have_no_text_boxes"}
