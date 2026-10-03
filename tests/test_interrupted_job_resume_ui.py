"""Safe interrupted-job recovery exposed in the UI (TDD #56).

The backend already owns recovery: ``/api/ui/resume`` validates recoverability,
refuses a job whose previous runner is still alive and creates the linked attempt.
What was missing is the user-facing half: a normal tester had no way to trigger it,
so an interrupted chapter looked lost.

These tests pin the whole contract:

* the backend, not JavaScript, decides whether a job may be resumed (``can_resume``);
* an interrupted job that carries no valid recovery state is *not* resumable;
* resume is idempotent - a second request never creates a second attempt;
* the preserved checkpoint/progress evidence survives the resume request;
* the browser renders a real ``Retomar`` button for exactly those jobs, sends exactly
  one request per activation and refreshes authoritative state afterwards.

Everything runs against an isolated temporary SQLite store. No worker, no provider,
no network, no real jobs database and no real output directory.
"""

import _test_bootstrap  # noqa: F401

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import ui_bridge
from job_store import JobStatus, JobStore

ROOT = Path(__file__).resolve().parent
UI_SOURCE = ROOT / "static" / "tradutor_ui.js"
UI_SHELL = ROOT / "ui" / "ui_shell.html"


class ResumeCapabilityTests(unittest.TestCase):
    """Backend authority: which jobs report ``can_resume`` and why."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._patches = [
            patch.dict(os.environ, {"TRADUTOR_TEST_RUNTIME_ROOT": str(self.tmp)}),
            patch.object(ui_bridge, "env_status",
                         lambda *a, **k: {"env_exists": True, "nvidia_configured": True}),
            patch.object(ui_bridge, "_current_commit", lambda: "deadbeef"),
            patch.object(ui_bridge, "_current_branch", lambda: "main"),
        ]
        for patcher in self._patches:
            patcher.start()
        self.bridge = ui_bridge.UiBridge()

    def tearDown(self):
        self.bridge.store.close()
        for patcher in self._patches:
            patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _job(self, name="out"):
        return self.bridge.store.create_job(
            source_url="https://example.invalid/chapter",
            output_dir=str(self.tmp / name),
            command=["run"],
            configuration={"chapter_name": "Capítulo de teste"},
        )

    def _run_to(self, job_id, status, **fields):
        store = self.bridge.store
        store.claim_next_job("w1", 1)
        store.transition(job_id, JobStatus.STARTING, expected_worker="w1")
        store.transition(job_id, JobStatus.RUNNING, expected_worker="w1",
                         progress_current=7, progress_total=20, stage="ocr")
        if status == JobStatus.RUNNING:
            return
        store.transition(job_id, status, expected_worker="w1", **fields)

    def _record(self, job_id):
        return self.bridge._job_record(self.bridge.store.get_job(job_id))

    def test_recoverable_interrupted_job_reports_can_resume(self):
        job_id = self._job()
        self._run_to(job_id, JobStatus.INTERRUPTED,
                     interrupted_reason="worker_process_lost", recoverable=1)
        self.assertIs(self._record(job_id)["can_resume"], True)

    def test_interrupted_without_valid_recovery_state_is_not_resumable(self):
        """``interrupted`` alone must never unlock the action."""
        job_id = self._job()
        self._run_to(job_id, JobStatus.INTERRUPTED,
                     interrupted_reason="runner_exited_before_terminal", recoverable=0)
        record = self._record(job_id)
        self.assertEqual(record["status"], JobStatus.INTERRUPTED)
        self.assertIs(record["can_resume"], False)
        with self.assertRaises(ValueError):
            self.bridge.resume(job_id)

    def test_running_queued_completed_and_cancelled_are_not_resumable(self):
        # Each job is driven off the queue before the next is created, because the store
        # claims the oldest queued row rather than a chosen one.
        running = self._job("running")
        self._run_to(running, JobStatus.RUNNING)
        self.assertIs(self._record(running)["can_resume"], False)
        self.bridge.store.transition(running, JobStatus.FINISHED, expected_worker="w1")
        self.assertIs(self._record(running)["can_resume"], False)

        cancelled = self._job("cancelled")
        self._run_to(cancelled, JobStatus.RUNNING)
        self.bridge.store.transition(cancelled, JobStatus.CANCELLING, expected_worker="w1")
        self.bridge.store.transition(cancelled, JobStatus.CANCELLED, expected_worker="w1")
        self.assertIs(self._record(cancelled)["can_resume"], False)

        failed = self._job("failed")
        self._run_to(failed, JobStatus.RUNNING)
        self.bridge.store.transition(failed, JobStatus.FAILED, expected_worker="w1")
        self.assertIs(self._record(failed)["can_resume"], False)

        queued = self._job("queued")
        self.assertIs(self._record(queued)["can_resume"], False)

    def test_community_publish_job_is_never_resumable_from_the_ui(self):
        job_id = self.bridge.store.create_job(
            source_url="", output_dir=str(self.tmp / "publish"),
            command=["community_publish"],
            configuration={"job_type": "community_publish"})
        self._run_to(job_id, JobStatus.INTERRUPTED,
                     interrupted_reason="worker_stop", recoverable=1)
        self.assertIs(self._record(job_id)["can_resume"], False)

    def test_live_previous_runner_closes_the_capability(self):
        job_id = self._job()
        self._run_to(job_id, JobStatus.INTERRUPTED,
                     interrupted_reason="worker_stop", recoverable=1)
        with patch.object(ui_bridge, "_runner_still_alive", lambda job: True):
            self.assertIs(self._record(job_id)["can_resume"], False)
            with self.assertRaises(ValueError):
                self.bridge.resume(job_id)

    def test_capability_survives_a_reload_and_an_application_restart(self):
        """Availability comes from the persisted row, never from page-local state."""
        job_id = self._job()
        self._run_to(job_id, JobStatus.INTERRUPTED,
                     interrupted_reason="worker_stop", recoverable=1)
        self.bridge.store.close()
        restarted = ui_bridge.UiBridge()
        try:
            entries = [item for item in restarted.runtime_state()["resumable"]
                       if item["id"] == job_id]
            self.assertEqual(len(entries), 1)
            self.assertIs(entries[0]["can_resume"], True)
        finally:
            restarted.store.close()
        self.bridge = restarted

    def test_runtime_state_publishes_the_capability_for_the_browser(self):
        job_id = self._job()
        self._run_to(job_id, JobStatus.INTERRUPTED,
                     interrupted_reason="worker_stop", recoverable=1)
        state = self.bridge.runtime_state()
        entries = [item for item in state["resumable"] if item["id"] == job_id]
        self.assertEqual(len(entries), 1)
        self.assertIs(entries[0]["can_resume"], True)


class ResumeTransitionTests(unittest.TestCase):
    """The canonical resume operation: transition, checkpoint, idempotency."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._patches = [
            patch.dict(os.environ, {"TRADUTOR_TEST_RUNTIME_ROOT": str(self.tmp)}),
            patch.object(ui_bridge, "env_status",
                         lambda *a, **k: {"env_exists": True, "nvidia_configured": True}),
            patch.object(ui_bridge, "_current_commit", lambda: "deadbeef"),
            patch.object(ui_bridge, "_current_branch", lambda: "main"),
        ]
        for patcher in self._patches:
            patcher.start()
        self.bridge = ui_bridge.UiBridge()

    def tearDown(self):
        self.bridge.store.close()
        for patcher in self._patches:
            patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _interrupted(self):
        store = self.bridge.store
        job_id = store.create_job(
            source_url="https://example.invalid/chapter",
            output_dir=str(self.tmp / "out"), command=["run"],
            configuration={"chapter_name": "Capítulo de teste"})
        store.claim_next_job("w1", 1)
        store.transition(job_id, JobStatus.STARTING, expected_worker="w1")
        store.transition(job_id, JobStatus.RUNNING, expected_worker="w1",
                         progress_current=7, progress_total=20, stage="ocr")
        store.transition(job_id, JobStatus.INTERRUPTED, expected_worker="w1",
                         interrupted_reason="worker_process_lost", recoverable=1,
                         resume_from_stage="ocr")
        return job_id

    def test_resume_creates_one_linked_attempt_and_preserves_the_checkpoint(self):
        job_id = self._interrupted()
        before = self.bridge.store.get_job(job_id)

        result = self.bridge.resume(job_id)

        self.assertTrue(result["ok"])
        attempt = self.bridge.store.get_job(result["job_id"])
        self.assertEqual(attempt["status"], JobStatus.QUEUED)
        self.assertEqual(attempt["previous_job_id"], job_id)
        self.assertEqual(attempt["attempt"], 2)
        self.assertEqual(attempt["resume_from_stage"], "ocr")
        self.assertEqual(attempt["output_dir"], before["output_dir"])

        after = self.bridge.store.get_job(job_id)
        self.assertEqual(after["resume_from_stage"], before["resume_from_stage"])
        self.assertEqual(after["progress_current"], before["progress_current"])
        self.assertEqual(after["progress_total"], before["progress_total"])
        self.assertTrue(after["recoverable"])

    def test_second_resume_never_creates_a_second_attempt(self):
        job_id = self._interrupted()
        first = self.bridge.resume(job_id)
        second = self.bridge.resume(job_id)

        self.assertEqual(second["job_id"], first["job_id"])
        attempts = [job for job in self.bridge.store.list_jobs(statuses=None, limit=None)
                    if job.get("previous_job_id") == job_id]
        self.assertEqual(len(attempts), 1)

    def test_an_already_resumed_job_stops_advertising_the_action(self):
        job_id = self._interrupted()
        self.bridge.resume(job_id)
        record = self.bridge._job_record(self.bridge.store.get_job(job_id))
        self.assertIs(record["can_resume"], False)

    def test_resume_never_requeues_the_original_row(self):
        """Requeuing the original beside the new attempt would run the chapter twice."""
        job_id = self._interrupted()
        self.bridge.resume(job_id)
        self.bridge.resume(job_id)
        queued = [job["id"] for job in self.bridge.store.list_jobs(
            statuses=[JobStatus.QUEUED], limit=None)]
        self.assertNotIn(job_id, queued)
        self.assertEqual(len(queued), 1)


