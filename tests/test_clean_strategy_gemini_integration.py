import numpy as np

from yomu.clean.orchestrator import (
    CleanRequest, INVALID_SOURCE, LOCAL_MASK_UNCERTAIN, LOCAL_RECONSTRUCTION_HARD,
    LOCAL_SAFE, SFX, SYSTEM_ERROR, run_clean_strategy,
)
from yomu.runtime.output_plan import normalize_output_plan


def _request():
    original = np.zeros((4, 4, 3), dtype=np.uint8)
    mask = np.zeros((4, 4), dtype=np.uint8)
    mask[1:3, 1:3] = 255
    return CleanRequest(original=original, authorized_mask=mask, mask_confidence=0.95,
                        job_id="job-1", page_id="1")


def _remote(**overrides):
    payload = {"status": "COMPLETED", "safe_clean_image": np.full((4, 4, 3), 200, dtype=np.uint8),
               "outside_mask_changed_pixels": 0, "seam_score": 0.1,
               "ocr_text_removal": {"status": "PASS", "residual_matches": []},
               "alignment_validation": {"status": "PASS"},
               "sfx_validation": {"status": "PASS"},
               "residual_validation": {"status": "PASS"}}
    payload.update(overrides)
    return lambda _request: payload


def test_local_safe_never_calls_gemini():
    calls = []
    result = run_clean_strategy(_request(), {"status": LOCAL_SAFE, "image": np.zeros((4, 4, 3), dtype=np.uint8)},
                                fallback=lambda request: calls.append(request))
    assert result["strategy_used"] == "local"
    assert result["fallback_invoked"] is False
    assert calls == []


def test_hard_local_invokes_gemini_and_composites_only_inside_mask():
    result = run_clean_strategy(_request(), {"status": LOCAL_RECONSTRUCTION_HARD}, fallback=_remote())
    assert result["strategy_used"] == "gemini"
    assert result["outside_mask_changed_pixels"] == 0
    assert np.all(result["image"][0, 0] == 0)
    assert np.all(result["image"][1, 1] == 200)


def test_mask_uncertain_blocks_gemini():
    request = _request()
    request = CleanRequest(original=request.original, authorized_mask=request.authorized_mask,
                           mask_confidence=0.2)
    result = run_clean_strategy(request, {"status": LOCAL_RECONSTRUCTION_HARD}, fallback=_remote())
    assert result["strategy_used"] == "none"
    assert result["local_status"] == LOCAL_MASK_UNCERTAIN
    assert result["fallback_invoked"] is False


def test_sfx_and_infrastructure_do_not_fallback():
    for status in (SFX, INVALID_SOURCE, SYSTEM_ERROR):
        calls = []
        result = run_clean_strategy(_request(), {"status": status}, fallback=lambda request: calls.append(request))
        assert result["fallback_invoked"] is False
        assert calls == []


def test_timeout_alignment_seam_and_outside_fail_closed():
    request = _request()
    timeout = run_clean_strategy(request, {"status": LOCAL_RECONSTRUCTION_HARD},
                                 fallback=lambda _: (_ for _ in ()).throw(TimeoutError()))
    assert timeout["status"] == "REVIEW_REQUIRED"
    assert timeout["fallback_status"] == "TimeoutError"
    bad_shape = run_clean_strategy(request, {"status": LOCAL_RECONSTRUCTION_HARD},
                                   fallback=_remote(safe_clean_image=np.zeros((3, 3, 3), dtype=np.uint8)))
    assert bad_shape["fallback_status"] == "ALIGNMENT_FAIL"
    seam = run_clean_strategy(request, {"status": LOCAL_RECONSTRUCTION_HARD}, fallback=_remote(seam_score=0.81))
    assert seam["fallback_status"] == "SEAM_FAIL"
    violating = np.full((4, 4, 3), 200, dtype=np.uint8)
    outside = run_clean_strategy(request, {"status": LOCAL_RECONSTRUCTION_HARD},
                                 fallback=_remote(raw_image=violating, outside_mask_changed_pixels=1))
    assert outside["fallback_status"] == "SAFETY_FAIL"


def test_remote_safe_composite_without_residual_text_validation_is_review():
    response = _remote(ocr_text_removal=None)
    result = run_clean_strategy(_request(), {"status": LOCAL_RECONSTRUCTION_HARD}, fallback=response)
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["fallback_status"] == "VALIDATION_EVIDENCE_MISSING"
    assert result["fallback_reason"] == "semantic_text_removal_unverified"


def test_output_plan_matrix_does_not_expose_provider():
    for preset in ("apenas_clean", "apenas_traducao", "traducao_clean", "traducao_pronta", "profissional"):
        plan = normalize_output_plan(preset)
        assert plan.executable is True
        assert "gemini" not in plan.to_config()
    translate_only = normalize_output_plan("apenas_traducao")
    assert translate_only.translate_only is True
    assert translate_only.has("clean") is False
