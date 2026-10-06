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
