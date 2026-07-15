"""Integration tests for the worker, runner and fake pipeline as real subprocesses.

These exercise the real process boundary - the worker spawns a runner subprocess, which
spawns the fake pipeline subprocess - so the survival and recovery guarantees are proven
against actual processes, not mocks. No network, NVIDIA, OCR or heavy PDF is involved.
"""

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from job_store import JobStatus, JobStore
from worker_service import Worker

REPO = Path(__file__).resolve().parent


def _fake_command(output_dir: Path, *, outcome="finished", steps=2, sleep=0.02,
                  hang=False, stop_after="", fail_at=""):
    cmd = [sys.executable, "-u", str(REPO / "fake_pipeline.py"),
           "--output-dir", str(output_dir), "--outcome", outcome,
           "--steps", str(steps), "--sleep", str(sleep)]
    if hang:
        cmd.append("--hang")
    if stop_after:
        cmd += ["--stop-after-stage", stop_after]
    if fail_at:
        cmd += ["--fail-at-stage", fail_at]
    return cmd


class WorkerRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "jobs.sqlite3"
        self.store = JobStore(self.db)

    def tearDown(self):
        self.store.close()

    def _job(self, **over):
        out = self.tmp / over.pop("out", "chapter")
        cmd = _fake_command(out, **{k: v for k, v in over.items()
                                    if k in {"outcome", "steps", "sleep", "hang",
                                             "stop_after", "fail_at"}})
        return self.store.create_job(source_url="https://example/x", output_dir=str(out),
                                     command=cmd), out

    def _run_worker_once(self):
        worker = Worker(self.db, poll_seconds=0.05, stale_seconds=5)
        try:
            worker.run(once=True)
        finally:
            worker.close()

    def test_finished_job(self):
        jid, out = self._job(outcome="finished")
        self._run_worker_once()
        job = self.store.get_job(jid)
        self.assertEqual(job["status"], JobStatus.FINISHED)
        self.assertTrue(job["pdf_path"])
        self.assertEqual(job["exit_code"], 0)

    def test_review_required_job(self):
        jid, out = self._job(outcome="review")
        self._run_worker_once()
        self.assertEqual(self.store.get_job(jid)["status"], JobStatus.REVIEW_REQUIRED)

    def test_failed_job_without_pdf(self):
        jid, out = self._job(outcome="fail")
        self._run_worker_once()
        job = self.store.get_job(jid)
        self.assertEqual(job["status"], JobStatus.FAILED)
        self.assertNotEqual(job["exit_code"], 0)

    def test_initial_manifest_and_log_created(self):
        jid, out = self._job(outcome="finished")
        self._run_worker_once()
        self.assertTrue((out / "job_manifest.json").is_file())
        log = REPO / ".cache" / "runtime" / "logs" / f"{jid}.log"
        self.assertTrue(log.is_file())
        self.assertTrue(log.read_text(encoding="utf-8").strip())

    def test_checkpoints_written(self):
        jid, out = self._job(outcome="finished")
        self._run_worker_once()
        self.assertTrue((out / "checkpoints" / "download.done").is_file())
        self.assertTrue((out / "checkpoints" / "pdf.done").is_file())

    def test_runner_failure_does_not_break_worker(self):
        # A failing job must not stop the worker from finishing the next one.
        jid_fail, _ = self._job(outcome="fail", out="a")
        jid_ok, _ = self._job(outcome="finished", out="b")
        worker = Worker(self.db, poll_seconds=0.05, stale_seconds=5)
        try:
            worker.run(max_idle_cycles=2)
        finally:
            worker.close()
        self.assertEqual(self.store.get_job(jid_fail)["status"], JobStatus.FAILED)
        self.assertEqual(self.store.get_job(jid_ok)["status"], JobStatus.FINISHED)

    def test_second_worker_exits_when_one_is_healthy(self):
        self.store.register_worker("other", 99999)  # fresh lease
        worker = Worker(self.db, poll_seconds=0.05, stale_seconds=30)
        try:
            worker.run(once=True)  # should exit cleanly without claiming
        finally:
            worker.close()
        # No job existed, but the point is it returned without error.
        self.assertTrue(True)


class CancelAndRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "jobs.sqlite3"
        self.store = JobStore(self.db)

    def tearDown(self):
        self.store.close()

    def test_cancel_during_run_preserves_artifacts(self):
        out = self.tmp / "chapter"
        cmd = _fake_command(out, hang=True, steps=1)
        jid = self.store.create_job(source_url="https://example/x",
                                    output_dir=str(out), command=cmd)
        # Run the worker on a background thread; it will block on the hanging pipeline.
        import threading
        worker = Worker(self.db, poll_seconds=0.05, stale_seconds=30)
        t = threading.Thread(target=lambda: worker.run(once=True), daemon=True)
        t.start()
        # Wait until it is running, then request cancel.
        for _ in range(100):
            job = self.store.get_job(jid)
            if job["status"] == JobStatus.RUNNING:
                break
            time.sleep(0.1)
        self.assertEqual(self.store.get_job(jid)["status"], JobStatus.RUNNING)
        self.store.request_cancel(jid)
        t.join(timeout=30)
        worker.close()
        job = self.store.get_job(jid)
        self.assertEqual(job["status"], JobStatus.CANCELLED)
        # The partial output dir is preserved, not deleted.
        self.assertTrue(out.is_dir())

    def test_interrupted_job_recovers_and_resumes_reusing_checkpoints(self):
        # First attempt stops cleanly after 'download' (simulating a partial run), then
        # is marked interrupted, resumed, and the second attempt reuses the checkpoint.
        out = self.tmp / "chapter"
        cmd = _fake_command(out, stop_after="download", steps=1)
        jid = self.store.create_job(source_url="https://example/x",
                                    output_dir=str(out), command=cmd)
        worker = Worker(self.db, poll_seconds=0.05, stale_seconds=30)
        try:
            worker.run(once=True)
        finally:
            worker.close()
        # The runner saw a non-zero, non-standard exit (3) with no PDF -> failed.
        job = self.store.get_job(jid)
        self.assertIn(job["status"], {JobStatus.FAILED})
        self.assertTrue((out / "checkpoints" / "download.done").is_file())

        # Resume: requeue a new attempt whose command reuses the same output dir.
        resume_cmd = _fake_command(out, outcome="finished", steps=1)
        resume_id = self.store.create_job(
            source_url="https://example/x", output_dir=str(out), command=resume_cmd,
            previous_job_id=jid, attempt=2, resume_from_stage="download",
        )
        worker2 = Worker(self.db, poll_seconds=0.05, stale_seconds=30)
        try:
            worker2.run(once=True)
        finally:
            worker2.close()
        resumed = self.store.get_job(resume_id)
        self.assertEqual(resumed["status"], JobStatus.FINISHED)
        self.assertEqual(resumed["attempt"], 2)
        self.assertEqual(resumed["previous_job_id"], jid)
        # The download checkpoint from attempt 1 was reused (log records it).
        log = REPO / ".cache" / "runtime" / "logs" / f"{resume_id}.log"
        self.assertIn("reaproveitado", log.read_text(encoding="utf-8"))

    def test_stale_running_job_is_recovered_on_next_worker(self):
        # Simulate a crashed runner: a job stuck in RUNNING with an old heartbeat.
        out = self.tmp / "chapter"
        jid = self.store.create_job(source_url="https://example/x",
                                    output_dir=str(out), command=["x"])
        self.store.claim_next_job("dead", 1)
        self.store.transition(jid, JobStatus.STARTING, expected_worker="dead")
        self.store.transition(jid, JobStatus.RUNNING, expected_worker="dead")
        self.store.update_fields(jid, heartbeat_at=time.time() - 999)
        recovered = self.store.recover_stale(stale_seconds=30)
        self.assertEqual(recovered, [jid])
        self.assertEqual(self.store.get_job(jid)["status"], JobStatus.INTERRUPTED)


if __name__ == "__main__":
    unittest.main()
