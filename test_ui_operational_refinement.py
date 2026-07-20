"""Hermetic contracts for the operational UI refinements."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _test_bootstrap  # noqa: F401

import ui_bridge
from job_store import JobStatus, JobStore
from process_options import build_background_process_options
from ui_bridge import UiBridge


class OperationalBridge(UiBridge):
    def __init__(self, db: Path):
        self.history_store = mock.Mock()
        self.history = []
        self.profile = {}
        self.store = JobStore(db)
        self.history_revision = 1

    def close(self):
        self.store.close()


class QualityReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.output = self.tmp / "quality-output"
        self.output.mkdir()
        self.bridge = OperationalBridge(self.tmp / "jobs.sqlite3")
        report_path = self.output / "quality_report.json"
        image_path = self.output / "page_001.png"
        image_path.write_bytes(b"fake-image")
        report_path.write_text(json.dumps({
            "pages": [{
                "index": 1,
                "output_path": str(image_path),
                "translation_terminal_items": [{
                    "id": "BALAO_1", "classification": "speech", "text": "Hello",
                    "translation": "Ola", "manual_review_required": True,
                    "preserved_original": False,
                }],
            }],
        }), encoding="utf-8")
        self.job_id = self.bridge.store.create_job(
            source_url="https://example.test/chapter", output_dir=str(self.output),
            command=["python", "run_webtoon.py"],
            configuration={"job_type": "translation", "chapter_name": "Example"},
        )
        self.bridge.store.transition(self.job_id, JobStatus.CLAIMING, worker_id="w")
        self.bridge.store.transition(self.job_id, JobStatus.STARTING)
        self.bridge.store.transition(self.job_id, JobStatus.RUNNING)
        self.bridge.store.transition(
            self.job_id, JobStatus.REVIEW_REQUIRED,
            quality_report_path=str(report_path), reason_code="quality_review_required",
        )

    def tearDown(self):
        self.bridge.close()

    def test_quality_review_exposes_human_item_and_page_reference(self):
        review = self.bridge.quality_review(self.job_id)
        self.assertEqual(review["pending_count"], 1)
        item = review["items"][0]
        self.assertEqual(item["page"], 1)
        self.assertEqual(item["original"], "Hello")
        self.assertIn("confirma", item["reason"].lower())
        self.assertIn(f"/api/ui/quality-review/{self.job_id}/page/1", item["page_url"])

    def test_item_action_persists_and_confirmation_requires_no_pending_items(self):
        with self.assertRaisesRegex(ValueError, "quality_review_items_pending"):
            self.bridge.confirm_quality_review(self.job_id)
        review = self.bridge.quality_review_action(self.job_id, "p1:iBALAO_1", "preserved_original")
        self.assertEqual(review["pending_count"], 0)
        confirmed = self.bridge.confirm_quality_review(self.job_id)
        self.assertTrue(confirmed["confirmed"])
        self.assertEqual(self.bridge.store.review_actions(self.job_id)["p1:iBALAO_1"], "preserved_original")


class LifecycleAndProcessTests(unittest.TestCase):
    def test_cancel_timestamps_are_persisted(self):
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder) / "jobs.sqlite3")
            job_id = store.create_job(
                source_url="https://example.test/chapter", output_dir="", command=["python"],
                configuration={"job_type": "translation"},
            )
            store.transition(job_id, JobStatus.CLAIMING, worker_id="w")
            store.transition(job_id, JobStatus.STARTING)
            store.transition(job_id, JobStatus.RUNNING)
            self.assertTrue(store.request_cancel(job_id))
            requested = store.get_job(job_id)
            self.assertIsNotNone(requested["cancellation_requested_at"])
            store.transition(job_id, JobStatus.CANCELLING)
            store.transition(job_id, JobStatus.CANCELLED, reason_code="user_cancelled")
            self.assertIsNotNone(store.get_job(job_id)["cancellation_completed_at"])
            store.close()

    def test_stage_clock_and_eta_inputs_are_persisted(self):
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder) / "jobs.sqlite3")
            job_id = store.create_job(
                source_url="https://example.test/chapter", output_dir="", command=["python"],
                configuration={"job_type": "translation"},
            )
            store.transition(job_id, JobStatus.CLAIMING, worker_id="w")
            store.transition(job_id, JobStatus.STARTING)
            store.transition(job_id, JobStatus.RUNNING)
            store.update_progress(job_id, stage="downloading_pages", current=2, total=4)
            row = store.get_job(job_id)
            self.assertEqual(row["stage"], "downloading_pages")
            self.assertIsNotNone(row["stage_started_at"])
            store.close()

    def test_background_options_are_shell_free_and_session_is_explicit(self):
        options = build_background_process_options(new_session=True)
        self.assertFalse(options["shell"])
        self.assertTrue(options["stdin"] is not None)
        if ui_bridge.os.name != "nt":
            self.assertTrue(options["start_new_session"])


class FrontendOperationalContractsTests(unittest.TestCase):
    def test_frontend_exposes_review_cancel_eta_and_retry(self):
        root = Path(__file__).resolve().parent
        js = (root / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        shell = (root / "ui" / "ui_shell.html").read_text(encoding="utf-8")
        for marker in (
            "qualityReviewPanel", "/api/ui/quality-review/action", "confirmQualityReview",
            "runCancelAction", "runEtaHuman", "runRetryAction", "qualityReviewFilter",
        ):
            self.assertIn(marker, js + shell, marker)

    def test_frontend_uses_structured_human_messages_not_tracebacks(self):
        js = (Path(__file__).resolve().parent / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        self.assertIn("worker_unavailable", js)
        self.assertIn("user_cancelled", js)
        self.assertNotIn("traceback", js[js.index("function renderRunStatus"):])


if __name__ == "__main__":
    unittest.main()

