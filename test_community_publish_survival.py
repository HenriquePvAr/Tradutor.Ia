"""A community_publish upload survives the UI dropping and recovers from a worker crash.

Real processes: a real worker spawns the real community publish runner, which streams a
PDF to a filesystem-backed fake provider (a stand-in for Drive, no network). The "UI" is
a reader we drop and reopen. Every wait has a deadline; workers are torn down by verified
PID in a finally, so a failure never leaks a process or hangs.
"""

import _test_bootstrap  # noqa: F401

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import process_tree
from community_auth import RequestPrincipal
from community_service import CommunityService
from community_storage import FilesystemStorageProvider
from community_store import CommunityStore, FileStatus, PostStatus
from job_store import JobStatus, JobStore

REPO = Path(__file__).resolve().parent
OWNER = RequestPrincipal("local", True, auth_source="test", session_id="test-owner")


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
        self._orphan_runners = []

    def tearDown(self):
        for pid, create_time, job_id in self._orphan_runners:
            fingerprint = ["community_publish_runner.py", job_id]
            if process_tree.is_alive(
                pid, create_time=create_time, substrings=fingerprint
            ):
                process_tree.terminate_tree(
                    pid,
                    create_time=create_time,
                    substrings=fingerprint,
                    timeout=10,
                )
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
        draft = self.svc.create_draft(principal=OWNER, output_dir=str(self.output_root / "chap"),
                                      series_slug="chap", episode_number="1", series_title="Chap")
        return self.svc.request_publish(draft["post_id"], principal=OWNER)

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

    def test_new_worker_stops_orphan_runner_and_retries_without_remote_orphan(self):
        # Keep the runner alive long enough to kill only its supervising worker.
        (self.output_root / "chap" / "chapter.pdf").write_bytes(
            b"%PDF-1.4\n" + b"r" * (8 * 1024 * 1024) + b"\n%%EOF\n")
        pub = self._publish()
        worker1 = self._start_worker("crash-worker-1.out")

        self.assertTrue(_wait(
            lambda: (
                (job := self.jobs.get_job(pub["job_id"]))["status"] == JobStatus.RUNNING
                and int(job.get("progress_current") or 0) > 0
                and bool(job.get("runner_pid"))
            ),
            30,
            "first runner uploaded a chunk",
        ))
        crashed_job = self.jobs.get_job(pub["job_id"])
        old_pid = crashed_job["runner_pid"]
        old_create_time = crashed_job["runner_create_time"]
        old_worker_id = crashed_job["worker_id"]
        self._orphan_runners.append((old_pid, old_create_time, pub["job_id"]))

        # Kill only the known worker process.  Its runner must remain alive so worker 2
        # proves fingerprinted orphan reconciliation rather than ordinary stop handling.
        worker1.kill()
        worker1.wait(timeout=8)
        self.jobs.unregister_worker(old_worker_id)
        fingerprint = ["community_publish_runner.py", pub["job_id"]]
        self.assertTrue(process_tree.is_alive(
            old_pid,
            create_time=old_create_time,
            substrings=fingerprint,
        ))

        self._start_worker("crash-worker-2.out")

        def replacement_started():
            job = self.jobs.get_job(pub["job_id"])
            return bool(
                job
                and job["status"] == JobStatus.RUNNING
                and job.get("runner_pid")
                and (
                    job["runner_pid"] != old_pid
                    or job.get("runner_create_time") != old_create_time
                )
            )

        self.assertTrue(_wait(replacement_started, 30, "replacement runner"))
        self.assertFalse(process_tree.is_alive(
            old_pid,
            create_time=old_create_time,
            substrings=fingerprint,
        ))
        self.assertTrue(_wait(
            lambda: self.jobs.get_job(pub["job_id"])["status"] in JobStatus.TERMINAL,
            75,
            "replacement finished",
        ))

        job = self.jobs.get_job(pub["job_id"])
        post = self.store.get_post(pub["post_id"])
        file = self.store.get_file(pub["file_id"])
        self.assertEqual(job["status"], JobStatus.FINISHED)
        self.assertEqual(job["attempt"], 1)
        self.assertEqual(post["status"], PostStatus.PUBLISHED)
        self.assertEqual(file["upload_status"], FileStatus.VERIFIED)
        self.assertEqual(len(list((self.storage_root / "files").glob("*.bin"))), 1)
        self.assertEqual(len(list((self.storage_root / "sessions").glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
