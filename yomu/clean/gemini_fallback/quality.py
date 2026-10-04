"""Versioned quality metadata and fail-closed training-candidate policy."""
from __future__ import annotations

from typing import Any

QUALITY_METADATA_VERSION = "SAFE_CLEAN_QUALITY_V1"
MASK_GENERATOR_VERSION = "AUTHORIZED_MASK_GENERATOR_V2"
OCR_REMOVAL_STATUSES = {"PASS", "FAIL", "INCONCLUSIVE", "NOT_APPLICABLE", "NOT_RUN"}
VISUAL_QUALITY_VALUES = {"EXCELLENT", "ACCEPTABLE", "QUESTIONABLE", "REJECT", "NOT_REVIEWED"}


def evaluate_training_candidate(metadata: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic promotion decision; missing data never promotes."""
    metadata = metadata if isinstance(metadata, dict) else {}
    validation = metadata.get("validation") if isinstance(metadata.get("validation"), dict) else metadata
    seam = validation.get("seam") if isinstance(validation.get("seam"), dict) else {}
    checks = {
        "completed": str(metadata.get("status") or "COMPLETED").upper() == "COMPLETED",
        "transport": str(metadata.get("transport_status") or "").upper() == "PASS",
        "mask": str(metadata.get("mask_status") or "").upper() == "PASS",
        "alignment": str(metadata.get("alignment_status") or validation.get("alignment") or "").upper() in {"PASS", "NATIVE", "NOMINAL_RESIZE_REVIEWED"},
        "outside_mask": validation.get("outside_mask_changed_pixels", metadata.get("outside_mask_changed_pixels")) == 0,
        "seam": str(metadata.get("seam_status") or seam.get("status") or "").upper() == "SEAM_PASS",
        "ocr_text_removal": str(metadata.get("ocr_text_removal") or "").upper() == "PASS",
        "sfx": str(metadata.get("sfx_preserved") or "").upper() in {"PASS", "NOT_APPLICABLE"},
        "non_target": str(metadata.get("non_target_text_preserved") or "").upper() in {"PASS", "NOT_APPLICABLE"},
        "visual_quality": str(metadata.get("visual_quality") or "").upper() in {"ACCEPTABLE", "EXCELLENT"},
        "review_provenance": str(metadata.get("visual_quality_provenance") or "").upper() == "OWNER_DELEGATED_TO_CODEX_TECHNICAL_REVIEW",
    }
    reasons = [name.upper() + "_GATE_MISSING" for name, passed in checks.items() if not passed]
    candidate = not reasons
    return {"quality_metadata_version": QUALITY_METADATA_VERSION,
            "training_candidate": candidate,
            "training_candidate_reason": "ALL_GATES_PASS" if candidate else reasons[0],
            "training_candidate_reason_codes": reasons,
            "checks": checks}


def build_quality_metadata(*, status: str, transport_status: str, mask_status: str,
                           mask_generator_version: str = MASK_GENERATOR_VERSION,
                           mask_confidence: float | None = None,
                           alignment_status: str = "NOT_RUN", alignment_score: float | None = None,
                           scene_recreation_detected: bool | None = None,
                           outside_mask_changed_pixels: int | None = None,
                           seam_status: str = "NOT_RUN", seam_score: float | None = None,
                           ocr_text_removal: str = "NOT_RUN", ocr_before: list[str] | None = None,
                           ocr_after: list[str] | None = None, sfx_preserved: str = "NOT_APPLICABLE",
                           non_target_text_preserved: str = "NOT_APPLICABLE",
                           visual_quality: str = "NOT_REVIEWED",
                           visual_quality_provenance: str = "") -> dict[str, Any]:
    if ocr_text_removal not in OCR_REMOVAL_STATUSES:
        raise ValueError("ocr_text_removal_invalid")
    if visual_quality not in VISUAL_QUALITY_VALUES:
        raise ValueError("visual_quality_invalid")
    metadata = {"quality_metadata_version": QUALITY_METADATA_VERSION, "status": status,
                "transport_status": transport_status, "mask_status": mask_status,
                "mask_generator_version": mask_generator_version, "mask_confidence": mask_confidence,
                "alignment_status": alignment_status, "alignment_score": alignment_score,
                "scene_recreation_detected": scene_recreation_detected,
                "outside_mask_changed_pixels": outside_mask_changed_pixels,
                "seam_status": seam_status, "seam_score": seam_score,
                "ocr_text_removal": ocr_text_removal, "ocr_before": ocr_before or [], "ocr_after": ocr_after or [],
                "sfx_preserved": sfx_preserved, "non_target_text_preserved": non_target_text_preserved,
                "visual_quality": visual_quality, "visual_quality_provenance": visual_quality_provenance}
    metadata.update(evaluate_training_candidate(metadata))
    return metadata