class ResumeHttpContractTests(unittest.TestCase):
    """The endpoint the browser is allowed to call, over the real app router."""

    def test_resume_route_is_authenticated_and_takes_a_canonical_job_id(self):
        import app_ui

        route = next(r for r in app_ui.app.routes
                     if getattr(r, "path", "") == "/api/ui/resume")
        self.assertEqual(set(route.methods), {"POST"})
        source = (ROOT / "app_ui.py").read_text(encoding="utf-8")
        marker = '@app.post("/api/ui/resume")'
        handler = source[source.index(marker):]
        handler = handler[:handler.index("@app.post", len(marker))]
        # Ownership is enforced before the bridge is reached, and the identifier comes
        # from the request payload rather than from anything the page derived.
        self.assertIn("_owned_ui_job(request, job_id, mutate=True)", handler)
        self.assertIn("BRIDGE.resume", handler)


class ResumeFrontendContractTests(unittest.TestCase):
    """The production frontend actually calls the canonical endpoint."""

    def setUp(self):
        self.source = UI_SOURCE.read_text(encoding="utf-8")
        self.shell = UI_SHELL.read_text(encoding="utf-8")

    def test_frontend_has_a_caller_for_the_canonical_resume_endpoint(self):
        self.assertIn("'/api/ui/resume'", self.source)

    def test_resume_control_is_a_real_accessible_button(self):
        self.assertIn('id="interruptedJobsPanel"', self.shell)
        self.assertIn("createElement('button')", self.source)
        self.assertIn("Retomar", self.source)

    def test_frontend_uses_the_backend_capability_not_a_local_status_guess(self):
        block = self.source[self.source.index("function renderResumableJobs"):]
        block = block[:block.index("\n  function ", 10)]
        self.assertIn("can_resume === true", block)
        self.assertNotIn("'interrupted'", block)

    def test_frontend_uses_the_shared_authenticated_request_helper(self):
        block = self.source[self.source.index("async function resumeInterruptedJob"):]
        block = block[:block.index("\n  function ", 10)]
        self.assertIn("await api('/api/ui/resume'", block)
        self.assertNotIn("fetch(", block)
        # Authoritative refresh instead of an optimistic local state change.
        self.assertIn("pollState()", block)


class ResumeBrowserHarnessTests(unittest.TestCase):
    """Real DOM rendering and the real click path, in an isolated Node harness."""

    def test_resume_ui_harness(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        result = subprocess.run(
            [node, str(ROOT / "test_interrupted_job_resume_ui.mjs")],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
