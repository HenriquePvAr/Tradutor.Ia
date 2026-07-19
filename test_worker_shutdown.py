"""Worker stop and crash recovery leave no orphaned runner/pipeline processes.

Real processes throughout: a real worker spawns a real runner, which spawns a real fake
pipeline. Every wait has a monotonic deadline; every worker is torn down in a finally by
its verified PID tree, so a failing test never leaks a process or hangs.
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
from job_store import JobStatus, JobStore

REPO = Path(__file__).resolve().parent


def _fake_command(output_dir: Path, *, steps=40, sleep=0.3, hang=False):
    cmd = [sys.executable, "-u", str(REPO / "fake_pipeline.py"),
           "--output-dir", str(output_dir), "--outcome", "finished",
           "--steps", str(steps), "--sleep", str(sleep)]
    if hang:
        cmd.append("--hang")
    return cmd


def _wait(pred, timeout, label):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.15)
    print("TIMEOUT:", label)
    return False


class WorkerShutdownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "jobs.sqlite3"
        self.store = JobStore(self.db)
        self._workers = []  # (popen, logfile)

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

    def _start_worker(self, name):
        log = (self.tmp / name).open("wb")
        proc = subprocess.Popen(
            [sys.executable, "-u", str(REPO / "worker_service.py"),
             "--db", str(self.db), "--poll-interval", "0.2"],
            cwd=str(REPO), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        self._workers.append((proc, log))
        return proc

    def _runner_tree_pids(self, job):
        pid = job.get("runner_pid")
        info = process_tree.snapshot(pid)
        if info is None:
            return []
        import psutil
        try:
            return [pid] + [c.pid for c in psutil.Process(pid).children(recursive=True)]
        except psutil.Error:
            return [pid]

    def test_worker_stop_takes_down_runner_tree_and_interrupts(self):
        out = self.tmp / "chapter"
        jid = self.store.create_job(source_url="https://example/x", output_dir=str(out),
                                    command=_fake_command(out), run_id="stop-1")
        worker = self._start_worker("w1.out")

        self.assertTrue(_wait(lambda: self.store.get_job(jid)["status"] == JobStatus.RUNNING, 30, "running"))
        job = self.store.get_job(jid)
        runner_pids = self._runner_tree_pids(job)
        self.assertTrue(job["runner_pid"] and runner_pids, "runner tree not found")

        # Request stop through the DB (as the launcher's stop-worker does).
        worker_row = self.store.healthy_worker(stale_seconds=15)
        self.store.request_worker_stop(worker_row["worker_id"])

        # Worker exits, runner tree is gone, job is interrupted, checkpoints preserved.
        self.assertTrue(_wait(lambda: worker.poll() is not None, 30, "worker exit"))
        for pid in runner_pids:
            self.assertTrue(process_tree.wait_gone(pid, timeout=10), f"orphan pid {pid}")
        job = self.store.get_job(jid)
        self.assertEqual(job["status"], JobStatus.INTERRUPTED)
        self.assertTrue((out / "checkpoints").is_dir())

    def test_new_worker_reconciles_before_resume_no_duplicate(self):
        # Crash (no graceful stop): kill only the worker by verified PID, leaving the
        # runner/pipeline alive. A new worker must NOT immediately start another attempt;
        # it reconciles the live runner (stops it) before marking the job interrupted.
        out = self.tmp / "chapter"
        jid = self.store.create_job(source_url="https://example/x", output_dir=str(out),
                                    command=_fake_command(out, hang=True), run_id="crash-1")
        worker = self._start_worker("w1.out")
        self.assertTrue(_wait(lambda: self.store.get_job(jid)["status"] == JobStatus.RUNNING, 30, "running"))
        job = self.store.get_job(jid)
        runner_pid = job["runner_pid"]
        self.assertTrue(process_tree.snapshot(runner_pid) is not None, "runner not alive")

        # Kill ONLY the worker process (simulate crash), not its runner subtree.
        import psutil
        psutil.Process(worker.pid).kill()
        worker.wait(timeout=10)
        self.assertTrue(process_tree.snapshot(runner_pid) is not None, "runner should still be alive after worker crash")

        # Let the worker lease go stale, then start a replacement worker.
        time.sleep(17)
        self._start_worker("w2.out")

        # The replacement reconciles the live runner: stops its tree, marks interrupted,
        # and never leaves a second attempt running.
        self.assertTrue(_wait(lambda: self.store.get_job(jid)["status"] == JobStatus.INTERRUPTED, 60, "interrupted"))
        self.assertTrue(process_tree.wait_gone(runner_pid, timeout=15), "orphaned runner tree not reconciled")
        job = self.store.get_job(jid)
        self.assertIn(job["interrupted_reason"], {"reconciled_live_runner", "orphaned_worker_gone"})
        # No duplicate: exactly one job for this chapter exists in a non-terminal state.
        in_flight = [j for j in self.store.list_jobs() if j["status"] in JobStatus.IN_FLIGHT]
        self.assertEqual(in_flight, [])


if __name__ == "__main__":
    unittest.main()
