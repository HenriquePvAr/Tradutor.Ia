"""Operational proof: a job survives the UI being dropped and restarted.

The worker runs as a real, independent OS process. The "UI" here is a UiBridge-style
reader we open, drop, and reopen against the same database - standing in for app_ui.py
being closed and restarted. The job keeps processing throughout and is completed exactly
once, by the worker, with no orphaned processes.

The worker's own stdout/stderr are redirected to a file, never inherited and never left
on an unread PIPE: a surviving worker holding the test runner's console pipe is exactly
what hung an earlier run for hours. Every external wait has a monotonic deadline and a
diagnostic dump, so the test fails fast and loud instead of hanging.
"""

import _test_bootstrap  # noqa: F401

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from job_store import JobStatus, JobStore

REPO = Path(__file__).resolve().parent


def _fake_command(output_dir: Path, *, steps=4, sleep=0.2):
    return [sys.executable, "-u", str(REPO / "fake_pipeline.py"),
            "--output-dir", str(output_dir), "--outcome", "finished",
            "--steps", str(steps), "--sleep", str(sleep)]


def _wait_until(predicate, *, timeout, on_timeout):
    """Poll ``predicate`` until true or the monotonic deadline passes; then diagnose."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    on_timeout()
    return None


class WorkerSurvivalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "jobs.sqlite3"
        self.worker_log = self.tmp / "worker.out"
        self._worker = None
        self._worker_log_handle = None

    def tearDown(self):
        self._stop_worker()
        if self._worker_log_handle:
            self._worker_log_handle.close()

    def _start_worker(self):
        # Redirect to a file (not inherited, not an unread PIPE) and give the worker its
        # own process group so it can be signalled without touching the test runner.
        self._worker_log_handle = self.worker_log.open("wb")
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self._worker = subprocess.Popen(
            [sys.executable, "-u", str(REPO / "worker_service.py"),
             "--db", str(self.db), "--poll-interval", "0.2"],
            cwd=str(REPO), stdin=subprocess.DEVNULL,
            stdout=self._worker_log_handle, stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        return self._worker

    def _stop_worker(self):
        proc = self._worker
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.terminate()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=8)
            return
        except subprocess.TimeoutExpired:
            pass
        # Escalate to a verified terminate/kill of exactly this PID.
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired, ValueError):
                pass

    def _dump(self, store, jid, note):
        job = store.get_job(jid) or {}
        tail = ""
        if self.worker_log.is_file():
            tail = "\n".join(self.worker_log.read_text(encoding="utf-8", errors="replace").splitlines()[-15:])
        self.fail(
            f"{note}\n  job status={job.get('status')} attempt={job.get('attempt')} "
            f"heartbeat={job.get('heartbeat_at')} worker_pid={job.get('worker_pid')} "
            f"runner_pid={job.get('runner_pid')} progress={job.get('progress_current')}/"
            f"{job.get('progress_total')} exit={job.get('exit_code')}\n"
            f"  worker_alive={self._worker and self._worker.poll() is None}\n"
            f"  worker.out tail:\n{tail}"
        )

    def test_job_survives_ui_restart_and_runs_once(self):
        out = self.tmp / "chapter"
        store = JobStore(self.db)
        jid = store.create_job(source_url="https://example/x", output_dir=str(out),
                               command=_fake_command(out), run_id="run-fixed-1")
        store.close()

        self._start_worker()

        # 1) Reaches running.
        reader = JobStore(self.db)
        running = _wait_until(
            lambda: reader.get_job(jid)["status"] == JobStatus.RUNNING,
            timeout=30, on_timeout=lambda: self._dump(reader, jid, "job never reached running"),
        )
        self.assertTrue(running)
        first = reader.get_job(jid)
        self.assertEqual(first["attempt"], 1)
        self.assertTrue(first["worker_pid"])
        hb1 = first["heartbeat_at"]

        # 2) "Close" the UI reader; the worker (a separate process) keeps going.
        reader.close()
        self.assertIsNone(self._worker.poll(), "worker must survive the UI reader closing")

        # 3) "Restart" the UI: a fresh reader sees the same job/run_id, still advancing.
        reader = JobStore(self.db)
        job = reader.get_job(jid)
        self.assertEqual(job["run_id"], "run-fixed-1")
        advanced = _wait_until(
            lambda: (reader.get_job(jid)["heartbeat_at"] or 0) > (hb1 or 0)
            or reader.get_job(jid)["status"] in JobStatus.TERMINAL,
            timeout=20, on_timeout=lambda: self._dump(reader, jid, "heartbeat/progress did not advance"),
        )
        self.assertTrue(advanced)

        # 4) Completes exactly once.
        done = _wait_until(
            lambda: reader.get_job(jid)["status"] in JobStatus.TERMINAL,
            timeout=60, on_timeout=lambda: self._dump(reader, jid, "job never finished"),
        )
        self.assertTrue(done)
        job = reader.get_job(jid)
        self.assertEqual(job["status"], JobStatus.FINISHED)
        self.assertEqual(job["attempt"], 1, "job must run exactly once")
        self.assertEqual(job["exit_code"], 0)
        reader.close()

        # 5) Artifacts present; stopping the worker leaves no orphan.
        self.assertTrue((out / "the_fake_chapter.pdf").is_file())
        self.assertTrue((out / "job_manifest.json").is_file())
        self._stop_worker()
        self.assertIsNotNone(self._worker.poll(), "worker did not stop on request")


if __name__ == "__main__":
    unittest.main()
