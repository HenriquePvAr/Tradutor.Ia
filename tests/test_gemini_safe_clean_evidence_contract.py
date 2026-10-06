"""Offline contract tests for safe-clean.v2; no HTTP or provider is contacted."""
from __future__ import annotations

import json
import hashlib

import cv2
import numpy as np
import pytest

from yomu.clean.gemini_fallback.adapter import remote_clean_callback
from yomu.clean.gemini_fallback.authorized_mask import generate_authorized_mask_v2
from yomu.clean.orchestrator import CleanRequest, LOCAL_RECONSTRUCTION_HARD, run_clean_strategy
from yomu.ocr.ocr_balloon import TextGroup, get_cleanable_translatable_groups
from yomu.ocr.ocr_engine import OCRLine
from yomu.runtime.job_runner import _set_gemini_safe_clean_environment


class _FakeRemote:
    def __init__(self, *, corrupt_identity=False, ocr_text_removal="PASS", omit_alignment=False):
        self.fields = None
        self.corrupt_identity = corrupt_identity
        self.ocr_text_removal = ocr_text_removal
        self.omit_alignment = omit_alignment
        self.safe_bytes = cv2.imencode(".png", np.full((20, 20, 3), 127, dtype=np.uint8))[1].tobytes()
        self.raw_bytes = cv2.imencode(".png", np.full((20, 20, 3), 90, dtype=np.uint8))[1].tobytes()

    def submit(self, _image, **fields):
        self.fields = fields
        return {"job_id": fields["job_id"]}

    def wait(self, job_id):
        request_meta = json.loads(self.fields["metadata"])
        echo = dict(request_meta)
        if self.corrupt_identity:
            echo["request_id"] = "not-the-request"
        identity = {key: request_meta.get(key) for key in (
            "schema_version", "request_id", "parent_job_id", "run_id", "page_id",
            "region_ids", "source_sha256", "base_sha256", "mask_sha256")}
        semantic = None if self.ocr_text_removal is None else {
            "status": self.ocr_text_removal,
            "source_observations": [], "post_clean_observations": [],
        }
        echo["validation"] = {
            "identity": {**identity, "job_id": request_meta["request_id"]},
            "mask": {"mask_sha256": request_meta["mask_sha256"]},
            "outside_mask_changed_pixels": 0,
            "quality_status": "ACCEPTABLE",
            "final_sha256": hashlib.sha256(self.safe_bytes).hexdigest(),
            "raw_sha256": hashlib.sha256(self.raw_bytes).hexdigest(),
            "ocr_text_removal": semantic,
            "sfx_firewall": "PASS",
            "seam": {"status": "SEAM_PASS", "score": 0.1},
            "alignment": {"status": "PASS", "method": "NATIVE"},
            "alignment_validation": {"status": "PASS", "method": "NATIVE"},
            "sfx": {"status": "PASS"},
            "residual": {"status": "PASS"},
        }
        if self.omit_alignment:
            echo["validation"].pop("alignment")
        return {"job_id": job_id, "status": "COMPLETED", "source_sha256": request_meta["source_sha256"],
                "metadata": echo}

    def download_result(self, _job_id, destination):
        open(destination, "wb").write(self.safe_bytes)

    def download_raw_result(self, _job_id, destination):
        open(destination, "wb").write(self.raw_bytes)


def _request(tmp_path):
    source = np.zeros((20, 20, 3), dtype=np.uint8)
    mask = np.zeros((20, 20), dtype=np.uint8)
    mask[5:10, 5:10] = 255
    base = source.copy()
    base[1:3, 1:3] = 33
    return CleanRequest(original=source, base_image=base, authorized_mask=mask, mask_confidence=0.95,
                        job_id="parent-job", run_id="run-1", page_id="7", region_ids=("r-3",),
                        evidence_dir=str(tmp_path / "evidence"),
                        metadata={"disposition": "LOCAL_HARD", "ownership_confirmed": True,
                                  "local_clean_attempted": True,
                                  "local_validation": {"clean_validation_passed": False},
                                  "source_text": "WHY IS IT SO NOISY?", "source_text_confidence": .98,
                                  "source_text_provenance": "rapidocr_windows", "source_language": "eng",
                                  "mask_coverage": {"status": "PASS", "geometry_kind": "owned_components_plus_supported_light_outline",
                                                    "coverage_ratio": 1.0, "minimum_coverage_ratio": 0.98,
                                                    "region_id": "r-3", "owned_group_id": "g-3", "target_group_index": 0,
                                                    "groups": [{"status": "PASS", "coverage_ratio": 1.0, "lines": [
                                                        {"status": "PASS", "coverage_ratio": 1.0}]}]}})


def test_safe_clean_adapter_echoes_identity_and_persists_non_training_evidence(tmp_path):
    request = _request(tmp_path)
    client = _FakeRemote()
    result = remote_clean_callback(client, request)
    assert client.fields["metadata"] == json.dumps(json.loads(client.fields["metadata"]), sort_keys=True,
                                                   separators=(",", ":"))
    assert result["request_id"] == client.fields["job_id"]
    assert json.loads(client.fields["metadata"])["mask_coverage"]["coverage_ratio"] == 1.0
    assert result["raw_gemini_available"] is True
    evidence = next((tmp_path / "evidence").iterdir())
    manifest = json.loads((evidence / "evidence.json").read_text(encoding="utf-8"))
    assert manifest["training_eligible"] is False
    assert manifest["images"]["raw_gemini"] == "gemini-raw.png"
    assert (evidence / "gemini-raw.png").is_file()
    assert cv2.imread(str(evidence / "local_base.png"))[1, 1, 0] == 33


