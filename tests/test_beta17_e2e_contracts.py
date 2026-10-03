"""Beta.17 regressions: one owner, one attempt, explicit source outcomes."""

import sqlite3
from pathlib import Path

from job_store import JobStore


def _job(store: JobStore, key: str) -> str:
    return store.create_job(
        source_url="https://example.invalid/chapter",
        output_dir=str(Path(store.db_path).parent / "out"),
        command=["python", "-c", "pass"],
        configuration={
            "job_type": "translation",
            "chapter_slug": "chapter",
            "trace_id": "trace",
            "analysis_result_id": key.split(":", 1)[-1],
            "idempotency_key": key,
        },
    )


def test_source_analyze_route_has_no_job_side_effect():
    source = Path("app_ui.py").read_text(encoding="utf-8")
    block = source[source.index('@app.post("/api/ui/source/analyze")'):]
    block = block[:block.index('@app.post("/api/ui/source/report")')]
    assert "BRIDGE.start(" not in block
    assert "job_created" not in block


def test_attempt_idempotency_is_database_enforced(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    try:
        first = _job(store, "trace-a:analysis-a")
        try:
            _job(store, "trace-a:analysis-a")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("duplicate active attempt created a second job")
        assert store.get_job(first)["id"] == first
    finally:
        store.close()


def test_distinct_attempts_remain_independent(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    try:
        first = _job(store, "trace-a:analysis-a")
        # A new attempt for the same chapter is allowed once the previous attempt is
        # terminal; the active-attempt guard must not become a permanent ban.
        from job_store import JobStatus
        for state in (JobStatus.CLAIMING, JobStatus.STARTING, JobStatus.RUNNING):
            store.transition(first, state)
        store.transition(first, JobStatus.FAILED, reason_code="source_validation_failed")
        second = _job(store, "trace-b:analysis-b")
        assert first != second
    finally:
        store.close()
