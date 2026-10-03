"""community_publish job: upload via worker, resume, cancel, verification — offline."""

import _test_bootstrap  # noqa: F401

import subprocess
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import community_publish_runner
import process_tree
from community_auth import RequestPrincipal
from community_storage import FilesystemStorageProvider, StorageError
from community_store import CommunityStore, FileStatus, PostStatus
from community_service import CommunityService
from job_store import JobStatus, JobStore
from worker_service import Worker

REPO = Path(__file__).resolve().parent
OWNER = RequestPrincipal("local", True, auth_source="test", session_id="test-owner")


def _write_pdf(path: Path, size=5000):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = b"%PDF-1.4\n" + b"a" * size + b"\n%%EOF\n"
    path.write_bytes(body)
    return path


class CommunityPublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.output_root = self.tmp / "output"
        (self.output_root / "chap").mkdir(parents=True)
        _write_pdf(self.output_root / "chap" / "chapter.pdf")
        (self.output_root / "chap" / "run_manifest.json").write_text(
            '{"pdf_filename":"chapter.pdf"}', encoding="utf-8")
        self.storage_root = self.tmp / "drive"
        self.community_db = self.tmp / "community.sqlite3"
        self.jobs_db = self.tmp / "jobs.sqlite3"
        self.store = CommunityStore(self.community_db)
        self.jobs = JobStore(self.jobs_db)
        self.svc = CommunityService(
            self.store, self.jobs, output_root=self.output_root, provider_name="filesystem",
            community_db_path=str(self.community_db),
            storage_config={"storage_root": str(self.storage_root)})

    def tearDown(self):
        self.store.close()
        self.jobs.close()

    def _draft_and_publish(self):
        draft = self.svc.create_draft(principal=OWNER, output_dir=str(self.output_root / "chap"),
                                      series_slug="chap", episode_number="1", series_title="Chap")
        return self.svc.request_publish(draft["post_id"], principal=OWNER)

    def test_runner_uploads_verifies_and_publishes(self):
        pub = self._draft_and_publish()
        # Claim the job as the worker would, then run the community runner in-process.
        self.jobs.claim_next_job("w1", 1)
        self.jobs.transition(pub["job_id"], JobStatus.STARTING, expected_worker="w1")
        rc = community_publish_runner.run_job(pub["job_id"], str(self.jobs_db))
        self.assertEqual(rc, 0)
        self.assertEqual(self.jobs.get_job(pub["job_id"])["status"], JobStatus.FINISHED)
        post = self.store.get_post(pub["post_id"])
        self.assertEqual(post["status"], PostStatus.PUBLISHED)
        file = self.store.get_file(pub["file_id"])
        self.assertEqual(file["upload_status"], FileStatus.VERIFIED)
        self.assertTrue(file["storage_file_id"])
        # The uploaded bytes match the local PDF exactly.
        provider = FilesystemStorageProvider(self.storage_root)
        meta = provider.stat_file(file["storage_file_id"])
        self.assertEqual(meta.size, file["size_bytes"])

    def test_verification_mismatch_fails_without_publishing(self):
        pub = self._draft_and_publish()
        self.jobs.claim_next_job("w1", 1)
        self.jobs.transition(pub["job_id"], JobStatus.STARTING, expected_worker="w1")
        # Corrupt the stored size after upload by patching stat via a corrupt provider.
        import community_storage
        real = community_storage.FilesystemStorageProvider.stat_file

        def corrupt(self, file_id):
            meta = real(self, file_id)
            meta.size -= 1
            return meta

        community_storage.FilesystemStorageProvider.stat_file = corrupt
        try:
            community_publish_runner.run_job(pub["job_id"], str(self.jobs_db))
        finally:
            community_storage.FilesystemStorageProvider.stat_file = real
        self.assertEqual(self.jobs.get_job(pub["job_id"])["status"], JobStatus.FAILED)
        self.assertNotEqual(self.store.get_post(pub["post_id"])["status"], PostStatus.PUBLISHED)

    def test_worker_runs_community_publish_by_type(self):
        pub = self._draft_and_publish()
        worker = Worker(self.jobs_db, poll_seconds=0.05, stale_seconds=10)
        try:
            worker.run(once=True)
        finally:
            worker.close()
        self.assertEqual(self.jobs.get_job(pub["job_id"])["status"], JobStatus.FINISHED)
        self.assertEqual(self.store.get_post(pub["post_id"])["status"], PostStatus.PUBLISHED)

    def test_worker_recovers_interrupted_publish_without_generic_ui_resume(self):
        pub = self._draft_and_publish()
        self.jobs.claim_next_job("dead-worker", 1)
        self.jobs.transition(pub["job_id"], JobStatus.STARTING, expected_worker="dead-worker")
        self.jobs.transition(pub["job_id"], JobStatus.RUNNING, expected_worker="dead-worker")
        self.jobs.transition(
            pub["job_id"],
            JobStatus.INTERRUPTED,
            expected_worker="dead-worker",
            interrupted_reason="test_crash",
            recoverable=1,
        )

        worker = Worker(self.jobs_db, poll_seconds=0.05, stale_seconds=10)
        try:
            self.assertEqual(
                worker._runner_fingerprint(pub["job_id"])[0],
                "community_publish_runner.py",
            )
            worker.run(once=True)
        finally:
            worker.close()

        job = self.jobs.get_job(pub["job_id"])
        self.assertEqual(job["status"], JobStatus.FINISHED)
        self.assertEqual(job["attempt"], 1)
        self.assertEqual(self.store.get_post(pub["post_id"])["status"], PostStatus.PUBLISHED)
        self.assertEqual(self.store.get_file(pub["file_id"])["upload_status"], FileStatus.VERIFIED)

    def test_worker_cancels_invalidated_interrupted_publish_without_spawning(self):
        pub = self._draft_and_publish()
        self.jobs.claim_next_job("dead-worker", 1)
        self.jobs.transition(pub["job_id"], JobStatus.STARTING, expected_worker="dead-worker")
        self.jobs.transition(pub["job_id"], JobStatus.RUNNING, expected_worker="dead-worker")
        self.jobs.transition(
            pub["job_id"],
            JobStatus.INTERRUPTED,
            expected_worker="dead-worker",
            interrupted_reason="test_crash",
            recoverable=1,
        )
        self.svc.unpublish(pub["post_id"], principal=OWNER)

        worker = Worker(self.jobs_db, poll_seconds=0.05, stale_seconds=10)
        try:
            with patch.object(
                worker,
                "_spawn_runner",
                side_effect=AssertionError("invalidated publish must not spawn"),
            ) as spawn:
                worker.run(once=True)
            spawn.assert_not_called()
        finally:
            worker.close()

        self.assertEqual(self.jobs.get_job(pub["job_id"])["status"], JobStatus.CANCELLED)
        self.assertEqual(self.store.get_post(pub["post_id"])["status"], PostStatus.UNPUBLISHED)
        self.assertEqual(self.store.get_file(pub["file_id"])["upload_status"], FileStatus.FAILED)

    def test_resumable_publish_is_requeued_by_trusted_worker_path(self):
        pub = self._draft_and_publish()
        self.jobs.claim_next_job("dead-worker", 1)
        self.jobs.transition(pub["job_id"], JobStatus.STARTING, expected_worker="dead-worker")
        self.jobs.transition(pub["job_id"], JobStatus.RUNNING, expected_worker="dead-worker")
        self.jobs.transition(
            pub["job_id"], JobStatus.INTERRUPTED, expected_worker="dead-worker",
            recoverable=1,
        )
        self.jobs.mark_resumable(pub["job_id"], resume_from_stage="uploading")
        worker = Worker(self.jobs_db, poll_seconds=0.01, stale_seconds=10)
        try:
            self.assertTrue(worker._recover_interrupted_community_publishes())
        finally:
            worker.close()
        self.assertEqual(self.jobs.get_job(pub["job_id"])["status"], JobStatus.QUEUED)

    def test_transient_community_db_error_keeps_publish_recoverable(self):
        pub = self._draft_and_publish()
        self.jobs.claim_next_job("dead-worker", 1)
        self.jobs.transition(pub["job_id"], JobStatus.STARTING, expected_worker="dead-worker")
        self.jobs.transition(pub["job_id"], JobStatus.RUNNING, expected_worker="dead-worker")
        self.jobs.transition(
            pub["job_id"], JobStatus.INTERRUPTED, expected_worker="dead-worker",
            recoverable=1,
        )
        worker = Worker(self.jobs_db, poll_seconds=0.01, stale_seconds=10)
        try:
            with patch(
                "community_store.CommunityStore",
                side_effect=sqlite3.OperationalError("temporarily locked"),
            ):
                self.assertFalse(worker._recover_interrupted_community_publishes())
            self.assertEqual(
                self.jobs.get_job(pub["job_id"])["status"], JobStatus.INTERRUPTED)
            self.assertEqual(
                self.store.get_post(pub["post_id"])["status"], PostStatus.PUBLISHING)
            self.assertTrue(self.store.active_publish_exists(pub["post_id"]))
            self.assertTrue(worker._recover_interrupted_community_publishes())
        finally:
            worker.close()
        self.assertEqual(self.jobs.get_job(pub["job_id"])["status"], JobStatus.QUEUED)

    def test_recovery_reconciles_already_committed_publish_as_finished(self):
        pub = self._draft_and_publish()
        self.jobs.claim_next_job("dead-worker", 1)
        self.jobs.transition(pub["job_id"], JobStatus.STARTING, expected_worker="dead-worker")
        self.jobs.transition(pub["job_id"], JobStatus.RUNNING, expected_worker="dead-worker")
        self.store.update_file(
            pub["file_id"],
            upload_status=FileStatus.VERIFYING,
            storage_file_id="remote-completed",
        )
        self.assertTrue(self.store.complete_publish_attempt(
            post_id=pub["post_id"],
            file_id=pub["file_id"],
            upload_job_id=pub["job_id"],
            provider_checksum="checksum",
            actor_id="dead-worker",
            size=self.store.get_file(pub["file_id"])["size_bytes"],
        ))
        self.jobs.transition(
            pub["job_id"], JobStatus.INTERRUPTED, expected_worker="dead-worker",
            recoverable=1,
        )
        worker = Worker(self.jobs_db, poll_seconds=0.01, stale_seconds=10)
        try:
            with patch.object(worker, "_spawn_runner") as spawn:
                worker.run(once=True)
            spawn.assert_not_called()
        finally:
            worker.close()
        self.assertEqual(self.jobs.get_job(pub["job_id"])["status"], JobStatus.FINISHED)
        self.assertEqual(self.store.get_post(pub["post_id"])["status"], PostStatus.PUBLISHED)
        self.assertEqual(self.store.get_file(pub["file_id"])["upload_status"], FileStatus.VERIFIED)

    def test_recovery_reconciles_already_committed_failure_as_failed(self):
        pub = self._draft_and_publish()
        self.jobs.claim_next_job("dead-worker", 1)
        self.jobs.transition(pub["job_id"], JobStatus.STARTING, expected_worker="dead-worker")
        self.jobs.transition(pub["job_id"], JobStatus.RUNNING, expected_worker="dead-worker")
        self.assertTrue(self.store.fail_publish_attempt(
            post_id=pub["post_id"],
            file_id=pub["file_id"],
            upload_job_id=pub["job_id"],
            actor_id="dead-worker",
            reason="simulated_failure_commit",
        ))
        self.jobs.transition(
            pub["job_id"], JobStatus.INTERRUPTED, expected_worker="dead-worker",
            recoverable=1,
        )
        worker = Worker(self.jobs_db, poll_seconds=0.01, stale_seconds=10)
        try:
            with patch.object(worker, "_spawn_runner") as spawn:
                worker.run(once=True)
            spawn.assert_not_called()
        finally:
            worker.close()
        self.assertEqual(self.jobs.get_job(pub["job_id"])["status"], JobStatus.FAILED)
        self.assertEqual(self.store.get_post(pub["post_id"])["status"], PostStatus.FAILED)
        self.assertEqual(self.store.get_file(pub["file_id"])["upload_status"], FileStatus.FAILED)

    def test_recovery_deletes_partial_remote_before_retrying_same_job(self):
        pdf_path = self.output_root / "chap" / "chapter.pdf"
        _write_pdf(pdf_path, size=community_publish_runner.CHUNK_SIZE * 2 + 31)
        pub = self._draft_and_publish()
        self.jobs.claim_next_job("w1", 1)
        self.jobs.transition(pub["job_id"], JobStatus.STARTING, expected_worker="w1")
        inner = FilesystemStorageProvider(self.storage_root)

        class InterruptAfterFirstChunk:
            def __init__(self):
                self.calls = 0

            def __getattr__(self, name):
                return getattr(inner, name)

            def upload_chunk(self, session, offset, data):
                result = inner.upload_chunk(session, offset, data)
                self.calls += 1
                if self.calls == 1:
                    community_publish_runner._STOP = True
                return result

        with patch.object(
            community_publish_runner,
            "build_storage_provider",
            return_value=InterruptAfterFirstChunk(),
        ):
            self.assertEqual(
                community_publish_runner.run_job(pub["job_id"], str(self.jobs_db)), 0)
        self.assertEqual(self.jobs.get_job(pub["job_id"])["status"], JobStatus.INTERRUPTED)
        self.assertEqual(len(list((self.storage_root / "files").glob("*.bin"))), 1)

        worker = Worker(self.jobs_db, poll_seconds=0.01, stale_seconds=10)
        try:
            worker.run(once=True)
        finally:
            worker.close()
        self.assertEqual(self.jobs.get_job(pub["job_id"])["status"], JobStatus.FINISHED)
        self.assertEqual(len(list((self.storage_root / "files").glob("*.bin"))), 1)
        self.assertEqual(len(list((self.storage_root / "sessions").glob("*.json"))), 1)

    def test_once_returns_when_old_runner_cannot_be_reconciled(self):
        worker = Worker(self.jobs_db, poll_seconds=0.01, stale_seconds=10)
        try:
            with patch.object(worker, "_reconcile_stale", return_value=False), patch(
                "worker_service.time.sleep",
                side_effect=AssertionError("--once must not poll forever"),
            ):
                worker.run(once=True)
        finally:
            worker.close()

    def test_transient_errors_retry(self):
        # Point the runner's provider at a fake that fails a few chunks transiently.
        pub = self._draft_and_publish()
        self.jobs.claim_next_job("w1", 1)
        self.jobs.transition(pub["job_id"], JobStatus.STARTING, expected_worker="w1")
        import community_storage
        real = community_storage.build_storage_provider

        def flaky(config):
            return community_storage.FakeStorageProvider(transient_failures=3)

        community_storage.build_storage_provider = flaky
        community_publish_runner.build_storage_provider = flaky
        try:
            rc = community_publish_runner.run_job(pub["job_id"], str(self.jobs_db))
        finally:
            community_storage.build_storage_provider = real
            community_publish_runner.build_storage_provider = real
        self.assertEqual(rc, 0)
        self.assertEqual(self.jobs.get_job(pub["job_id"])["status"], JobStatus.FINISHED)

    def test_unpublish_queued_attempt_cancels_before_provider_construction(self):
        pub = self._draft_and_publish()
        self.svc.unpublish(pub["post_id"], principal=OWNER)
        self.assertEqual(self.store.get_file(pub["file_id"])["upload_status"], FileStatus.FAILED)
        self.assertFalse(self.store.active_publish_exists(pub["post_id"]))
        self.assertTrue(self.jobs.cancel_requested(pub["job_id"]))
        self.jobs.claim_next_job("w1", 1)
        self.jobs.transition(pub["job_id"], JobStatus.STARTING, expected_worker="w1")
        with patch.object(
            community_publish_runner,
            "build_storage_provider",
            side_effect=AssertionError("provider must not be built for invalidated publish"),
        ):
            rc = community_publish_runner.run_job(pub["job_id"], str(self.jobs_db))
        self.assertEqual(rc, 0)
        self.assertEqual(self.jobs.get_job(pub["job_id"])["status"], JobStatus.CANCELLED)
        self.assertEqual(self.store.get_post(pub["post_id"])["status"], PostStatus.UNPUBLISHED)
        self.assertEqual(self.store.get_file(pub["file_id"])["upload_status"], FileStatus.FAILED)

    def test_unpublish_during_final_verification_cannot_be_overwritten(self):
        pub = self._draft_and_publish()
        self.jobs.claim_next_job("w1", 1)
        self.jobs.transition(pub["job_id"], JobStatus.STARTING, expected_worker="w1")
        real_build = community_publish_runner.build_storage_provider
        owner_service = self.svc

        class UnpublishingProvider:
            def __init__(self, inner):
                self.inner = inner

            def __getattr__(self, name):
                return getattr(self.inner, name)

            def stat_file(self, file_id):
                metadata = self.inner.stat_file(file_id)
                owner_service.unpublish(pub["post_id"], principal=OWNER)
                return metadata

        with patch.object(
            community_publish_runner,
            "build_storage_provider",
            side_effect=lambda config: UnpublishingProvider(real_build(config)),
        ):
            rc = community_publish_runner.run_job(pub["job_id"], str(self.jobs_db))
        self.assertEqual(rc, 0)
        self.assertEqual(self.jobs.get_job(pub["job_id"])["status"], JobStatus.CANCELLED)
        self.assertEqual(self.store.get_post(pub["post_id"])["status"], PostStatus.UNPUBLISHED)
        self.assertEqual(self.store.get_file(pub["file_id"])["upload_status"], FileStatus.FAILED)

    def test_publish_linkage_exists_before_job_can_be_claimed(self):
        draft = self.svc.create_draft(
            principal=OWNER,
            output_dir=str(self.output_root / "chap"),
            series_slug="chap",
            episode_number="1",
            series_title="Chap",
        )
        real_create = self.jobs.create_job
        observed = []

        def create_and_claim_immediately(**kwargs):
            job_id = real_create(**kwargs)
            claimed = self.jobs.claim_next_job("instant-worker", 1)
            observed.append((job_id, kwargs["initial_status"], claimed))
            return job_id

        with patch.object(self.jobs, "create_job", side_effect=create_and_claim_immediately):
            pub = self.svc.request_publish(draft["post_id"], principal=OWNER)
        self.assertEqual(observed, [(pub["job_id"], JobStatus.STAGING, None)])
        self.assertEqual(self.jobs.get_job(pub["job_id"])["status"], JobStatus.QUEUED)
        self.assertTrue(self.store.publish_attempt_is_current(
            pub["post_id"], pub["file_id"], pub["job_id"]))
        linked = self.store.get_file(pub["file_id"])
        self.assertEqual(linked["upload_job_id"], pub["job_id"])
        self.assertEqual(self.store.get_post(pub["post_id"])["status"], PostStatus.PUBLISHING)

    def test_same_size_pdf_substitution_fails_hash_verification(self):
        pub = self._draft_and_publish()
        pdf_path = self.output_root / "chap" / "chapter.pdf"
        changed = bytearray(pdf_path.read_bytes())
        changed[-8] = ord("z") if changed[-8] != ord("z") else ord("y")
        pdf_path.write_bytes(changed)
        self.jobs.claim_next_job("w1", 1)
        self.jobs.transition(pub["job_id"], JobStatus.STARTING, expected_worker="w1")
        rc = community_publish_runner.run_job(pub["job_id"], str(self.jobs_db))
        self.assertEqual(rc, 0)
        self.assertEqual(self.jobs.get_job(pub["job_id"])["status"], JobStatus.FAILED)
        self.assertEqual(self.store.get_post(pub["post_id"])["status"], PostStatus.FAILED)
        file = self.store.get_file(pub["file_id"])
        self.assertEqual(file["upload_status"], FileStatus.FAILED)
        self.assertFalse(file["storage_file_id"])
        self.assertFalse(
            self.storage_root.exists()
            and any(self.storage_root.rglob("*.bin")),
            "rejected local bytes must never reach storage",
        )

    def test_provider_construction_failure_terminalizes_attempt_without_secret_text(self):
        pub = self._draft_and_publish()
        self.jobs.claim_next_job("w1", 1)
        self.jobs.transition(pub["job_id"], JobStatus.STARTING, expected_worker="w1")
        with patch.object(
            community_publish_runner,
            "build_storage_provider",
            side_effect=StorageError(
                "secret-session-url-must-not-be-persisted",
                transient=True,
                status=503,
            ),
        ):
            rc = community_publish_runner.run_job(pub["job_id"], str(self.jobs_db))
        self.assertEqual(rc, 0)
        job = self.jobs.get_job(pub["job_id"])
        self.assertEqual(job["status"], JobStatus.FAILED)
        self.assertEqual(job["error_message"], "storage_error:503")
        self.assertNotIn("secret-session", str(job))
        self.assertEqual(self.store.get_post(pub["post_id"])["status"], PostStatus.FAILED)
        self.assertEqual(self.store.get_file(pub["file_id"])["upload_status"], FileStatus.FAILED)
        self.assertFalse(self.store.active_publish_exists(pub["post_id"]))

    def test_community_db_write_failure_keeps_job_recoverable_not_half_failed(self):
        pub = self._draft_and_publish()
        self.jobs.claim_next_job("w1", 1)
        self.jobs.transition(pub["job_id"], JobStatus.STARTING, expected_worker="w1")
        with patch.object(
            community_publish_runner,
            "build_storage_provider",
            side_effect=StorageError("offline", transient=True, status=503),
        ), patch.object(
            CommunityStore,
            "fail_publish_attempt",
            side_effect=sqlite3.OperationalError("temporarily locked"),
        ):
            rc = community_publish_runner.run_job(pub["job_id"], str(self.jobs_db))
        self.assertEqual(rc, 0)
        job = self.jobs.get_job(pub["job_id"])
        self.assertEqual(job["status"], JobStatus.INTERRUPTED)
        self.assertTrue(job["recoverable"])
        self.assertEqual(job["error_message"], "community_state_unavailable")
        self.assertEqual(self.store.get_post(pub["post_id"])["status"], PostStatus.PUBLISHING)
        self.assertEqual(self.store.get_file(pub["file_id"])["upload_status"], FileStatus.PENDING)

        worker = Worker(self.jobs_db, poll_seconds=0.01, stale_seconds=10)
        try:
            self.assertTrue(worker._recover_interrupted_community_publishes())
        finally:
            worker.close()
        self.assertEqual(self.jobs.get_job(pub["job_id"])["status"], JobStatus.QUEUED)

    def test_terminal_community_state_survives_transient_job_reconcile_failure(self):
        pub = self._draft_and_publish()
        worker = Worker(self.jobs_db, poll_seconds=0.01, stale_seconds=10)
        try:
            self.jobs.claim_next_job(worker.worker_id, 1)
            self.jobs.transition(
                pub["job_id"], JobStatus.STARTING, expected_worker=worker.worker_id)
            self.jobs.transition(
                pub["job_id"], JobStatus.RUNNING, expected_worker=worker.worker_id)
            self.store.update_file(
                pub["file_id"],
                upload_status=FileStatus.VERIFYING,
                storage_file_id="already-committed",
            )
            self.assertTrue(self.store.complete_publish_attempt(
                post_id=pub["post_id"],
                file_id=pub["file_id"],
                upload_job_id=pub["job_id"],
                provider_checksum="checksum",
                actor_id=OWNER.user_id,
                size=self.store.get_file(pub["file_id"])["size_bytes"],
            ))
            job = self.jobs.get_job(pub["job_id"])
            with patch.object(
                self.jobs,
                "reconcile_community_publish_terminal",
                side_effect=sqlite3.OperationalError("jobs database busy"),
            ):
                community_publish_runner._fail_unexpected(
                    self.jobs,
                    self.store,
                    pub["job_id"],
                    pub["post_id"],
                    pub["file_id"],
                    job,
                    RuntimeError("late runner error"),
                )
            self.assertEqual(
                self.jobs.get_job(pub["job_id"])["status"], JobStatus.RUNNING)
            self.assertEqual(
                self.store.get_post(pub["post_id"])["status"], PostStatus.PUBLISHED)
            self.assertEqual(
                self.store.get_file(pub["file_id"])["upload_status"], FileStatus.VERIFIED)

            worker._reconcile_runner_exit(pub["job_id"])
            self.assertEqual(
                self.jobs.get_job(pub["job_id"])["status"], JobStatus.INTERRUPTED)
            self.assertTrue(worker._recover_interrupted_community_publishes())
        finally:
            worker.close()
        self.assertEqual(self.jobs.get_job(pub["job_id"])["status"], JobStatus.FINISHED)

    def test_spawn_pid_persistence_failure_terminates_untracked_child(self):
        job_id = self.jobs.create_job(
            source_url="https://example.invalid/offline",
            output_dir=str(self.output_root / "spawn-window"),
            command=["offline"],
            configuration={"job_type": "translation"},
        )
        worker = Worker(self.jobs_db, poll_seconds=0.01, stale_seconds=10)
        claimed = self.jobs.claim_next_job(worker.worker_id, 1)
        self.assertEqual(claimed["id"], job_id)
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            with patch.object(worker, "_spawn_runner", return_value=proc), patch.object(
                worker.store,
                "update_fields",
                side_effect=sqlite3.OperationalError("pid write failed"),
            ):
                worker._run_one(claimed)
            self.assertIsNotNone(proc.poll())
            self.assertIsNone(worker._active)
            self.assertEqual(
                self.jobs.get_job(job_id)["status"], JobStatus.INTERRUPTED)
            self.assertFalse(self.jobs.get_job(job_id)["runner_pid"])
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            worker.close()

    def test_runner_early_exit_interrupts_job_owned_by_healthy_worker(self):
        job_id = self.jobs.create_job(
            source_url="https://example.invalid/offline",
            output_dir=str(self.output_root / "early-exit"),
            command=["offline"],
            configuration={"job_type": "translation"},
        )
        worker = Worker(self.jobs_db, poll_seconds=0.01, stale_seconds=10)
        claimed = self.jobs.claim_next_job(worker.worker_id, 1)
        self.assertEqual(claimed["id"], job_id)
        proc = subprocess.Popen(
            [sys.executable, "-c", "raise SystemExit(3)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            with patch.object(worker, "_spawn_runner", return_value=proc):
                worker._run_one(claimed)
            job = self.jobs.get_job(job_id)
            self.assertEqual(job["status"], JobStatus.INTERRUPTED)
            self.assertFalse(job["recoverable"])
            self.assertEqual(
                job["interrupted_reason"], "runner_exited_before_terminal")
            self.assertIsNone(worker._active)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            worker.close()

    def test_prestart_community_runner_exit_does_not_respawn_loop(self):
        pub = self._draft_and_publish()
        worker = Worker(self.jobs_db, poll_seconds=0.01, stale_seconds=10)
        spawns = []

        def exit_before_start(_job):
            spawns.append(1)
            return subprocess.Popen(
                [sys.executable, "-c", "raise SystemExit(3)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        try:
            with patch.object(worker, "_spawn_runner", side_effect=exit_before_start):
                worker.run(max_idle_cycles=3)
        finally:
            worker.close()
        self.assertEqual(len(spawns), 1)
        self.assertEqual(self.jobs.get_job(pub["job_id"])["status"], JobStatus.FAILED)
        self.assertEqual(self.store.get_post(pub["post_id"])["status"], PostStatus.FAILED)
        self.assertEqual(self.store.get_file(pub["file_id"])["upload_status"], FileStatus.FAILED)

    def test_runner_exit_recovery_is_delayed_and_bounded(self):
        pub = self._draft_and_publish()
        self.jobs.claim_next_job("crashed-worker", 1)
        self.jobs.transition(pub["job_id"], JobStatus.STARTING, expected_worker="crashed-worker")
        self.jobs.transition(pub["job_id"], JobStatus.RUNNING, expected_worker="crashed-worker")
        self.jobs.transition(
            pub["job_id"],
            JobStatus.INTERRUPTED,
            expected_worker="crashed-worker",
            interrupted_reason="runner_exited_before_terminal",
            recoverable=1,
        )
        worker = Worker(self.jobs_db, poll_seconds=0.01, stale_seconds=10)
        try:
            self.assertTrue(worker._recover_interrupted_community_publishes())
            delayed = self.jobs.get_job(pub["job_id"])
            self.assertEqual(delayed["status"], JobStatus.QUEUED)
            self.assertEqual(delayed["attempt"], 2)
            self.assertGreater(delayed["queued_at"], time.time())
            self.assertIsNone(self.jobs.claim_next_job("too-early", 2))

            self.jobs.update_fields(
                pub["job_id"],
                status=JobStatus.INTERRUPTED,
                worker_id="crashed-again",
                interrupted_reason="runner_exited_before_terminal",
                recoverable=1,
                attempt=3,
            )
            self.assertTrue(worker._recover_interrupted_community_publishes())
        finally:
            worker.close()
        self.assertEqual(self.jobs.get_job(pub["job_id"])["status"], JobStatus.FAILED)
        self.assertEqual(self.store.get_post(pub["post_id"])["status"], PostStatus.FAILED)

    def test_missing_local_pdf_fails_before_provider_construction(self):
        pub = self._draft_and_publish()
        (self.output_root / "chap" / "chapter.pdf").unlink()
        self.jobs.claim_next_job("w1", 1)
        self.jobs.transition(pub["job_id"], JobStatus.STARTING, expected_worker="w1")
        with patch.object(community_publish_runner, "build_storage_provider") as build:
            rc = community_publish_runner.run_job(pub["job_id"], str(self.jobs_db))
        self.assertEqual(rc, 0)
        build.assert_not_called()
        job = self.jobs.get_job(pub["job_id"])
        self.assertEqual(job["status"], JobStatus.FAILED)
        self.assertEqual(job["error_message"], "local_pdf_io_error")
        self.assertEqual(self.store.get_post(pub["post_id"])["status"], PostStatus.FAILED)
        self.assertFalse(self.store.active_publish_exists(pub["post_id"]))

    def test_enqueue_failure_does_not_overwrite_concurrent_unpublish(self):
        draft = self.svc.create_draft(
            principal=OWNER,
            output_dir=str(self.output_root / "chap"),
            series_slug="chap",
            episode_number="1",
            series_title="Chap",
        )

        def unpublish_then_fail(**_kwargs):
            self.svc.unpublish(draft["post_id"], principal=OWNER)
            raise RuntimeError("simulated queue insert failure")

        with patch.object(self.jobs, "create_job", side_effect=unpublish_then_fail):
            with self.assertRaisesRegex(RuntimeError, "queue insert failure"):
                self.svc.request_publish(draft["post_id"], principal=OWNER)

        post = self.store.get_post(draft["post_id"])
        file = self.store.file_for_post(draft["post_id"])
        events = [event["event_type"] for event in self.store.events_for_post(draft["post_id"])]
        self.assertEqual(post["status"], PostStatus.UNPUBLISHED)
        self.assertIsNone(file)
        self.assertNotIn("status_failed", events)
        self.assertNotIn("publish_failed", events)

    def test_staged_linked_publish_is_recovered_to_queue_after_api_crash(self):
        draft = self.svc.create_draft(
            principal=OWNER,
            output_dir=str(self.output_root / "chap"),
            series_slug="chap",
        )
        real_transition = self.jobs.transition

        def fail_first_queue(job_id, target, **fields):
            if target == JobStatus.QUEUED:
                raise sqlite3.OperationalError("simulated API crash before queue")
            return real_transition(job_id, target, **fields)

        with patch.object(self.jobs, "transition", side_effect=fail_first_queue):
            with self.assertRaisesRegex(sqlite3.OperationalError, "before queue"):
                self.svc.request_publish(draft["post_id"], principal=OWNER)
        staged = self.jobs.list_jobs(statuses=[JobStatus.STAGING], limit=None)
        self.assertEqual(len(staged), 1)
        config = staged[0]["configuration"]
        self.assertTrue(self.store.publish_attempt_is_current(
            config["post_id"], config["file_id"], staged[0]["id"]))

        worker = Worker(self.jobs_db, poll_seconds=0.01, stale_seconds=10)
        try:
            self.assertTrue(worker._recover_staged_community_publishes(grace_seconds=0))
        finally:
            worker.close()
        self.assertEqual(self.jobs.get_job(staged[0]["id"])["status"], JobStatus.QUEUED)

    def test_linked_staging_recovers_even_when_failed_lease_clear_leaves_api_pid_alive(self):
        draft = self.svc.create_draft(
            principal=OWNER,
            output_dir=str(self.output_root / "chap"),
            series_slug="chap",
        )
        real_transition = self.jobs.transition

        def fail_queue(job_id, target, **fields):
            if target == JobStatus.QUEUED:
                raise sqlite3.OperationalError("queue unavailable")
            return real_transition(job_id, target, **fields)

        with patch.object(self.jobs, "transition", side_effect=fail_queue), patch.object(
            self.jobs,
            "update_fields",
            side_effect=sqlite3.OperationalError("lease clear unavailable"),
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "queue unavailable"):
                self.svc.request_publish(draft["post_id"], principal=OWNER)

        staged = self.jobs.list_jobs(statuses=[JobStatus.STAGING], limit=None)
        self.assertEqual(len(staged), 1)
        self.assertTrue(process_tree.is_alive(
            staged[0]["worker_pid"],
            create_time=staged[0]["worker_create_time"],
        ))
        worker = Worker(self.jobs_db, poll_seconds=0.01, stale_seconds=10)
        try:
            self.assertTrue(worker._recover_staged_community_publishes(grace_seconds=0))
        finally:
            worker.close()
        self.assertEqual(self.jobs.get_job(staged[0]["id"])["status"], JobStatus.QUEUED)

    def test_staged_job_without_community_link_is_cancelled_after_grace(self):
        job_id = self.jobs.create_job(
            source_url="",
            output_dir=str(self.output_root / "missing-link"),
            command=["community_publish"],
            configuration={
                "job_type": "community_publish",
                "community_db": str(self.community_db),
                "post_id": "missing-post",
                "file_id": "missing-file",
            },
            initial_status=JobStatus.STAGING,
        )
        worker = Worker(self.jobs_db, poll_seconds=0.01, stale_seconds=10)
        try:
            self.assertTrue(worker._recover_staged_community_publishes(grace_seconds=0))
        finally:
            worker.close()
        self.assertEqual(self.jobs.get_job(job_id)["status"], JobStatus.CANCELLED)


if __name__ == "__main__":
    unittest.main()
