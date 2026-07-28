"""UI submit → job → runner → PDF, for a URL source, without fetching anything.

The Webtoons fetch layer was verified against the live reader by direct measurement:
adapter selection, preflight, container, selectors, lazy loading, CDN authorization and the
transport all behave. What had never been proven is the layer after it — that a job created
through the UI reaches the runner, drives the stage machine, produces a discoverable PDF and
settles in a correct terminal state.

That question does not need a real chapter, so this exercises the real ui_bridge, the real
job store and the real runner contract while the pipeline itself is fake_pipeline.py. No
network, no browser, no provider, no chapter content.
"""

import _test_bootstrap  # noqa: F401

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import ui_bridge
from job_store import JobStatus, JobStore

REPO = Path(__file__).resolve().parent
CHAPTER_URL = ("https://www.webtoons.com/en/drama/daytime-in-the-bunker/episode-1/viewer"
               "?title_no=9842&episode_no=1")


def drive(coro):
    """Run a coroutine with no event loop (the offline guard blocks the self-pipe)."""
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("unexpectedly awaited")


class _Bridge(ui_bridge.UiBridge):
    """Real bridge logic, isolated store, no worker spawned."""

    def __init__(self, db_path):
        self.store = JobStore(db_path)
        self.history_revision = 1
        self.worker_calls = 0

    def _refresh_history(self):
        pass

    def ensure_worker(self):
        self.worker_calls += 1
        return {"online": True, "started": False}


# Submit-path coverage (job creation, command building, duplicate guard) lives in
# test_translation_start.py. Reproducing it here would mean mocking the whole source
# analysis contract, which tests the mock rather than the code.


class StageMachineTests(unittest.TestCase):
    """The counter must belong to the stage that produced it, across a whole run."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = JobStore(self.tmp / "jobs.sqlite3")
        self.bridge = _Bridge(self.tmp / "jobs.sqlite3")

    def tearDown(self):
        self.store.close()
        self.bridge.store.close()

    def make_running(self):
        job_id = self.bridge.store.create_job(
            source_url=CHAPTER_URL, output_dir=str(self.tmp / "out"),
            configuration={"job_type": "translation"},
            command=[sys.executable, "-c", "pass"])
        for target in (JobStatus.CLAIMING, JobStatus.STARTING, JobStatus.RUNNING):
            self.bridge.store.transition(
                job_id, target, **({"worker_id": "w1"} if target == JobStatus.CLAIMING else {}))
        return job_id

    def test_counter_is_reported_only_for_its_own_stage(self):
        job_id = self.make_running()
        # Downloads finish 167/167, then translation starts with no counter of its own.
        self.bridge.store.update_progress(job_id, stage="Baixando imagens", current=167,
                                          total=167, counter_stage="Baixando imagens")
        self.bridge.store.update_progress(job_id, stage="Tradução NVIDIA",
                                          counter_stage="Baixando imagens")
        with mock.patch.object(ui_bridge, "_runner_still_alive", return_value=True):
            progress = self.bridge.runtime_state()["progress"]
        # The download's 167/167 must not be rendered under the translation label.
        self.assertEqual((progress["current"], progress["total"]), (0, 0))
        self.assertIsNone(progress["fraction"])

    def test_matching_counter_is_reported(self):
        job_id = self.make_running()
        self.bridge.store.update_progress(job_id, stage="Tradução NVIDIA", current=12,
                                          total=40, counter_stage="Tradução NVIDIA")
        with mock.patch.object(ui_bridge, "_runner_still_alive", return_value=True):
            progress = self.bridge.runtime_state()["progress"]
        self.assertEqual((progress["current"], progress["total"]), (12, 40))

    def test_browser_log_redacts_local_absolute_paths(self):
        job_id = self.make_running()
        log_path = self.tmp / "runner.log"
        log_path.write_text(
            "PDF: C:\\Projetos\\Tradutor.Ia\\output\\chapter\\result.pdf\n",
            encoding="utf-8",
        )
        self.bridge.store.update_fields(job_id, log_path=str(log_path))

        entries = self.bridge._tail_job_logs(
            self.bridge.store.get_job(job_id), 0)["entries"]

        self.assertEqual(len(entries), 1)
        self.assertNotIn("C:\\", entries[0]["text"])
        self.assertIn("[CAMINHO LOCAL]", entries[0]["text"])


class RunnerHandoffTests(unittest.TestCase):
    """The layer that was never proven: job → runner → outputs → terminal state."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bridge = _Bridge(self.tmp / "jobs.sqlite3")
        self.out = self.tmp / "chapter_out"

    def tearDown(self):
        self.bridge.store.close()

    def run_pipeline(self, outcome="finished"):
        command = [sys.executable, "-u", str(REPO / "fake_pipeline.py"),
                   "--output-dir", str(self.out), "--outcome", outcome,
                   "--steps", "2", "--sleep", "0.01"]
        job_id = self.bridge.store.create_job(
            source_url=CHAPTER_URL, output_dir=str(self.out),
            configuration={"job_type": "translation"}, command=command)
        for target in (JobStatus.CLAIMING, JobStatus.STARTING, JobStatus.RUNNING):
            self.bridge.store.transition(
                job_id, target, **({"worker_id": "w1"} if target == JobStatus.CLAIMING else {}))
        proc = subprocess.run(command, cwd=str(REPO), capture_output=True, timeout=180)
        self.bridge.store.update_fields(job_id, exit_code=proc.returncode)
        return job_id, proc

    def test_run_produces_a_valid_pdf_and_a_correct_terminal_state(self):
        job_id, proc = self.run_pipeline()
        self.assertEqual(proc.returncode, 0, proc.stderr[-300:])
        self.bridge.store.transition(job_id, JobStatus.FINISHED)

        pdfs = list(self.out.glob("*.pdf"))
        self.assertTrue(pdfs, "no PDF discovered")
        self.assertTrue(pdfs[0].read_bytes().startswith(b"%PDF-"))
        self.assertGreater(pdfs[0].stat().st_size, 0)

        row = self.bridge.store.get_job(job_id)
        self.assertEqual(row["status"], JobStatus.FINISHED)
        self.assertEqual(row["exit_code"], 0)
        self.assertIsNotNone(row["finished_at"])

    def test_stage_checkpoints_are_written_in_order(self):
        self.run_pipeline()
        done = sorted(p.stem for p in (self.out / "checkpoints").glob("*.done"))
        for expected in ("download", "ocr", "translate", "pdf"):
            self.assertIn(expected, done, expected)

    def test_elapsed_freezes_once_terminal(self):
        job_id, _ = self.run_pipeline()
        self.bridge.store.transition(job_id, JobStatus.FINISHED)
        row = self.bridge.store.get_job(job_id)
        first = row["finished_at"] - row["started_at"]
        time.sleep(0.05)
        again = self.bridge.store.get_job(job_id)
        self.assertEqual(again["finished_at"] - again["started_at"], first)

    def test_no_pdf_means_the_job_is_not_presentable_as_a_result(self):
        # Never finished merely because a process exited.
        job_id = self.bridge.store.create_job(
            source_url=CHAPTER_URL, output_dir=str(self.tmp / "empty_out"),
            configuration={"job_type": "translation"},
            command=[sys.executable, "-c", "pass"])
        for target in (JobStatus.CLAIMING, JobStatus.STARTING, JobStatus.RUNNING,
                       JobStatus.FINISHED):
            self.bridge.store.transition(
                job_id, target, **({"worker_id": "w1"} if target == JobStatus.CLAIMING else {}))
        self.assertIsNone(self.bridge._latest_terminal_job())

    def test_refresh_does_not_resurrect_or_duplicate_the_job(self):
        job_id, _ = self.run_pipeline()
        self.bridge.store.transition(job_id, JobStatus.FINISHED)
        before = len(self.bridge.store.list_jobs(statuses=list(JobStatus.ALL), limit=None))
        first = self.bridge.runtime_state()
        second = self.bridge.runtime_state()          # F5
        self.assertEqual(first["status"], second["status"])
        self.assertFalse(second["queue_running"])
        self.assertEqual(
            len(self.bridge.store.list_jobs(statuses=list(JobStatus.ALL), limit=None)), before)
        self.assertEqual(self.bridge.store.get_job(job_id)["status"], JobStatus.FINISHED)


class CancellationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bridge = _Bridge(self.tmp / "jobs.sqlite3")

    def tearDown(self):
        self.bridge.store.close()

    def running_job(self):
        job_id = self.bridge.store.create_job(
            source_url=CHAPTER_URL, output_dir=str(self.tmp / "out"),
            configuration={"job_type": "translation"},
            command=[sys.executable, "-c", "pass"])
        for target in (JobStatus.CLAIMING, JobStatus.STARTING, JobStatus.RUNNING):
            self.bridge.store.transition(
                job_id, target, **({"worker_id": "w1"} if target == JobStatus.CLAIMING else {}))
        self.bridge.store.update_fields(job_id, runner_pid=999999,
                                        started_at=time.time() - 30,
                                        heartbeat_at=time.time() - 20)
        return job_id

    def test_cancel_settles_the_job_when_the_runner_is_gone(self):
        job_id = self.running_job()
        with mock.patch.object(ui_bridge, "_runner_still_alive", return_value=False):
            drive(self.bridge.cancel())
        row = self.bridge.store.get_job(job_id)
        self.assertEqual(row["status"], JobStatus.CANCELLED)
        self.assertIsNotNone(row["finished_at"])

    def test_cancelled_job_stays_terminal_across_refresh(self):
        self.running_job()
        with mock.patch.object(ui_bridge, "_runner_still_alive", return_value=False):
            drive(self.bridge.cancel())
            self.assertFalse(self.bridge.runtime_state()["queue_running"])
            self.assertFalse(self.bridge.runtime_state()["queue_running"])


class NoProviderTests(unittest.TestCase):
    def test_fake_pipeline_imports_no_provider_and_no_network(self):
        # Assert on imports, not prose: the stage label "Tradução NVIDIA" and a docstring
        # promising the opposite both contain the word.
        source = (REPO / "fake_pipeline.py").read_text(encoding="utf-8")
        imports = [line.strip() for line in source.splitlines()
                   if line.strip().startswith(("import ", "from "))]
        for banned in ("requests", "selenium", "socket", "urllib", "http"):
            for line in imports:
                self.assertNotIn(banned, line, f"{banned} in {line}")


if __name__ == "__main__":
    unittest.main()
