"""The worker analyses a URL source before any child process exists.

The point of doing this in the worker, before ``_spawn_runner``, is that a review, a failed
analysis or a cancellation must not leave a runner behind. These tests intercept the spawn
and assert it never happens for those outcomes.

Hermetic: fake analyses, no browser, no network, no child process.
"""

import _test_bootstrap  # noqa: F401

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from job_store import JobStatus, JobStore
from source_analysis_phase import (
    AWAITING_REVIEW, FAILED, READY_FOR_RUNNER, SourceAnalysisPhaseResult,
    has_usable_selection, should_spawn_runner,
)
from worker_service import Worker

URL = "https://www.webtoons.com/en/drama/serie/episode-1/viewer?title_no=1&episode_no=1"


class FakeCandidate:
    def __init__(self, index):
        self.id = f"c{index:03}"


class FakeAnalysis:
    """Mimics the analysis object the phase consumes."""

    def __init__(self, outcome, *, accepted=5, warnings=()):
        from universal_chapter_adapter import SUPPORTED_SPECIFIC_ADAPTER  # noqa: F401

        self.outcome = outcome
        self.accepted = [FakeCandidate(i) for i in range(accepted)]
        self._warnings = list(warnings)

    def public(self):
        return {"adapter": "webtoons", "adapter_version": "1",
                "candidate_count": len(self.accepted),
                "accepted_count": len(self.accepted), "confidence": 1.0,
                "warnings": self._warnings,
                "accepted": [{"id": c.id} for c in self.accepted]}


def specific_outcome():
    from universal_chapter_adapter import SUPPORTED_SPECIFIC_ADAPTER

    return SUPPORTED_SPECIFIC_ADAPTER


def review_outcome():
    from universal_chapter_adapter import REVIEW_REQUIRED_MEDIUM_CONFIDENCE

    return REVIEW_REQUIRED_MEDIUM_CONFIDENCE


class _Worker(Worker):
    """Real worker logic with the analysis and the spawn replaced."""

    def __init__(self, store, analysis=None, error=None):
        self.store = store
        self.worker_id = "w-test"
        self.pid = 4321
        self._active = None
        self._stop_requested = False
        self._analysis = analysis
        self._error = error
        self.spawns = 0

    def _analyze_source(self, url, *, cancel_check=None, on_progress=None):
        if self._error is not None:
            raise self._error
        return self._analysis

    def _spawn_runner(self, job):
        self.spawns += 1
        raise AssertionError("runner spawned")


class _WorkerWithPreparedJob(_Worker):
    def __init__(self, store, prepared):
        super().__init__(store)
        self._prepared = prepared

    def _prepare_source(self, job):
        return self._prepared


class _BlockingCancellationWorker(_Worker):
    def __init__(self, store):
        super().__init__(store)
        self.started = threading.Event()

    def _analyze_source(self, url, *, cancel_check=None, on_progress=None):
        from chapter_source import SourceError

        self.started.set()
        while not cancel_check():
            time.sleep(0.01)
        raise SourceError("cancelled", "test_cancel")


class WorkerPhaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = JobStore(self.tmp / "jobs.sqlite3")

    def tearDown(self):
        self.store.close()

    def queued_url_job(self, **fields):
        job_id = self.store.create_job(
            source_url=URL, output_dir=str(self.tmp / "out"),
            configuration={"job_type": "translation"}, command=["python", "-c", "pass"])
        self.store.update_fields(job_id, source_type="url", **fields)
        # claim_next_job already moved the job to CLAIMING before _run_one sees it.
        self.store.transition(job_id, JobStatus.CLAIMING, worker_id="w-test")
        return self.store.get_job(job_id)

    def env_ok(self):
        return mock.patch("source_analysis_phase.apply_source_analysis",
                          wraps=__import__("source_analysis_phase").apply_source_analysis)

    # ---- runner suppression ------------------------------------------------
    def test_review_outcome_never_spawns_a_runner(self):
        job = self.queued_url_job()
        worker = _Worker(self.store, analysis=FakeAnalysis(review_outcome()))
        self.assertIsNone(worker._prepare_source(job))
        self.assertEqual(worker.spawns, 0)
        row = self.store.get_job(job["id"])
        self.assertEqual(row["status"], JobStatus.AWAITING_SOURCE_REVIEW)

    def test_incomplete_coverage_never_spawns_a_runner(self):
        job = self.queued_url_job()
        worker = _Worker(self.store, analysis=FakeAnalysis(
            specific_outcome(), warnings=["scroll_incomplete"]))
        self.assertIsNone(worker._prepare_source(job))
        self.assertEqual(worker.spawns, 0)
        row = self.store.get_job(job["id"])
        self.assertEqual(row["status"], JobStatus.FAILED)
        self.assertEqual(row["reason_code"], "incomplete_source_coverage")

    def test_an_analysis_error_is_terminal_and_never_spawns(self):
        from chapter_source import SourceError

        job = self.queued_url_job()
        worker = _Worker(self.store, error=SourceError("source_not_ready", "no_driver"))
        self.assertIsNone(worker._prepare_source(job))
        self.assertEqual(worker.spawns, 0)
        row = self.store.get_job(job["id"])
        self.assertEqual(row["status"], JobStatus.FAILED)
        self.assertEqual(row["reason_code"], "source_not_ready")

    def test_a_cancel_requested_job_never_reaches_analysis(self):
        job = self.queued_url_job()
        self.store.request_cancel(job["id"])
        worker = _Worker(self.store, error=AssertionError("analysis ran"))
        self.assertIsNone(worker._prepare_source(job))
        self.assertEqual(worker.spawns, 0)

    def test_cancellation_during_source_analysis_terminalizes_claim(self):
        job = self.queued_url_job()
        worker = _BlockingCancellationWorker(self.store)
        result = {}
        thread = threading.Thread(target=lambda: result.setdefault("job", worker._prepare_source(job)))
        thread.start()
        self.assertTrue(worker.started.wait(5), "analysis did not start")
        self.store.request_cancel(job["id"])
        thread.join(5)
        self.assertFalse(thread.is_alive(), "analysis cancellation did not return")
        self.assertIsNone(result.get("job"))
        row = self.store.get_job(job["id"])
        self.assertEqual(row["status"], JobStatus.CANCELLED)
        self.assertEqual(row["stage"], "cancelled")
        self.assertEqual(row["reason_code"], "user_cancelled")

    def test_a_terminal_job_never_reaches_analysis(self):
        job = self.queued_url_job()
        for target in (JobStatus.STARTING, JobStatus.RUNNING, JobStatus.FINISHED):
            self.store.transition(job["id"], target)
        worker = _Worker(self.store, error=AssertionError("analysis ran"))
        self.assertIsNone(worker._prepare_source(self.store.get_job(job["id"])))

    # ---- runner allowed ----------------------------------------------------
    def test_a_high_confidence_analysis_allows_the_runner(self):
        job = self.queued_url_job()
        worker = _Worker(self.store, analysis=FakeAnalysis(specific_outcome()))
        with mock.patch("source_analysis_phase.apply_source_analysis") as apply:
            apply.return_value = SourceAnalysisPhaseResult(
                outcome=READY_FOR_RUNNER, job_id=job["id"], status=JobStatus.QUEUED)
            prepared = worker._prepare_source(job)
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["id"], job["id"])

    def test_automatic_selection_is_persisted_but_runner_waits_for_authorization(self):
        job = self.queued_url_job(
            configuration_json='{"job_type":"translation","mode":"fast","full":false,'
                               '"max_images":2,"force":true,"use_cache":false,'
                               '"use_context":true}',
            command_json='["python","run_webtoon.py","url","--max-images","2"]',
        )
        worker = _Worker(self.store, analysis=FakeAnalysis(specific_outcome(), accepted=3))

        prepared = worker._prepare_source(job)

        self.assertIsNone(prepared)
        row = self.store.get_job(job["id"])
        self.assertEqual(row["status"], JobStatus.SOURCE_ANALYSIS_READY)
        self.assertEqual(row["configuration"]["max_images"], 2)
        self.assertEqual(len(row["source_selection"]["candidate_ids"]), 3)
        self.assertIn("--max-images", row["command"])
        self.assertIn("2", row["command"])
        self.assertEqual(row["command"].count("--source-candidate-id"), 2)
        self.assertIn("c000", row["command"])
        self.assertIn("c001", row["command"])
        self.assertNotIn("c002", row["command"])

    def test_an_existing_selection_skips_reanalysis(self):
        job = self.queued_url_job(
            source_selection_json='{"candidate_ids": ["c001", "c002"]}')
        worker = _Worker(self.store, error=AssertionError("re-analysed"))
        prepared = worker._prepare_source(job)
        self.assertIsNotNone(prepared)          # goes straight to the runner

    def test_existing_selection_refreshes_runner_command_before_reuse(self):
        job = self.queued_url_job(
            configuration_json='{"job_type":"translation","mode":"fast","full":false,'
                               '"max_images":2,"force":true,"use_cache":false,'
                               '"use_context":true}',
            command_json='["python","run_webtoon.py","url","--max-images","2"]',
            source_selection_json='{"candidate_ids": ["c001", "c002", "c003"], '
                                  '"automatic": true}',
        )
        worker = _Worker(self.store, error=AssertionError("re-analysed"))

        prepared = worker._prepare_source(job)

        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["command"].count("--source-candidate-id"), 2)
        self.assertIn("c001", prepared["command"])
        self.assertIn("c002", prepared["command"])
        self.assertNotIn("c003", prepared["command"])

    def test_a_local_folder_job_skips_url_analysis(self):
        job = self.queued_url_job()
        self.store.update_fields(job["id"], source_type="local_folder")
        worker = _Worker(self.store, error=AssertionError("url analysis ran"))
        prepared = worker._prepare_source(self.store.get_job(job["id"]))
        self.assertIsNotNone(prepared)

    def test_source_analysis_keeps_the_worker_lease_alive(self):
        job = self.queued_url_job()
        self.store.register_worker("w-test", 4321, create_time=1.0)
        started = threading.Event()
        release = threading.Event()

        def blocking_analysis(_url, *, cancel_check=None, on_progress=None):
            started.set()
            release.wait(timeout=5)
            return FakeAnalysis(specific_outcome())

        worker = _Worker(self.store)
        worker._analyze_source = blocking_analysis
        before = self.store.healthy_worker(stale_seconds=999)["heartbeat_at"]

        thread = threading.Thread(target=lambda: worker._prepare_source(job))
        thread.start()
        self.assertTrue(started.wait(timeout=2))
        deadline = time.time() + 3
        after = before
        while time.time() < deadline and after <= before:
            row = self.store.healthy_worker(stale_seconds=999)
            after = row["heartbeat_at"] if row else 0
            time.sleep(0.05)
        release.set()
        thread.join(timeout=5)

        self.assertGreater(after, before)

    def test_url_job_without_usable_selection_never_spawns_runner(self):
        job = self.queued_url_job(
            source_analysis_json='{"outcome":"source_analysis_pending","accepted":[]}',
            source_selection_json="{}")
        worker = _WorkerWithPreparedJob(self.store, self.store.get_job(job["id"]))

        worker._run_one(job)

        self.assertEqual(worker.spawns, 0)
        row = self.store.get_job(job["id"])
        self.assertEqual(row["status"], JobStatus.FAILED)
        self.assertEqual(row["stage"], "source_selection")
        self.assertEqual(row["reason_code"], "missing_source_selection")


