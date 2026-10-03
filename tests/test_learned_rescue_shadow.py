from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import json

import numpy as np

from learned_rescue import CANDIDATE_ID, LearnedRescueService, ShadowRequest


def _request(review_required=True):
    pre = np.full((12, 14, 3), 120, dtype=np.uint8)
    pre[4:8, 5:10] = 40
    mask = np.zeros((12, 14), dtype=np.uint8)
    mask[4:8, 5:10] = 255
    baseline = pre.copy()
    return ShadowRequest("region-1", pre, mask, baseline, review_required)


def _valid_service(tmp_path, enabled=True, mode=None):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"test-model")
    return LearnedRescueService(
        enabled=enabled,
        model_path=model,
        adapter_hash="adapter-v3",
        h1_spec_hash="h1-v1",
        model_validator=lambda path: None,
        mode=mode,
    )


def _runtime(pre, mask):
    result = pre.copy()
    result[mask > 0] = 80
    return result


def test_feature_off_does_not_load_model_or_run_candidate(tmp_path):
    called = []
    service = LearnedRescueService(enabled=False, model_path=tmp_path / "missing.onnx")
    result = service.request_shadow(_request(), runtime_factory=lambda *_: called.append(1))
    assert result.status == "SHADOW_UNAVAILABLE"
    assert result.failure_reason == "FEATURE_DISABLED"
    assert called == []


def test_manual_request_requires_existing_review_context(tmp_path):
    service = _valid_service(tmp_path)
    result = service.request_shadow(_request(False), runtime_factory=_runtime, guard=lambda *_: {"status": "PASS"})
    assert result.failure_reason == "REVIEW_CONTEXT_REQUIRED"


def test_model_absence_is_structured_and_fail_closed(tmp_path):
    service = LearnedRescueService(enabled=True, model_path=tmp_path / "missing.onnx")
    result = service.request_shadow(_request(), runtime_factory=_runtime, guard=lambda *_: {"status": "PASS"})
    assert result.failure_reason == "MODEL_UNAVAILABLE"
    assert result.candidate_available is False


def test_model_hash_is_checked_before_runtime(tmp_path):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"wrong")
    called = []
    service = LearnedRescueService(enabled=True, model_path=model)
    result = service.request_shadow(_request(), runtime_factory=lambda *_: called.append(1), guard=lambda *_: {"status": "PASS"})
    assert result.failure_reason == "MODEL_HASH_MISMATCH"
    assert called == []


def test_success_is_shadow_only_and_bundle_binds_identity(tmp_path):
    request = _request()
    baseline = request.baseline.copy()
    service = _valid_service(tmp_path)
    result = service.request_shadow(
        request,
        runtime_factory=_runtime,
        guard=lambda candidate, mask: {"status": "PASS", "detections": []},
        bundle_root=tmp_path / "bundle",
    )
    assert result.status == "REVIEW_PENDING"
    assert result.candidate_identity == CANDIDATE_ID
    assert result.guard_status == "PASS"
    assert np.array_equal(request.baseline, baseline)
    bundle = json.loads((tmp_path / "bundle" / "review_bundle.json").read_text())
    assert bundle["candidate_status"] == "GUARD_PASS"
    assert bundle["review_decision"] is None
    assert bundle["warnings"] == ["NON_TEXT_HALLUCINATION_NOT_COVERED"]


def test_h1_reject_keeps_candidate_non_authoritative(tmp_path):
    service = _valid_service(tmp_path)
    result = service.request_shadow(
        _request(), runtime_factory=_runtime,
        guard=lambda *_: {"status": "REJECT", "detections": [{"text": "x", "overlap_pixels": 2}]},
        bundle_root=tmp_path / "bundle",
    )
    assert result.status == "REVIEW_PENDING"
    assert result.guard_status == "REJECT"
    assert result.candidate_available is True
    assert json.loads((tmp_path / "bundle" / "review_bundle.json").read_text())["candidate_status"] == "GUARD_REJECT"


def test_guard_error_fails_closed(tmp_path):
    service = _valid_service(tmp_path)
    result = service.request_shadow(_request(), runtime_factory=_runtime, guard=lambda *_: (_ for _ in ()).throw(RuntimeError("ocr")))
    assert result.status == "GUARD_ERROR"
    assert result.failure_reason == "GUARD_ERROR"


def test_outside_mask_violation_is_not_reviewable(tmp_path):
    service = _valid_service(tmp_path)
    def bad_runtime(pre, mask):
        out = _runtime(pre, mask)
        out[0, 0] = 0
        return out
    result = service.request_shadow(_request(), runtime_factory=bad_runtime, guard=lambda *_: {"status": "PASS"})
    assert result.failure_reason == "OUTSIDE_MASK_INVARIANT_FAILED"
    assert result.candidate_available is False


