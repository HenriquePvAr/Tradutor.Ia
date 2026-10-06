import json
from unittest.mock import patch

import yomu.runtime.job_runner as job_runner


def test_observability_client_requires_loopback_url():
    with patch.dict("os.environ", {}, clear=False):
        assert job_runner._observability_client({}) is None
        assert job_runner._observability_client({
            "gemini_safe_clean_remote_url": "https://example.test",
        }) is None


def test_analysis_event_uses_persisted_counts_and_marks_unknowns():
    fields = job_runner._analysis_event_fields({
        "logical_pages": 43,
        "candidate_count": 43,
        "source_analysis": {"accepted_page_count": 42, "story_groups": 8},
    })
    assert fields["pages"] == 42
    assert fields["story_groups"] == 8
    assert fields["sfx_groups"] == "UNKNOWN"


def test_terminal_observability_separates_eligible_from_running(monkeypatch):
    emitted = []

    class Client:
        def emit(self, name, **fields):
            emitted.append((name, fields))

    job_runner._emit_terminal_observability(
        Client(),
        {"id": "job", "run_id": "run", "status": "finished", "configuration": {}},
        None,
    )
    completed = next(fields for name, fields in emitted if name == "JOB_COMPLETED")
    dataset = next(fields for name, fields in emitted if name == "DATASET_SUMMARY")
    assert completed["training_eligible"] == "UNKNOWN"
    assert completed["training_running"] is False
    assert dataset["samples_eligible"] == "UNKNOWN"
    assert dataset["training_running"] is False


def test_review_terminal_event_names_unverified_source_pages(tmp_path):
    quality_path = tmp_path / "quality_report.json"
    quality_path.write_text(json.dumps({"summary": {"quality_validation": {
        "passed": False,
        "source_page_accounting": {
            "accounting_closed": True,
            "source_pages_expected": 183,
            "source_pages_analyzed": 181,
            "source_pages_completed_with_error": 2,
            "source_pages_missing": 0,
            "source_pages_unverified": 2,
            "source_pages_unverified_ids": ["p022", "p059"],
            "findings": [
                {"code": "page_analysis_error", "page_id": f"p{page:03}", "page": page,
                 "stage": "ocr", "status": "completed_with_error",
                 "error_code": "ocr_insufficient:zero_lines_on_story_like_page",
                 "story_content_verified": False, "quality": "review_required"}
                for page in (22, 59)
            ],
        },
        "physical_quality": {"physical_population": {
            "pages_total": 183, "pages_text_analyzed": 181,
            "pages_upstream_failed": 2, "pages_unresolved": 0,
        }},
        "zero_processing_errors": False,
    }}}), encoding="utf-8")
    emitted = []

    class Client:
        def emit(self, name, **fields):
            emitted.append((name, fields))

    job_runner._emit_terminal_observability(Client(), {
        "id": "job", "run_id": "run", "status": "review_required",
        "quality_report_path": str(quality_path), "progress_total": 183,
        "progress_current": 181,
    }, None)
    review = next(fields for name, fields in emitted if name == "REVIEW_REQUIRED")
    completed = next(fields for name, fields in emitted if name == "JOB_COMPLETED")
    assert review["pages"] == 183
    assert review["pages_verified"] == 181
    assert review["review_required_count"] == 2
    assert review["review_required_pages"] == "022,059"
    assert completed["status"] == "review_required"
