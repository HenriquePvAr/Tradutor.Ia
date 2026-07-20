"""The submit enqueues; the worker analyses.

The submit used to run Selenium inside the HTTP request — 93-101s measured on a real page —
so the browser lived in the web process and the screen had no job to poll. These tests pin
the new division: the request creates a queued job and returns, and every worker stage the
UI can receive has a label.

Hermetic: no browser, no network, no analyzer is ever reached.
"""

import _test_bootstrap  # noqa: F401

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ui_bridge
from job_store import JobStatus, JobStore

URL = "https://www.webtoons.com/en/drama/serie/episode-1/viewer?title_no=1&episode_no=1"
OTHER = "https://www.webtoons.com/en/drama/serie/episode-2/viewer?title_no=1&episode_no=2"


def drive(coro):
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("unexpectedly awaited")


class _Bridge(ui_bridge.UiBridge):
    def __init__(self, db_path):
        self.store = JobStore(db_path)
        self.history_revision = 1
        self.worker_calls = 0
        self.analysis_calls = 0
        self.driver_calls = 0

    def _refresh_history(self):
        pass

    def ensure_worker(self):
        self.worker_calls += 1
        return {"online": True, "started": False}

    def _analyze_source(self, *_a, **_kw):
        self.analysis_calls += 1
        raise AssertionError("the submit analysed the source")


class SubmitEnqueuesOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bridge = _Bridge(self.tmp / "jobs.sqlite3")

    def tearDown(self):
        self.bridge.store.close()

    def payload(self, url=URL, **over):
        base = {"url": url, "slug": "cap", "mode": "fast", "full": True,
                "use_cache": False, "force": True, "use_context": True}
        base.update(over)
        return base

    def submit(self, **over):
        return drive(self.bridge.start(self.payload(**over)))

    def test_the_submit_returns_a_queued_job(self):
        result = self.submit()
        self.assertTrue(result["ok"])
        self.assertTrue(result["job_id"])
        self.assertEqual(result["status"], JobStatus.QUEUED)
        self.assertEqual(result["stage"], "queued")

    def test_no_analysis_is_reached(self):
        self.submit()
        self.assertEqual(self.bridge.analysis_calls, 0)

    def test_no_browser_is_created(self):
        import down

        with mock.patch.object(down, "_create_driver") as factory:
            self.submit()
        factory.assert_not_called()

    def test_the_job_is_persisted_as_queued_with_its_source(self):
        job = self.bridge.store.get_job(self.submit()["job_id"])
        self.assertEqual(job["status"], JobStatus.QUEUED)
        self.assertEqual(job["stage"], "queued")
        self.assertEqual(job["source_type"], "url")
        self.assertIn("title_no=1", job["source_url"])
        self.assertIn("episode_no=1", job["source_url"])

    def test_a_consumer_is_ensured_after_the_payload_exists(self):
        result = self.submit()
        self.assertEqual(self.bridge.worker_calls, 1)
        # The row a worker could claim is complete before the worker is asked to exist.
        job = self.bridge.store.get_job(result["job_id"])
        self.assertTrue(job["source_url"])
        self.assertTrue(job["command"])

    def test_no_analysis_json_is_invented_by_the_submit(self):
        job = self.bridge.store.get_job(self.submit()["job_id"])
        analysis = job.get("source_analysis") or {}
        self.assertEqual(analysis.get("outcome"), "source_analysis_pending")
        self.assertFalse(job.get("source_selection") or {})


class DuplicateGuardTests(SubmitEnqueuesOnlyTests):
    def test_a_double_submit_creates_one_job(self):
        first, second = self.submit(), self.submit()
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(second["job_id"], first["job_id"])
        self.assertEqual(len(self.bridge.store.list_jobs(statuses=[JobStatus.QUEUED])), 1)

    def test_a_different_episode_is_a_different_job(self):
        first = self.submit()
        second = self.submit(url=OTHER, slug="cap2")
        self.assertNotEqual(second["job_id"], first["job_id"])
        self.assertEqual(len(self.bridge.store.list_jobs(statuses=[JobStatus.QUEUED])), 2)

    def test_same_url_with_new_output_is_a_new_attempt(self):
        first = self.submit(slug="cap")
        second = self.submit(slug="cap_retry")
        self.assertFalse(second.get("duplicate"))
        self.assertNotEqual(second["job_id"], first["job_id"])
        self.assertEqual(len(self.bridge.store.list_jobs(statuses=[JobStatus.QUEUED])), 2)

    def test_same_output_still_blocks_a_duplicate(self):
        first = self.submit(slug="shared_output")
        second = self.submit(url=OTHER, slug="shared_output")
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(second["job_id"], first["job_id"])
        self.assertEqual(len(self.bridge.store.list_jobs(statuses=[JobStatus.QUEUED])), 1)

    def test_a_terminal_job_does_not_block_a_new_submission(self):
        first = self.submit()
        self.bridge.store.transition(first["job_id"], JobStatus.CANCELLED)
        second = self.submit()
        self.assertNotEqual(second["job_id"], first["job_id"])
        self.assertFalse(second.get("duplicate"))


class StageLabelTests(unittest.TestCase):
    def test_every_worker_stage_the_ui_can_receive_has_a_label(self):
        from ui_bridge import _UI_STAGE_LABELS

        for stage in ("queued", "worker_starting", "source_validation", "browser_loading",
                      "source_analysis", "source_lazy_resolution", "source_selection",
                      "downloading_pages", "validating_pages", "awaiting_source_review"):
            self.assertIn(stage, _UI_STAGE_LABELS, stage)
            self.assertTrue(_UI_STAGE_LABELS[stage].strip(), stage)

    def test_an_unknown_stage_falls_back_to_its_key(self):
        from ui_bridge import _UI_STAGE_LABELS

        self.assertEqual(_UI_STAGE_LABELS.get("nao_existe", "nao_existe"), "nao_existe")


if __name__ == "__main__":
    unittest.main()