def test_manual_accept_stages_and_promotes_only_after_downstream_pass(tmp_path):
    request = _request()
    service = _valid_service(tmp_path, enabled=True)
    bundle = tmp_path / "bundle"
    result = service.request_shadow(request, runtime_factory=_runtime, guard=lambda *_: {"status": "PASS"}, bundle_root=bundle)
    target = tmp_path / "authoritative.npy"
    applied = service.apply_human_decision(request, candidate_path=result.candidate_path, decision="HUMAN_ACCEPT_C8", downstream_validator=lambda _: True, authoritative_path=target, bundle_path=result.bundle_path)
    assert applied["status"] == "REVIEW_ACCEPT_C8"
    assert np.array_equal(np.load(target)[request.mask > 0], _runtime(request.pre_cleanup, request.mask)[request.mask > 0])
    assert np.array_equal(request.baseline, _request().baseline)


def test_manual_reject_or_abandonment_never_changes_artifact(tmp_path):
    request = _request(); service = _valid_service(tmp_path)
    bundle = tmp_path / "bundle"
    result = service.request_shadow(request, runtime_factory=_runtime, guard=lambda *_: {"status": "PASS"}, bundle_root=bundle)
    target = tmp_path / "authoritative.npy"
    assert service.apply_human_decision(request, candidate_path=result.candidate_path, decision="HUMAN_ACCEPT_BASELINE", downstream_validator=lambda _: True, authoritative_path=target)["authoritative_source"] == "BASELINE"
    unresolved = service.apply_human_decision(request, candidate_path=result.candidate_path, decision="HUMAN_KEEP_UNRESOLVED", downstream_validator=lambda _: True, authoritative_path=target)
    assert unresolved["authoritative_source"] == "BASELINE"
    assert not target.exists()


def test_h1_reject_and_downstream_failure_block_manual_accept(tmp_path):
    request = _request(); service = _valid_service(tmp_path); bundle = tmp_path / "bundle"
    rejected = service.request_shadow(request, runtime_factory=_runtime, guard=lambda *_: {"status": "REJECT"}, bundle_root=bundle)
    blocked = service.apply_human_decision(request, candidate_path=rejected.candidate_path, decision="HUMAN_ACCEPT_C8", downstream_validator=lambda _: True, authoritative_path=tmp_path / "out.npy", bundle_path=rejected.bundle_path)
    assert blocked["failure_reason"] == "CANDIDATE_IDENTITY_MISMATCH"
    bundle2 = tmp_path / "bundle2"
    passed = service.request_shadow(request, runtime_factory=_runtime, guard=lambda *_: {"status": "PASS"}, bundle_root=bundle2)
    failed = service.apply_human_decision(request, candidate_path=passed.candidate_path, decision="HUMAN_ACCEPT_C8", downstream_validator=lambda _: False, authoritative_path=tmp_path / "out2.npy", bundle_path=passed.bundle_path)
    assert failed["status"] == "APPLICATION_VALIDATION_FAILED"
    assert not (tmp_path / "out2.npy").exists()


def test_auto_generates_shadow_but_unqualified_policy_stops_at_review(tmp_path):
    request = _request(); service = _valid_service(tmp_path, mode="auto"); bundle = tmp_path / "auto"
    result = service.request_auto_shadow(request, runtime_factory=_runtime, guard=lambda *_: {"status": "PASS"}, bundle_root=bundle)
    assert result.request_origin == "AUTO"
    assert result.status == "REVIEW_PENDING"
    assert result.failure_reason == "AUTO_ACCEPT_POLICY_NOT_QUALIFIED"
    assert result.guard_evidence["auto_policy_status"] == "REVIEW"


def test_auto_mode_does_not_allow_user_to_qualify_acceptance(tmp_path):
    request = _request(); service = _valid_service(tmp_path, mode="auto")
    assert service.auto_policy.evaluate(None).status == "REVIEW"
    assert service.request_auto_shadow(request, runtime_factory=_runtime, guard=lambda *_: {"status": "REJECT"}).failure_reason == "AUTO_ACCEPT_POLICY_NOT_QUALIFIED"


def test_stale_bound_inputs_block_manual_acceptance(tmp_path):
    request = _request(); service = _valid_service(tmp_path); bundle = tmp_path / "bundle"
    result = service.request_shadow(request, runtime_factory=_runtime, guard=lambda *_: {"status": "PASS"}, bundle_root=bundle)
    candidate_path = result.candidate_path
    variants = []
    for field in ("pre_cleanup", "mask", "baseline"):
        values = {"case_region_id": request.case_region_id, "pre_cleanup": request.pre_cleanup.copy(), "mask": request.mask.copy(), "baseline": request.baseline.copy(), "review_required": True}
        values[field].flat[0] ^= 1
        variants.append(ShadowRequest(**values))
    for stale in variants:
        result = service.apply_human_decision(stale, candidate_path=candidate_path, decision="HUMAN_ACCEPT_C8", downstream_validator=lambda _: True, authoritative_path=tmp_path / (field + ".npy"), bundle_path=bundle / "review_bundle.json")
        assert result["failure_reason"] in {"CANDIDATE_IDENTITY_MISMATCH", "OUTSIDE_MASK_INVARIANT_FAILED"}
