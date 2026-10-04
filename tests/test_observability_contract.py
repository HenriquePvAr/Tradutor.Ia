from yomu.observability.events import SCHEMA_VERSION, build_event, redact_event


def test_event_contract_redacts_secrets_and_text():
    event = build_event("JOB_COMPLETED", job_id="job-1", run_id="run-1", prompt="do not send", webhook="secret", duration_ms=4)
    assert event["schema_version"] == SCHEMA_VERSION
    assert event["event_name"] == "JOB_COMPLETED"
    assert "prompt" not in event and "webhook" not in event
    assert event["duration_ms"] == 4


def test_unsafe_identity_is_hashed():
    event = build_event("JOB_STARTED", job_id="C:\\private\\chapter text")
    assert event["job_id"] != "C:\\private\\chapter text"
    assert len(event["job_id"]) == 16


def test_unknown_event_fails_closed():
    try:
        build_event("NOT_A_REAL_EVENT")
    except ValueError as exc:
        assert "unsupported_event" in str(exc)
    else:
        raise AssertionError("unknown event accepted")
