from types import SimpleNamespace

import cv2
import numpy as np
import pytest

import yomu.runtime.artifact_contract as artifact_contract
from yomu.pipeline import benchmark_pipeline as pipeline


def _page(tmp_path, page_id):
    source = np.full((20, 20, 3), 120, dtype=np.uint8)
    source[1:4, 1:4] = (10, 30, 50)
    source_path = tmp_path / f"source-{page_id}.png"
    output_path = tmp_path / f"render-{page_id}.png"
    assert cv2.imwrite(str(source_path), source)
    group = SimpleNamespace(
        group_id=f"group-{page_id}", region_id=f"region-{page_id}",
        classification="dialogue", box=(8, 8, 5, 5),
        canonical_source_text="owned story text", text="owned story text",
        quality_score=0.99, ocr_provenance_engines=("rapidocr",), source_engine="rapidocr",
        sent_to_translation=False, text_overflow_ratio=0.0,
        clean_disposition="LOCAL_SAFE", clean_disposition_reason="owned_text_removed_and_validated",
        clean_local_attempted=True,
        clean_artifact_validation={"clean_validation_passed": True},
        clean_remote_candidate=False, clean_remote_eligible=False,
        clean_remote_attempted=False, clean_remote_result="NOT_RUN",
        manual_review_required=False,
    )
    return {
        "index": page_id, "run_id": "run-clean-certificate", "qc_revision_id": "qc-1",
        "image_path": str(source_path), "output_path": str(output_path),
        "original_bgr": source, "groups": [group], "raw_lines": [], "candidates": [],
        "ocr_metadata": {"ocr_sufficiency": "OCR_SUFFICIENT"}, "source_language": "en",
    }


@pytest.mark.parametrize("workers", [1, 2])
def test_serial_and_parallel_workers_complete_clean_and_bind_canonical_file(
    tmp_path, monkeypatch, workers,
):
    pages = [_page(tmp_path, page_id) for page_id in (1, 2)]
    clean = []
    for state in pages:
        image = state["original_bgr"].copy()
        image[8:13, 8:13] = (200, 190, 180)
        clean.append(image)

    monkeypatch.setenv("PIPELINE_PAGE_WORKERS", str(workers))
    monkeypatch.setenv("YOMU_ENABLE_GEMINI_SAFE_FALLBACK", "0")
    monkeypatch.setattr(pipeline, "_plan_pipeline_flags", lambda: {
        "want_clean": True, "want_typeset": False,
    })
    monkeypatch.setattr(pipeline, "_save_page_processed_cache", lambda _state: None)

    def render(source, _lines, _candidates, _groups, *, capture, page_index, **_kwargs):
        capture["cleaned_bgr"] = clean[int(page_index) - 1]
        return source.copy(), {"items": [{
            "id": f"group-{page_index}", "region_id": f"region-{page_index}",
            "classification": "dialogue", "clean_text": "owned story text",
        }]}

    monkeypatch.setattr(pipeline, "render_analyzed_image", render)
    results = pipeline.process_post_translation_pages(pages, job_id="job-clean-cert")
    assert [result.page_index for result in results] == [1, 2]
    assert all(result.error is None for result in results), [result.error for result in results]

    states = [result.page_state for result in results]
    for state in states:
        assert state["clean_verified"] is True
        assert state["clean_certificate"]["schema_version"] == "yomu.clean-certificate.v1"
        assert state["clean_certificate"]["status"] == "PASS"
        assert state["clean_certificate"]["job_id"] == "job-clean-cert"
        assert cv2.imread(state["cleaned_path"], cv2.IMREAD_COLOR) is not None

    report = pipeline._persist_canonical_artifacts(
        tmp_path / "canonical", states, 2, artifact_contract.OutputProfile.PROFESSIONAL_QC,
        typeset=False,
    )
    assert report["clean_produced"] == [1, 2]
    assert pipeline._clean_output_accounting(states)["passed"] is True
    physical = pipeline._physical_residual_accounting(states, translation_applicable=False)
    assert physical["physical_regions_clean_verified"] == 2
    assert physical["physical_gate_passed"] is True
    for state in states:
        provenance = report["provenance"][state["index"]]
        assert provenance["clean_verified"] is True
        assert provenance["clean_certificate_validation"] == {"status": "PASS", "reason": "verified"}
        canonical = artifact_contract.artifact_path(
            tmp_path / "canonical", artifact_contract.ArtifactType.CLEAN_IMAGE,
            page=state["index"], total_pages=2,
        )
        assert state["clean_certificate"]["clean_sha256"] == pipeline.file_sha256(canonical)

    states[0]["qc_revision_id"] = "qc-2"
    stale_report = pipeline._persist_canonical_artifacts(
        tmp_path / "canonical", [states[0]], 2,
        artifact_contract.OutputProfile.PROFESSIONAL_QC, typeset=False,
    )
    stale = stale_report["provenance"][1]
    assert stale["clean_verified"] is False
    assert "REVIEW_CERTIFICATE:clean_certificate_revision_mismatch" in stale["review_reasons"]
    assert pipeline._clean_output_accounting([states[0]])["passed"] is False

    modified = states[1]
    persisted = cv2.imread(modified["cleaned_path"], cv2.IMREAD_COLOR)
    persisted[0, 0] = (0, 0, 0)
    assert cv2.imwrite(modified["cleaned_path"], persisted)
    modified_report = pipeline._persist_canonical_artifacts(
        tmp_path / "canonical", [modified], 2,
        artifact_contract.OutputProfile.PROFESSIONAL_QC, typeset=False,
    )
    modified_provenance = modified_report["provenance"][2]
    assert modified_provenance["clean_verified"] is False
    assert "REVIEW_CERTIFICATE:clean_certificate_clean_hash_mismatch" in modified_provenance["review_reasons"]
