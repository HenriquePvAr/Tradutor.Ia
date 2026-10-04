import json

import pytest

from yomu.clean.gemini_fallback.quality import (
    QUALITY_METADATA_VERSION,
    build_quality_metadata,
    evaluate_training_candidate,
)


def complete_metadata(**overrides):
    value = {
        "status": "COMPLETED", "transport_status": "PASS", "mask_status": "PASS",
        "alignment_status": "PASS", "outside_mask_changed_pixels": 0,
        "seam_status": "SEAM_PASS", "ocr_text_removal": "PASS",
        "sfx_preserved": "NOT_APPLICABLE", "non_target_text_preserved": "PASS",
        "visual_quality": "ACCEPTABLE",
        "visual_quality_provenance": "OWNER_DELEGATED_TO_CODEX_TECHNICAL_REVIEW",
    }
    value.update(overrides)
    return value


def test_all_explicit_gates_promote_candidate():
    result = evaluate_training_candidate(complete_metadata())
    assert result["training_candidate"] is True
    assert result["training_candidate_reason"] == "ALL_GATES_PASS"


@pytest.mark.parametrize("quality", ["EXCELLENT", "ACCEPTABLE"])
def test_accepted_quality_promotes(quality):
    assert evaluate_training_candidate(complete_metadata(visual_quality=quality))["training_candidate"]


@pytest.mark.parametrize("quality", ["QUESTIONABLE", "REJECT", "NOT_REVIEWED"])
def test_non_accepted_quality_blocks(quality):
    assert not evaluate_training_candidate(complete_metadata(visual_quality=quality))["training_candidate"]


@pytest.mark.parametrize("ocr", ["INCONCLUSIVE", "FAIL", "NOT_RUN"])
def test_non_pass_ocr_blocks(ocr):
    assert not evaluate_training_candidate(complete_metadata(ocr_text_removal=ocr))["training_candidate"]


@pytest.mark.parametrize("field", ["outside_mask_changed_pixels", "sfx_preserved", "non_target_text_preserved", "alignment_status", "seam_status"])
def test_safety_gate_failures_block(field):
    values = {"outside_mask_changed_pixels": 1, "sfx_preserved": "FAIL", "non_target_text_preserved": "FAIL",
              "alignment_status": "FAIL", "seam_status": "SEAM_REVIEW"}
    assert not evaluate_training_candidate(complete_metadata(**{field: values[field]}))["training_candidate"]


def test_missing_legacy_metadata_blocks_and_reasons_are_deterministic():
    first = evaluate_training_candidate({})
    second = evaluate_training_candidate({})
    assert first == second
    assert first["training_candidate"] is False
    assert "TRANSPORT_GATE_MISSING" in first["training_candidate_reason_codes"]
    assert "OCR_TEXT_REMOVAL_GATE_MISSING" in first["training_candidate_reason_codes"]


def test_quality_metadata_is_versioned_and_round_trips():
    metadata = build_quality_metadata(
        status="COMPLETED", transport_status="PASS", mask_status="PASS",
        alignment_status="PASS", outside_mask_changed_pixels=0, seam_status="SEAM_PASS",
        ocr_text_removal="PASS", sfx_preserved="NOT_APPLICABLE",
        non_target_text_preserved="PASS", visual_quality="ACCEPTABLE",
        visual_quality_provenance="OWNER_DELEGATED_TO_CODEX_TECHNICAL_REVIEW",
    )
    assert metadata["quality_metadata_version"] == QUALITY_METADATA_VERSION
    assert json.loads(json.dumps(metadata)) == metadata
    assert metadata["training_candidate"] is True
