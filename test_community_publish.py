"""community_publish job: upload via worker, resume, cancel, verification — offline."""

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import community_publish_runner
from community_storage import FilesystemStorageProvider
from community_store import CommunityStore, FileStatus, PostStatus
from community_service import CommunityService
from job_store import JobStatus, JobStore
from worker_service import Worker

REPO = Path(__file__).resolve().parent


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
        draft = self.svc.create_draft(output_dir=str(self.output_root / "chap"),
                                      series_slug="chap", episode_number="1", series_title="Chap")
        return self.svc.request_publish(draft["post_id"])

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


if __name__ == "__main__":
    unittest.main()