class SelectionReuseTests(unittest.TestCase):
    def test_usable_selection_requires_candidate_ids(self):
        self.assertTrue(has_usable_selection({"source_selection_json": '{"candidate_ids":["a"]}'}))
        self.assertFalse(has_usable_selection({"source_selection_json": '{"candidate_ids":[]}'}))
        self.assertFalse(has_usable_selection({"source_selection_json": "not json"}))
        self.assertFalse(has_usable_selection({}))


class SpawnGateTests(unittest.TestCase):
    def test_the_gate_refuses_every_state_that_must_not_run(self):
        for status in (JobStatus.FINISHED, JobStatus.FAILED, JobStatus.CANCELLED,
                       JobStatus.REVIEW_REQUIRED, JobStatus.AWAITING_SOURCE_REVIEW,
                       JobStatus.SOURCE_ANALYSIS_READY):
            self.assertFalse(should_spawn_runner({"status": status}), status)

    def test_the_gate_refuses_a_cancel_request(self):
        self.assertFalse(
            should_spawn_runner({"status": JobStatus.QUEUED, "cancel_requested": 1}))

    def test_the_gate_allows_a_plain_queued_job(self):
        self.assertTrue(should_spawn_runner({"status": JobStatus.QUEUED}))

    def test_a_non_ready_result_always_refuses(self):
        job = {"status": JobStatus.QUEUED}
        for outcome in (AWAITING_REVIEW, FAILED):
            result = SourceAnalysisPhaseResult(outcome=outcome, job_id="x")
            self.assertFalse(should_spawn_runner(job, result), outcome)


if __name__ == "__main__":
    unittest.main()
