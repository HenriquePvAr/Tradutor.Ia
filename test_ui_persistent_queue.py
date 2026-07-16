"""The UI creates and reads jobs through the persistent store, never running them."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ui_bridge
from community_auth import RequestPrincipal
from job_store import JobStatus, JobStore


def _run(coro):
    """Drive a coroutine that performs no real awaits (avoids creating an event loop,
    which the offline network guard blocks via socketpair)."""
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise RuntimeError("coroutine did not complete synchronously")


class UiPersistentQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "jobs.sqlite3"
        self._patches = [
            patch.object(ui_bridge, "JOBS_DB_PATH", self.db),
            patch.object(ui_bridge, "env_status", lambda *a, **k: {"env_exists": True, "nvidia_configured": True}),
            patch.object(ui_bridge, "_current_commit", lambda: "deadbeef"),
            patch.object(ui_bridge, "_current_branch", lambda: "main"),
        ]
        for p in self._patches:
            p.start()
        self.bridge = ui_bridge.UiBridge()

    def tearDown(self):
        self.bridge.store.close()
        for p in self._patches:
            p.stop()

    def _payload(self):
        return {
            "url": "https://www.webtoons.com/en/action/x/episode-1/viewer?title_no=1&episode_no=1",
            "mode": "fast", "full": True, "force": True, "use_cache": False,
            "slug": "x_episode_1_test",
        }

    def test_start_creates_queued_job_without_running(self):
        import asyncio
        result = _run(self.bridge.start(self._payload()))
        self.assertTrue(result["ok"])
        jobs = self.bridge.store.list_jobs(statuses=[JobStatus.QUEUED])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], JobStatus.QUEUED)
        # No pipeline subprocess handle exists on the bridge anymore.
        self.assertFalse(hasattr(self.bridge, "process") and self.bridge.process)
        # The command was recorded but not executed.
        self.assertIn("run_webtoon.py", " ".join(jobs[0]["command"]))
        self.assertEqual(jobs[0]["configuration"]["job_type"], "translation")
        self.assertNotIn("community_owner_id", jobs[0]["configuration"])

    def test_authenticated_start_stamps_owner_only_from_principal(self):
        principal = RequestPrincipal("owner-a", True, auth_source="test")
        payload = {
            **self._payload(),
            "user_id": "forged-user",
            "community_owner_id": "forged-owner",
            "role": "admin",
        }
        result = _run(self.bridge.start(payload, principal=principal))
        job = self.bridge.store.get_job(result["job_id"])
        self.assertEqual(job["configuration"]["community_owner_id"], "owner-a")
        self.assertEqual(job["configuration"]["job_type"], "translation")
        self.assertNotIn("user_id", job["configuration"])
        self.assertNotIn("role", job["configuration"])

    def test_authenticated_queue_item_is_owned_by_principal(self):
        principal = RequestPrincipal("owner-a", True, auth_source="test")
        item = self.bridge.add_queue_item(self._payload(), principal=principal)
        job = self.bridge.store.get_job(item["id"])
        self.assertEqual(job["configuration"]["community_owner_id"], "owner-a")

    def test_state_comes_from_sqlite(self):
        # A job inserted straight into the store shows up in UI state.
        store = JobStore(self.db)
        store.create_job(source_url="https://example/x", output_dir=str(self.tmp / "out"),
                         command=["python", "fake.py"])
        store.close()
        state = self.bridge.runtime_state()
        self.assertEqual(len(state["queue"]), 1)
        self.assertFalse(state["worker"]["online"])

    def test_worker_online_reflected(self):
        self.bridge.store.register_worker("w1", 1234)
        state = self.bridge.runtime_state()
        self.assertTrue(state["worker"]["online"])
        self.assertEqual(state["worker"]["worker_id"], "w1")

    def test_shutdown_does_not_cancel_active_job(self):
        import asyncio
        store = JobStore(self.db)
        jid = store.create_job(source_url="https://example/x",
                               output_dir=str(self.tmp / "out"), command=["x"])
        store.claim_next_job("w1", 1)
        store.transition(jid, JobStatus.STARTING, expected_worker="w1")
        store.transition(jid, JobStatus.RUNNING, expected_worker="w1")
        store.close()
        _run(self.bridge.shutdown())
        # Re-open and confirm the job is still running and not cancelled.
        store = JobStore(self.db)
        self.assertEqual(store.get_job(jid)["status"], JobStatus.RUNNING)
        self.assertFalse(store.cancel_requested(jid))
        store.close()

    def test_cancel_sets_flag_on_active_job(self):
        import asyncio
        store = JobStore(self.db)
        jid = store.create_job(source_url="https://example/x",
                               output_dir=str(self.tmp / "out"), command=["x"])
        store.claim_next_job("w1", 1)
        store.transition(jid, JobStatus.STARTING, expected_worker="w1")
        store.transition(jid, JobStatus.RUNNING, expected_worker="w1")
        store.close()
        _run(self.bridge.cancel())
        store = JobStore(self.db)
        self.assertTrue(store.cancel_requested(jid))
        store.close()

    def test_resume_creates_new_attempt(self):
        store = JobStore(self.db)
        jid = store.create_job(source_url="https://example/x",
                               output_dir=str(self.tmp / "out"), command=["x"])
        store.claim_next_job("w1", 1)
        store.transition(jid, JobStatus.STARTING, expected_worker="w1")
        store.transition(jid, JobStatus.RUNNING, expected_worker="w1")
        store.update_fields(jid, heartbeat_at=1.0)  # epoch 1s -> very stale
        store.recover_stale(stale_seconds=1)  # -> interrupted
        store.close()
        result = self.bridge.resume(jid)
        self.assertTrue(result["ok"])
        new_job = self.bridge.store.get_job(result["job_id"])
        self.assertEqual(new_job["status"], JobStatus.QUEUED)
        self.assertEqual(new_job["attempt"], 2)
        self.assertEqual(new_job["previous_job_id"], jid)

    def test_remove_queued_job_cancels_it(self):
        store = JobStore(self.db)
        jid = store.create_job(source_url="https://example/x",
                               output_dir=str(self.tmp / "out"), command=["x"])
        store.close()
        self.bridge.remove_queue_item(jid)
        self.assertEqual(self.bridge.store.get_job(jid)["status"], JobStatus.CANCELLED)

    def test_generic_queue_controls_never_cancel_community_publish_job(self):
        jid = self.bridge.store.create_job(
            source_url="",
            output_dir=str(self.tmp / "publish"),
            command=["community_publish"],
            configuration={"job_type": "community_publish"},
        )
        self.bridge.remove_queue_item(jid)
        _run(self.bridge.cancel(queue=True))
        self.bridge.clear_queue()
        self.assertEqual(self.bridge.store.get_job(jid)["status"], JobStatus.QUEUED)

    def test_community_publish_jobs_are_hidden_from_translation_runtime_state(self):
        jid = self.bridge.store.create_job(
            source_url="",
            output_dir=str(self.tmp / "publish"),
            command=["community_publish"],
            configuration={"job_type": "community_publish"},
        )
        self.bridge.store.claim_next_job("publisher", 1)
        self.bridge.store.transition(jid, JobStatus.STARTING, expected_worker="publisher")
        self.bridge.store.transition(jid, JobStatus.RUNNING, expected_worker="publisher")
        state = self.bridge.runtime_state()
        self.assertIsNone(state["active"])
        self.assertEqual(state["status"], "ready")
        self.assertFalse(state["queue_running"])
        self.assertEqual(state["queue"], [])
        self.assertEqual(state["resumable"], [])

    def test_start_queue_rejects_queue_containing_only_community_publish(self):
        self.bridge.store.create_job(
            source_url="",
            output_dir=str(self.tmp / "publish"),
            command=["community_publish"],
            configuration={"job_type": "community_publish"},
        )
        with self.assertRaisesRegex(ValueError, "fila não tem itens"):
            _run(self.bridge.start_queue())

    def test_generic_resume_rejects_community_publish_job(self):
        jid = self.bridge.store.create_job(
            source_url="",
            output_dir=str(self.tmp / "publish"),
            command=["community_publish"],
            configuration={"job_type": "community_publish"},
        )
        self.bridge.store.claim_next_job("publisher", 1)
        self.bridge.store.transition(jid, JobStatus.STARTING, expected_worker="publisher")
        self.bridge.store.transition(jid, JobStatus.RUNNING, expected_worker="publisher")
        self.bridge.store.transition(jid, JobStatus.INTERRUPTED, expected_worker="publisher")
        with self.assertRaises(ValueError):
            self.bridge.resume(jid)

    def test_resume_blocked_while_previous_runner_alive(self):
        # An interrupted job whose recorded runner is still a live process must not be
        # resumed - that would run two trees for the same chapter.
        import os
        import process_tree
        store = JobStore(self.db)
        jid = store.create_job(source_url="https://example/x",
                               output_dir=str(self.tmp / "out"), command=["x"])
        store.claim_next_job("w1", 1)
        store.transition(jid, JobStatus.STARTING, expected_worker="w1")
        # Point runner_pid at THIS live process with a matching fingerprint, so the guard
        # sees a live runner. The command line of the test process contains "job_runner"?
        # Use a fingerprint we know is present: the running python's own args won't have
        # it, so instead assert the guard via a monkeypatched liveness check.
        store.transition(jid, JobStatus.RUNNING, expected_worker="w1",
                         runner_pid=os.getpid(), runner_create_time=process_tree.snapshot(os.getpid())["create_time"])
        store.update_fields(jid, heartbeat_at=1.0)
        store.recover_stale(stale_seconds=1)  # -> interrupted
        store.close()
        with patch.object(ui_bridge, "_runner_still_alive", lambda job: True):
            with self.assertRaises(ValueError) as ctx:
                self.bridge.resume(jid)
        self.assertIn("previous_attempt_still_running", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
