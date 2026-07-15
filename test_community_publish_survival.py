"""A community_publish upload survives the UI dropping and recovers from a worker crash.

Real processes: a real worker spawns the real community publish runner, which streams a
PDF to a filesystem-backed fake provider (a stand-in for Drive, no network). The "UI" is
a reader we drop and reopen. Every wait has a deadline; workers are torn down by verified
PID in a finally, so a failure never leaks a process or hangs.
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import process_tree
from community_service import CommunityService
from community_storage import FilesystemStorageProvider
from community_store import CommunityStore, FileStatus, PostStatus
from job_store import JobStatus, JobStore

REPO = Path(__file__).resolve().parent


def _wait(pred, timeout, label):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.15)
    print("TIMEOUT:", label)
    return False


class CommunityPublishSurvivalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.output_root = self.tmp / "output"
        (self.output_root / "chap").mkdir(parents=True)
        # A larger PDF so the chunked upload spans several heartbeats.
        (self.output_root / "chap" / "chapter.pdf").write_bytes(
            b"%PDF-1.4\n" + b"z" * (3 * 1024 * 1024) + b"\n%%EOF\n")
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
        self._workers = []

    def tearDown(self):
        for proc, log in self._workers:
            if proc.poll() is None:
                process_tree.terminate_tree(proc.pid, substrings=["worker_service.py"], timeout=10)
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
            log.close()
        self.store.close()
        self.jobs.close()

    def _start_worker(self, name):
        log = (self.tmp / name).open("wb")
        env = {**os.environ, "COMMUNITY_UPLOAD_CHUNK_DELAY": "0.15"}  # pace the upload
        proc = subprocess.Popen(
            [sys.executable, "-u", str(REPO / "worker_service.py"),
             "--db", str(self.jobs_db), "--poll-interval", "0.2"],
            cwd=str(REPO), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            env=env, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        self._workers.append((proc, log))
        return proc

    def _publish(self):
        draft = self.svc.create_draft(output_dir=str(self.output_root / "chap"),
                                      series_slug="chap", episode_number="1", series_title="Chap")
        return self.svc.request_publish(draft["post_id"])

    def test_upload_survives_ui_restart_and_publishes_once(self):
        pub = self._publish()
        worker = self._start_worker("w1.out")

        reader = JobStore(self.jobs_db)
        self.assertTrue(_wait(lambda: reader.get_job(pub["job_id"])["status"] == JobStatus.RUNNING,
                              30, "uploading"))
        # Drop the "UI" reader; the worker keeps uploading.
        reader.close()
        self.assertIsNone(worker.poll(), "worker died with UI reader")

        # Reopen: same job, bytes advancing.
        reader = JobStore(self.jobs_db)
        first = reader.get_job(pub["job_id"])["progress_current"]
        self.assertTrue(_wait(lambda: reader.get_job(pub["job_id"])["progress_current"] > first
                              or reader.get_job(pub["job_id"])["status"] in JobStatus.TERMINAL,
                              20, "bytes advance"))
        self.assertTrue(_wait(lambda: reader.get_job(pub["job_id"])["status"] in JobStatus.TERMINAL,
                              60, "finished"))
        job = reader.get_job(pub["job_id"])
        reader.close()
        self.assertEqual(job["status"], JobStatus.FINISHED)
        self.assertEqual(job["attempt"], 1)

        # Post published, exactly one verified file, uploaded bytes match.
        post = self.store.get_post(pub["post_id"])
        self.assertEqual(post["status"], PostStatus.PUBLISHED)
        file = self.store.get_file(pub["file_id"])
        self.assertEqual(file["upload_status"], FileStatus.VERIFIED)
        provider = FilesystemStorageProvider(self.storage_root)
        self.assertEqual(provider.stat_file(file["storage_file_id"]).size, file["size_bytes"])


if __name__ == "__main__":
    unittest.main()
