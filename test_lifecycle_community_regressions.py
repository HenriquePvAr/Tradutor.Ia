"""Offline regression coverage for UI lifecycle and community publication contracts."""

import _test_bootstrap  # noqa: F401

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from community_auth import RequestPrincipal, ResourceNotFound
from community_api import CommunityApi
from community_http import _community_call
from job_store import JobStatus, JobStore
import ui_bridge


def _run(coro):
    return asyncio.run(coro)


def _new_store(tmp: Path) -> JobStore:
    return JobStore(tmp / "jobs.sqlite3")


def _new_translation(store: JobStore, *, status=JobStatus.QUEUED) -> str:
    return store.create_job(
        source_url="https://example.invalid/chapter",
        output_dir=str(store.db_path.parent / "output"),
        command=["python", "run_webtoon.py"],
        configuration={"job_type": "translation"},
        initial_status=status,
    )


def _make_review_required(store: JobStore) -> str:
    job_id = _new_translation(store)
    store.claim_next_job("worker-test", 999999)
    store.transition(job_id, JobStatus.STARTING, expected_worker="worker-test")
    store.transition(job_id, JobStatus.RUNNING, expected_worker="worker-test")
    store.update_fields(job_id, exit_code=0)
    store.transition(job_id, JobStatus.REVIEW_REQUIRED,
                     reason_code="quality_review_required")
    return job_id


class ReviewLifecycleTests(unittest.TestCase):
    def test_confirmed_review_becomes_terminal_and_is_idempotent(self):
        tmp = Path(tempfile.mkdtemp())
        store = _new_store(tmp)
        try:
            job_id = _make_review_required(store)
            result = store.complete_review(job_id)
            self.assertEqual(result["status"], JobStatus.FINISHED)
            self.assertEqual(result["stage"], "review_completed")
            self.assertEqual(result["reason_code"], "quality_review_completed")
            self.assertIsNotNone(result["review_confirmed_at"])
            self.assertIsNone(result["runner_pid"])
            confirmed_at = result["review_confirmed_at"]
            time.sleep(0.01)
            again = store.complete_review(job_id)
            self.assertEqual(again["status"], JobStatus.FINISHED)
            self.assertEqual(again["review_confirmed_at"], confirmed_at)
        finally:
            store.close()

    def test_reconcile_repairs_old_timestamp_only_confirmation(self):
        tmp = Path(tempfile.mkdtemp())
        store = _new_store(tmp)
        try:
            job_id = _make_review_required(store)
            confirmed_at = time.time() - 10
            store.update_fields(job_id, review_confirmed_at=confirmed_at)
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.REVIEW_REQUIRED)
            self.assertEqual(store.reconcile_confirmed_reviews(), [job_id])
            job = store.get_job(job_id)
            self.assertEqual(job["status"], JobStatus.FINISHED)
            self.assertEqual(job["review_confirmed_at"], confirmed_at)
        finally:
            store.close()


class CancellationContractTests(unittest.TestCase):
    def test_queued_job_is_cancelled_by_explicit_id(self):
        tmp = Path(tempfile.mkdtemp())
        store = _new_store(tmp)
        bridge = object.__new__(ui_bridge.UiBridge)
        bridge.store = store
        bridge.history_revision = 0
        try:
            job_id = _new_translation(store)
            result = _run(bridge.cancel(job_id=job_id))
            self.assertEqual(result["job_id"], job_id)
            self.assertEqual(result["status"], JobStatus.CANCELLED)
            self.assertFalse(result["cancelable"])
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.CANCELLED)
        finally:
            store.close()

    def test_active_runner_returns_cancelling_without_waiting(self):
        tmp = Path(tempfile.mkdtemp())
        store = _new_store(tmp)
        bridge = object.__new__(ui_bridge.UiBridge)
        bridge.store = store
        bridge.history_revision = 0
        try:
            job_id = _new_translation(store)
            store.claim_next_job("worker-test", 999999)
            store.transition(job_id, JobStatus.STARTING, expected_worker="worker-test")
            store.transition(job_id, JobStatus.RUNNING, expected_worker="worker-test")
            with patch.object(ui_bridge, "_runner_still_alive", return_value=True):
                result = _run(bridge.cancel(job_id=job_id))
            self.assertEqual(result["status"], JobStatus.CANCELLING)
            self.assertTrue(result["cancelable"])
            self.assertTrue(store.cancel_requested(job_id))
        finally:
            store.close()

    def test_terminal_cancel_is_idempotent(self):
        tmp = Path(tempfile.mkdtemp())
        store = _new_store(tmp)
        bridge = object.__new__(ui_bridge.UiBridge)
        bridge.store = store
        bridge.history_revision = 0
        try:
            job_id = _new_translation(store)
            store.transition(job_id, JobStatus.CANCELLED, reason_code="user_cancelled")
            result = _run(bridge.cancel(job_id=job_id))
            self.assertEqual(result["status"], JobStatus.CANCELLED)
            self.assertFalse(result["cancelable"])
        finally:
            store.close()


class CommunityResolutionTests(unittest.TestCase):
    def test_local_legacy_job_resolves_and_binds_owner_after_manifest_validation(self):
        tmp = Path(tempfile.mkdtemp())
        output = tmp / "output" / "chapter"
        output.mkdir(parents=True)
        pdf = output / "chapter.pdf"
        pdf.write_bytes(b"%PDF-1.7\nminimal offline fixture\n")
        store = _new_store(tmp)
        api = CommunityApi(store, community_db_path=tmp / "community.sqlite3", output_root=tmp / "output")
        try:
            job_id = _make_review_required(store)
            store.update_fields(job_id, output_dir=str(output), pdf_path=str(pdf), exit_code=0)
            job = store.get_job(job_id)
            store.complete_review(job_id)
            job = store.get_job(job_id)
            (output / "job_manifest.json").write_text(json.dumps({
                "job_id": job_id,
                "run_id": job["run_id"],
                "status": JobStatus.REVIEW_REQUIRED,
                "exit_code": 0,
                "pdf_path": str(pdf),
            }), encoding="utf-8")
            principal = RequestPrincipal("local-user", True, auth_source="local_session")
            resolved = api._resolve_translation_job(job_id, principal)
            self.assertEqual(resolved["source_job_id"], job_id)
            self.assertEqual(Path(resolved["pdf_path"]), pdf.resolve())
            self.assertEqual(store.get_job(job_id)["configuration"]["community_owner_id"], "local-user")
        finally:
            api.close()
            store.close()

    def test_not_found_is_structured_for_the_ui(self):
        with self.assertRaises(Exception) as caught:
            _community_call(lambda: (_ for _ in ()).throw(ResourceNotFound("pdf_not_found")))
        error = caught.exception
        self.assertEqual(getattr(error, "status_code", None), 404)
        self.assertIsInstance(error.detail, dict)
        self.assertEqual(error.detail["code"], "pdf_not_found")
        self.assertNotEqual(error.detail["message"], "not_found")


class FrontendContractTests(unittest.TestCase):
    def test_frontend_has_timeout_per_job_cancel_and_review_completion_state(self):
        source = Path("static/tradutor_ui.js").read_text(encoding="utf-8")
        for marker in ("AbortController", "/api/ui/jobs/", "unhandledrejection", "review_status !== 'completed'", "revisão concluída"):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