def test_safe_clean_adapter_rejects_mismatched_request_identity(tmp_path):
    with pytest.raises(ValueError, match="remote_validation_or_identity_evidence_missing"):
        remote_clean_callback(_FakeRemote(corrupt_identity=True), _request(tmp_path))


def test_safe_clean_adapter_rejects_output_without_semantic_text_removal_validation(tmp_path):
    with pytest.raises(ValueError, match="remote_validation_or_identity_evidence_missing"):
        remote_clean_callback(_FakeRemote(ocr_text_removal=None), _request(tmp_path))


def test_safe_clean_adapter_requires_both_alignment_contract_fields(tmp_path):
    with pytest.raises(ValueError, match="remote_validation_or_identity_evidence_missing"):
        remote_clean_callback(_FakeRemote(omit_alignment=True), _request(tmp_path))


def test_remote_result_is_composited_on_local_base_not_original(tmp_path):
    request = _request(tmp_path)
    response = remote_clean_callback(_FakeRemote(), request)
    result = run_clean_strategy(request, {"status": LOCAL_RECONSTRUCTION_HARD},
                                fallback=lambda _request: response)
    assert result["status"] == "COMPLETED"
    assert tuple(result["image"][1, 1]) == (33, 33, 33)
    assert tuple(result["image"][6, 6]) == (127, 127, 127)
    assert result["outside_mask_changed_pixels"] == 0


def test_target_group_mask_excludes_every_non_target(monkeypatch, tmp_path):
    image_path = tmp_path / "page.png"
    assert cv2.imwrite(str(image_path), np.zeros((50, 50, 3), dtype=np.uint8))

    class _Line:
        confidence = 0.98

    class _Group:
        def __init__(self, group_id, x):
            self.group_id = group_id
            self.region_id = group_id
            self.classification = "dialogue"
            self.lines = [_Line()]
            self.text = "hello"
            self.box = (x, 5, 8, 8)

    groups = [_Group("target", 5), _Group("other", 15)]
    monkeypatch.setattr("yomu.clean.gemini_fallback.authorized_mask.OCREngine",
                        lambda *_args, **_kwargs: type("OCR", (), {"detect_lines": lambda self, _img: []})())
    monkeypatch.setattr("yomu.clean.gemini_fallback.authorized_mask.yomu_ocr.analyze_image_array",
                        lambda _image, _lines: ([], groups))

    def roi(_shape, selected, *, padding):
        mask = np.zeros((50, 50), dtype=np.uint8)
        x, y, width, height = selected[0].box
        mask[y:y + height, x:x + width] = 255
        return mask

    monkeypatch.setattr("yomu.clean.gemini_fallback.authorized_mask.yomu_ocr._build_text_mask", roi)
    monkeypatch.setattr("yomu.clean.gemini_fallback.authorized_mask.yomu_ocr._component_text_mask",
                        lambda _image, _group, mask, strategy: (mask, {}))
    result = generate_authorized_mask_v2(image_path, tmp_path / "mask.png", target_group_id="target")
    assert result["status"] == "PASS"
    assert [region["text"] for region in result["regions"]] == ["hello"]
    assert result["blocked_regions"][0]["reason_codes"] == ["NON_TARGET_REGION_EXCLUDED"]


def test_clean_only_story_group_does_not_need_target_translation(monkeypatch):
    points = np.asarray([[10, 10], [80, 10], [80, 30], [10, 30]], dtype=np.int32)
    line = OCRLine(text="WHY IS IT SO NOISY?", confidence=0.95, polygon=points,
                   box=(10, 10, 70, 20), raw_text="WHY IS IT SO NOISY?", engine="rapidocr", page=1)
    group = TextGroup(group_id="dialogue-1", lines=[line], text=line.text, classification="narration",
                      inside_narration_box_like_region=True, source_engine="rapidocr")
    group.translation = ""
    group.translation_candidate = ""
    monkeypatch.setattr("yomu.ocr.ocr_balloon._group_has_story_translation_authority", lambda _group: True)
    assert get_cleanable_translatable_groups([group]) == [group]
    assert group.clean_disposition == "PENDING_LOCAL"


def test_gemini_clean_environment_is_default_off_and_requires_complete_attestation():
    env = {"YOMU_ENABLE_GEMINI_SAFE_FALLBACK": "1", "YOMU_GEMINI_REMOTE_URL": "http://stale"}
    _set_gemini_safe_clean_environment(env, {})
    assert env["YOMU_ENABLE_GEMINI_SAFE_FALLBACK"] == "0"
    assert env["YOMU_GEMINI_SAFE_CLEAN_SCHEMA_VERIFIED"] == "0"
    assert "YOMU_GEMINI_REMOTE_URL" not in env
    _set_gemini_safe_clean_environment(env, {
        "gemini_safe_clean_enabled": True,
        "gemini_safe_clean_schema": "safe-clean.v2",
        "gemini_safe_clean_worker_verified": True,
        "gemini_safe_clean_remote_url": "http://127.0.0.1:18777",
    })
    assert env["YOMU_ENABLE_GEMINI_SAFE_FALLBACK"] == "1"
    assert env["YOMU_GEMINI_SAFE_CLEAN_SCHEMA_VERIFIED"] == "1"
    assert env["YOMU_GEMINI_REMOTE_URL"] == "http://127.0.0.1:18777"
