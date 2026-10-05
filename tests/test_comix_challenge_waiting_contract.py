from pathlib import Path

from yomu.download.sources.chapter_source import looks_like_challenge
from yomu.runtime.job_store import JobStatus, transition_allowed


ROOT = Path(__file__).resolve().parents[1]


def test_challenge_classifier_distinguishes_browser_challenge_from_reader_markup():
    assert looks_like_challenge("Checking your browser before accessing the reader")
    assert looks_like_challenge("Please verify you are human")
    assert not looks_like_challenge("<main class='reader'><img src='page-1.webp'></main>")


def test_claimed_source_challenge_enters_waiting_state_and_can_resume_same_job():
    assert transition_allowed(JobStatus.CLAIMING, JobStatus.WAITING_SOURCE_VERIFICATION)
    assert transition_allowed(JobStatus.WAITING_SOURCE_VERIFICATION, JobStatus.QUEUED)
    assert not transition_allowed(JobStatus.WAITING_SOURCE_VERIFICATION, JobStatus.RUNNING)


def test_ui_removes_redundant_format_selector_and_keeps_output_plan_authority():
    shell = (ROOT / "ui" / "ui_shell.html").read_text(encoding="utf-8")
    script = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
    assert 'id="outputFormatSelect"' not in shell
    assert 'id="outputPlanField"' in shell
    assert "source_verification_pending: true" in script
    assert "/api/ui/source/verification/resume" in script
