"""Offline classifier for explicitly supplied OCR/pipeline diagnostic artifacts.

This module does not run OCR, contact a provider, inspect a job store, or read
user output implicitly. Inputs are JSON snapshots supplied explicitly by a
developer/tester; output is stable JSON suitable for local regression triage.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


FAILURE_TAXONOMY = (
    "DETECTION_OR_OCR_MISS",
    "OCR_EMPTY",
    "OCR_LOW_CONFIDENCE_FILTER",
    "FILTERED_PRE_GROUP",
    "GROUPING_DROP",
    "CLASSIFIER_IGNORED",
    "WEAK_UNKNOWN_REJECTED",
    "SFX_PRESERVED",
    "NOT_TRANSLATION_ELIGIBLE",
    "TRANSLATION_EMPTY_OR_FAILED",
    "RENDER_EXCLUDED",
    "RENDER_FAILED",
    "OUTPUT_PRESENT",
    "UNKNOWN_BOUNDARY",
)


def _box_key(item: dict[str, Any]) -> tuple[float, float]:
    box = item.get("bbox")
    if isinstance(box, (list, tuple)) and len(box) >= 2:
        try:
            return float(box[1]), float(box[0])
        except (TypeError, ValueError):
            pass
    return float("inf"), float("inf")


def classify_item(item: dict[str, Any]) -> str:
    """Return the earliest evidenced terminal boundary for one region/line."""
    if item.get("output_present") is True or item.get("rendered") is True:
        return "OUTPUT_PRESENT"
    if item.get("render_failed") is True:
        return "RENDER_FAILED"
    if item.get("render_excluded") is True:
        return "RENDER_EXCLUDED"
    if item.get("translation_failed") is True or (
        item.get("translation_eligible") is True
        and item.get("translation_result") in (None, "")
    ):
        return "TRANSLATION_EMPTY_OR_FAILED"
    if item.get("sfx_preserved") is True or str(item.get("classification", "")).lower() == "sfx":
        return "SFX_PRESERVED"
    if item.get("translation_eligible") is False:
        return "NOT_TRANSLATION_ELIGIBLE"
    if item.get("ignored_reason") == "weak_unknown_text":
        return "WEAK_UNKNOWN_REJECTED"
    if item.get("filtered_pre_group") is True:
        return "OCR_LOW_CONFIDENCE_FILTER" if item.get("low_confidence") else "FILTERED_PRE_GROUP"
    if item.get("grouping_dropped") is True:
        return "GROUPING_DROP"
    if item.get("classifier_ignored") is True or item.get("ignored_reason"):
        return "CLASSIFIER_IGNORED"
    if item.get("ocr_empty") is True:
        return "OCR_EMPTY"
    if item.get("raw_line_present") is False:
        return "DETECTION_OR_OCR_MISS"
    if item.get("boundary") in FAILURE_TAXONOMY:
        return str(item["boundary"])
    return "UNKNOWN_BOUNDARY"


def normalize_artifact(payload: dict[str, Any]) -> dict[str, Any]:
    """Create deterministic boundary records without inventing missing data."""
    records = payload.get("regions", payload.get("items", [])) or []
    normalized = []
    for raw in records:
        item = dict(raw)
        item["failure_boundary"] = classify_item(item)
        normalized.append(item)
    normalized.sort(key=lambda item: (
        int(item.get("page_index", 0) or 0),
        *_box_key(item),
        str(item.get("region_id") or ""),
        str(item.get("raw_text") or item.get("normalized_text") or ""),
    ))
    result = {
        "schema_version": 1,
        "case_id": str(payload.get("case_id") or "unspecified"),
        "evidence_kind": str(payload.get("evidence_kind") or "PIPELINE_ARTIFACT"),
        "page_index": payload.get("page_index"),
        "regions": normalized,
        "missing_expected_text": [],
    }
    expected = payload.get("expected", []) or []
    observed_text = {
        str(item.get("normalized_text") or item.get("raw_text") or "").strip().casefold()
        for item in normalized
    }
    for expectation in expected:
        text = str(expectation.get("text") or "").strip()
        if text and text.casefold() not in observed_text:
            result["missing_expected_text"].append({
                "text": text,
                "failure_boundary": "DETECTION_OR_OCR_MISS",
                "evidence_note": "No matching raw OCR record was supplied; detector vs OCR is unresolved.",
            })
    result["missing_expected_text"].sort(key=lambda item: item["text"].casefold())
    return result


def progress_page_snapshot(payload: dict[str, Any], page_index: int) -> dict[str, Any]:
    """Adapt one explicitly supplied legacy progress page without opening a job store."""
    page = next((row for row in payload.get("pages", [])
                 if int(row.get("index", -1)) == int(page_index)), None)
    if page is None:
        raise ValueError(f"page_index_not_found:{page_index}")
    debug = page.get("debug_data") or {}
    regions = []
    for source in debug.get("items", []) or []:
        item = {
            "page_index": page.get("index"),
            "region_id": source.get("region_id", source.get("id")),
            "raw_text": source.get("raw_text"),
            "normalized_text": source.get("clean_text"),
            "confidence": source.get("confidence"),
            "bbox": source.get("bounding_box"),
            "group_id": source.get("group_id"),
            "classification": source.get("classification"),
            "classification_reason": source.get("classification_reason"),
            "sent_to_translation": source.get("sent_to_translation"),
            "translation_result": source.get("translation"),
            "ignored_reason": source.get("ignore_reason"),
        }
        if source.get("classification_reason") == "line_ignored_before_grouping":
            item["filtered_pre_group"] = True
        if "sent_to_translation" in source:
            item["translation_eligible"] = bool(source.get("sent_to_translation"))
        if source.get("translation_failed") is True:
            item["translation_failed"] = True
        if source.get("rendered") is not None:
            item["rendered"] = bool(source.get("rendered"))
        if source.get("output_present") is not None:
            item["output_present"] = bool(source.get("output_present"))
        item = {key: value for key, value in item.items() if value is not None}
        regions.append(item)
    return normalize_artifact({
        "case_id": f"progress_page_{int(page_index)}",
        "evidence_kind": "EXPLICIT_LOCAL_PROGRESS_ARTIFACT",
        "page_index": page.get("index"),
        "regions": regions,
    })


def review_to_expectation(payload: dict[str, Any], *, case_id: str) -> dict[str, Any]:
    """Convert an explicitly supplied local Review snapshot to expectations."""
    regions = []
    for row in payload.get("regions", payload.get("items", [])) or []:
        regions.append({
            "page_index": row.get("page_index"),
            "region_id": row.get("region_id"),
            "source_text_original": row.get("source_text_original"),
            "source_text_override": row.get("source_text_override"),
            "target_text_override": row.get("target_text_override"),
            "region_type_override": row.get("region_type_override"),
            "bbox_override": row.get("bbox_override"),
            "translate_override": row.get("translate_override"),
            "is_manual_region": bool(row.get("is_manual_region", False)),
        })
    regions.sort(key=lambda item: (
        int(item.get("page_index") or 0), str(item.get("region_id") or "")
    ))
    return {"schema_version": 1, "case_id": case_id, "source": "EXPLICIT_LOCAL_REVIEW_SNAPSHOT", "regions": regions}


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON root must be an object")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path, help="Explicit JSON diagnostic snapshot")
    parser.add_argument("--output", type=Path, help="Write JSON here; defaults to stdout")
    parser.add_argument("--review-to-expectation", action="store_true",
                        help="Convert the explicitly supplied local Review JSON instead of classifying it")
    parser.add_argument("--progress-page", type=int,
                        help="Adapt exactly this page index from an explicitly supplied progress.json")
    parser.add_argument("--case-id", default="review_snapshot")
    args = parser.parse_args(argv)
    payload = _read_json(args.artifact)
    if args.review_to_expectation:
        result = review_to_expectation(payload, case_id=args.case_id)
    elif args.progress_page is not None:
        result = progress_page_snapshot(payload, args.progress_page)
    else:
        result = normalize_artifact(payload)
    text = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8", newline="\n")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
