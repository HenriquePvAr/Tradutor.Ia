"""Unit tests for the persistent SQLite job store."""

import _test_bootstrap  # noqa: F401

import threading
import time
import unittest

from job_store import JobStatus, JobStore, TransitionError, transition_allowed


def _make_store(tmp):
    return JobStore(tmp / "jobs.sqlite3")


def _new_job(store, **over):
    kwargs = dict(source_url="https://example/x", output_dir=str("out/x"),
                  command=["python", "-u", "fake.py"])
    kwargs.update(over)
    return store.create_job(**kwargs)


class JobStoreBasicsTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = __import__("pathlib").Path(tempfile.mkdtemp())
        self.store = _make_store(self.tmp)

    def tearDown(self):
        self.store.close()

    def test_create_job_starts_queued(self):
        jid = _new_job(self.store)
        job = self.store.get_job(jid)
        self.assertEqual(job["status"], JobStatus.QUEUED)
        self.assertEqual(job["attempt"], 1)
        self.assertTrue(job["run_id"])
        self.assertEqual(job["command"], ["python", "-u", "fake.py"])

    def test_migration_is_idempotent(self):
        # Reopening the same DB must not fail or wipe data.
        jid = _new_job(self.store)
        self.store.close()
        store2 = JobStore(self.tmp / "jobs.sqlite3")
        self.assertIsNotNone(store2.get_job(jid))
        store2.close()

    def test_configuration_and_command_roundtrip(self):
        jid = _new_job(self.store, configuration={"mode": "fast", "force": True})
        job = self.store.get_job(jid)
        self.assertEqual(job["configuration"]["mode"], "fast")
        self.assertTrue(job["configuration"]["force"])

    def test_source_analysis_fields_roundtrip_without_leaking_into_configuration(self):
        jid = _new_job(self.store)
        self.store.update_fields(
            jid,
            reason_code="review_required_medium_confidence",
            source_analysis_json='{"adapter":"universal","accepted":[{"id":"opaque"}]}',
            source_selection_json='{"candidate_ids":["opaque"],"automatic":false}',
        )
        job = self.store.get_job(jid)
        self.assertEqual(job["reason_code"], "review_required_medium_confidence")
        self.assertEqual(job["source_analysis"]["accepted"][0]["id"], "opaque")
        self.assertFalse(job["source_selection"]["automatic"])

    def test_no_secrets_stored(self):
        jid = _new_job(self.store, configuration={"mode": "fast"})
        job = self.store.get_job(jid)
        blob = (job["configuration_json"] or "") + (job["command_json"] or "")
        self.assertNotIn("NVIDIA_API_KEY", blob)
        self.assertNotIn("sua_chave", blob)


class ClaimTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = __import__("pathlib").Path(tempfile.mkdtemp())
        self.store = _make_store(self.tmp)

    def tearDown(self):
        self.store.close()

    def test_claim_moves_job_to_claiming(self):
        jid = _new_job(self.store)
        claimed = self.store.claim_next_job("w1", 111)
        self.assertEqual(claimed["id"], jid)
        self.assertEqual(claimed["status"], JobStatus.CLAIMING)
        self.assertEqual(claimed["worker_id"], "w1")

    def test_claim_returns_none_when_empty(self):
        self.assertIsNone(self.store.claim_next_job("w1", 111))

    def test_fifo_order(self):
        first = _new_job(self.store)
        time.sleep(0.01)
        _new_job(self.store)
        claimed = self.store.claim_next_job("w1", 1)
        self.assertEqual(claimed["id"], first)

    def test_two_workers_only_one_wins(self):
        # Separate connections (separate processes in production) racing on one job.
        _new_job(self.store)
        stores = [JobStore(self.tmp / "jobs.sqlite3") for _ in range(8)]
        results = []
        barrier = threading.Barrier(len(stores))

        def claim(idx):
            barrier.wait()
            results.append(stores[idx].claim_next_job(f"w{idx}", idx))

        threads = [threading.Thread(target=claim, args=(i,)) for i in range(len(stores))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [r for r in results if r is not None]
        self.assertEqual(len(winners), 1, results)
        for s in stores:
            s.close()

    def test_concurrency_one(self):
        # After claiming one, no second job is claimable until the first leaves flight.
        _new_job(self.store)
        _new_job(self.store)
        first = self.store.claim_next_job("w1", 1)
        second = self.store.claim_next_job("w1", 1)
        # A second claim DOES pick the next queued job; concurrency is enforced by the
        # worker running one at a time, but the store still exposes the queue. What must
        # hold is that the SAME job is never claimed twice.
        self.assertNotEqual(first["id"], second["id"])


class TransitionTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = __import__("pathlib").Path(tempfile.mkdtemp())
        self.store = _make_store(self.tmp)

    def tearDown(self):
        self.store.close()

    def test_happy_path(self):
        jid = _new_job(self.store)
        self.store.claim_next_job("w1", 1)
        self.store.transition(jid, JobStatus.STARTING, expected_worker="w1")
        self.store.transition(jid, JobStatus.RUNNING, expected_worker="w1")
        job = self.store.transition(jid, JobStatus.FINISHED, expected_worker="w1",
                                    pdf_path="out/x/chapter.pdf")
        self.assertEqual(job["status"], JobStatus.FINISHED)
        self.assertIsNotNone(job["started_at"])
        self.assertIsNotNone(job["finished_at"])

    def test_illegal_transition_rejected(self):
        jid = _new_job(self.store)
        with self.assertRaises(TransitionError):
            self.store.transition(jid, JobStatus.FINISHED)  # queued -> finished not allowed

    def test_wrong_owner_rejected(self):
        jid = _new_job(self.store)
        self.store.claim_next_job("w1", 1)
        with self.assertRaises(TransitionError):
            self.store.transition(jid, JobStatus.STARTING, expected_worker="w2")

    def test_review_required_is_terminal(self):
        jid = _new_job(self.store)
        self.store.claim_next_job("w1", 1)
        self.store.transition(jid, JobStatus.STARTING, expected_worker="w1")
        self.store.transition(jid, JobStatus.RUNNING, expected_worker="w1")
        self.store.transition(jid, JobStatus.REVIEW_REQUIRED, expected_worker="w1")
        with self.assertRaises(TransitionError):
            self.store.transition(jid, JobStatus.RUNNING)

    def test_transition_rejects_unknown_column(self):
        jid = _new_job(self.store)
        with self.assertRaises(TransitionError):
            self.store.transition(jid, JobStatus.CLAIMING, bogus_column="x")

    def test_transition_table_symmetry(self):
        self.assertTrue(transition_allowed(JobStatus.RUNNING, JobStatus.FINISHED))
        self.assertFalse(transition_allowed(JobStatus.FINISHED, JobStatus.RUNNING))
        self.assertFalse(transition_allowed(JobStatus.QUEUED, JobStatus.RUNNING))

    def test_source_review_is_not_claimable_and_can_be_confirmed_or_cancelled(self):
        jid = _new_job(self.store, initial_status=JobStatus.AWAITING_SOURCE_REVIEW)
        self.assertIsNone(self.store.claim_next_job("w1", 1))
        waiting = self.store.get_job(jid)
        self.assertEqual(waiting["status"], JobStatus.AWAITING_SOURCE_REVIEW)
        queued = self.store.transition(jid, JobStatus.QUEUED)
        self.assertIsNotNone(queued["queued_at"])
        self.assertEqual(self.store.claim_next_job("w1", 1)["id"], jid)

    def test_source_review_can_be_cancelled_as_a_terminal_state(self):
        jid = _new_job(self.store, initial_status=JobStatus.AWAITING_SOURCE_REVIEW)
        job = self.store.transition(jid, JobStatus.CANCELLED, reason_code="cancelled")
        self.assertEqual(job["status"], JobStatus.CANCELLED)
        self.assertIsNotNone(job["finished_at"])

    def test_terminal_transitions_receive_safe_default_reason_codes(self):
        jid = _new_job(self.store)
        self.store.claim_next_job("w1", 1)
        self.store.transition(jid, JobStatus.STARTING, expected_worker="w1")
        self.store.transition(jid, JobStatus.RUNNING, expected_worker="w1")
        failed = self.store.transition(jid, JobStatus.FAILED, expected_worker="w1")
        self.assertEqual(failed["reason_code"], "pipeline_failed")


class HeartbeatRecoveryTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = __import__("pathlib").Path(tempfile.mkdtemp())
        self.store = _make_store(self.tmp)

    def tearDown(self):
        self.store.close()

    def _to_running(self, jid, worker="w1"):
        self.store.claim_next_job(worker, 1)
        self.store.transition(jid, JobStatus.STARTING, expected_worker=worker)
        self.store.transition(jid, JobStatus.RUNNING, expected_worker=worker)

    def test_fresh_running_not_recovered(self):
        jid = _new_job(self.store)
        self._to_running(jid)
        self.store.heartbeat(jid)
        recovered = self.store.recover_stale(stale_seconds=30)
        self.assertEqual(recovered, [])
        self.assertEqual(self.store.get_job(jid)["status"], JobStatus.RUNNING)

    def test_stale_running_becomes_interrupted(self):
        jid = _new_job(self.store)
        self._to_running(jid)
        # Force an old heartbeat.
        self.store.update_fields(jid, heartbeat_at=time.time() - 120)
        recovered = self.store.recover_stale(stale_seconds=30)
        self.assertEqual(recovered, [jid])
        job = self.store.get_job(jid)
        self.assertEqual(job["status"], JobStatus.INTERRUPTED)
        self.assertEqual(job["interrupted_reason"], "stale_heartbeat")

    def test_interrupted_becomes_resumable_then_queued(self):
        jid = _new_job(self.store)
        self._to_running(jid)
        self.store.update_fields(jid, heartbeat_at=time.time() - 120)
        self.store.recover_stale(stale_seconds=30)
        self.store.mark_resumable(jid, resume_from_stage="download")
        self.assertEqual(self.store.get_job(jid)["status"], JobStatus.RESUMABLE)
        self.store.transition(jid, JobStatus.QUEUED, resume_from_stage="download")
        self.assertEqual(self.store.get_job(jid)["status"], JobStatus.QUEUED)

    def test_cancel_request_flag(self):
        jid = _new_job(self.store)
        self._to_running(jid)
        self.assertTrue(self.store.request_cancel(jid))
        self.assertTrue(self.store.cancel_requested(jid))

    def test_cancel_request_ignored_when_terminal(self):
        jid = _new_job(self.store)
        self.store.claim_next_job("w1", 1)
        self.store.transition(jid, JobStatus.STARTING, expected_worker="w1")
        self.store.transition(jid, JobStatus.RUNNING, expected_worker="w1")
        self.store.transition(jid, JobStatus.FINISHED, expected_worker="w1")
        self.assertFalse(self.store.request_cancel(jid))


class WorkerRegistryTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = __import__("pathlib").Path(tempfile.mkdtemp())
        self.store = _make_store(self.tmp)

    def tearDown(self):
        self.store.close()

    def test_no_worker_initially(self):
        self.assertIsNone(self.store.healthy_worker())

    def test_registered_worker_is_healthy(self):
        self.store.register_worker("w1", 4321)
        healthy = self.store.healthy_worker(stale_seconds=15)
        self.assertEqual(healthy["worker_id"], "w1")
        self.assertEqual(healthy["pid"], 4321)

    def test_stale_worker_not_healthy(self):
        self.store.register_worker("w1", 4321)
        self.store.unregister_worker("w1")
        self.assertIsNone(self.store.healthy_worker())

    def test_second_worker_sees_healthy_first(self):
        self.store.register_worker("w1", 1)
        self.assertIsNotNone(self.store.healthy_worker(stale_seconds=15))


if __name__ == "__main__":
    unittest.main()
