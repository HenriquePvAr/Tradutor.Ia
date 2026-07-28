"""Stale in-flight jobs, frozen timers, stage-bound progress and cancel without a process."""

import _test_bootstrap  # noqa: F401

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import ui_bridge
from job_store import JobStatus, JobStore
from output_manifest import MANIFEST_FILENAME, build_run_manifest
from ui_bridge import UNAVAILABLE_DURATION, _duration, _format_seconds, _normalize_epoch
from ui_helpers import ProgressSnapshot, parse_progress_line


class FormatTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(_format_seconds(34), "34s")
        self.assertEqual(_format_seconds(754), "12min 34s")
        self.assertEqual(_format_seconds(4320), "1h 12min")

    def test_invalid_never_renders_a_number(self):
        for bad in (None, "abc", float("nan"), float("inf"), -1):
            self.assertEqual(_format_seconds(bad), UNAVAILABLE_DURATION, repr(bad))

    def test_seconds_and_milliseconds_are_distinguished(self):
        seconds = 1_700_000_000.0
        self.assertEqual(_normalize_epoch(seconds), seconds)
        self.assertEqual(_normalize_epoch(seconds * 1000), seconds)   # ms coerced to s
        self.assertIsNone(_normalize_epoch(0))
        self.assertIsNone(_normalize_epoch("not-a-time"))


class DurationTests(unittest.TestCase):
    def test_dead_job_clock_is_frozen_at_last_sign_of_life(self):
        job = {"started_at": 1000.0, "heartbeat_at": 1060.0, "updated_at": 1060.0}
        self.assertEqual(_duration(job), 60.0)
        time.sleep(0.01)
        self.assertEqual(_duration(job), 60.0)        # does not grow with wall clock

    def test_finished_at_wins_over_heartbeat(self):
        job = {"started_at": 1000.0, "heartbeat_at": 1500.0, "finished_at": 1100.0}
        self.assertEqual(_duration(job), 100.0)

    def test_live_job_uses_the_wall_clock(self):
        job = {"started_at": time.time() - 5, "heartbeat_at": time.time()}
        self.assertGreaterEqual(_duration(job, live=True), 5.0)

    def test_negative_duration_rejected(self):
        self.assertIsNone(_duration({"started_at": 2000.0, "updated_at": 1000.0}))

    def test_invalid_timestamps_yield_unavailable(self):
        self.assertIsNone(_duration({"started_at": "x", "updated_at": 1000.0}))
        self.assertIsNone(_duration({"started_at": 1000.0}))           # no end at all
        self.assertEqual(_format_seconds(_duration(None)), UNAVAILABLE_DURATION)

    def test_ghost_job_does_not_report_thousands_of_minutes(self):
        # The real bug: started 45h ago, no finished_at, process long gone.
        job = {"started_at": time.time() - 45 * 3600, "heartbeat_at": time.time() - 45 * 3600 + 120}
        self.assertEqual(_duration(job), 120.0)
        self.assertEqual(_format_seconds(_duration(job)), "2min 00s")


class ProgressBindingTests(unittest.TestCase):
    def test_counter_is_bound_to_the_stage_that_emitted_it(self):
        snap = ProgressSnapshot()
        snap = parse_progress_line("Baixando imagens 99/99", snap)
        self.assertEqual(snap.stage, "Baixando imagens")
        self.assertEqual(snap.counter_stage, "Baixando imagens")
        # Provider setup is emitted before translation and must not relabel the
        # download counter as NVIDIA. The explicit phase marker is authoritative.
        snap = parse_progress_line("Usando NVIDIA API (nemotron) - Origem: ingles", snap)
        self.assertEqual(snap.stage, "Baixando imagens")
        self.assertEqual(snap.counter_stage, "Baixando imagens")   # counter did NOT follow
        snap = parse_progress_line("Tradução NVIDIA: iniciando", snap)
        self.assertEqual(snap.stage, "Tradução NVIDIA")

    def test_new_counter_rebinds_to_the_new_stage(self):
        snap = parse_progress_line("Baixando imagens 99/99", ProgressSnapshot())
        snap = parse_progress_line("Tradução NVIDIA: 5/40", snap)
        self.assertEqual(snap.counter_stage, "Tradução NVIDIA")
        self.assertEqual((snap.current, snap.total), (5, 40))

    def test_corrupted_explicit_translation_marker_still_advances_stage(self):
        snap = parse_progress_line("OCR: 87/87", ProgressSnapshot())
        snap = parse_progress_line("Traduï¿½ï¿½o NVIDIA: iniciando", snap)
        self.assertEqual(snap.stage, "Tradução NVIDIA")
        self.assertEqual(snap.counter_stage, "OCR")


class _Bridge(ui_bridge.UiBridge):
    """UiBridge with only the job store wired — no profile/history side effects."""

    def __init__(self, db_path):
        self.store = JobStore(db_path)
        self.history_revision = 1

    def _is_translation_job(self, job):
        return bool(job)


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bridge = _Bridge(self.tmp / "jobs.sqlite3")
        self.store = self.bridge.store

    def tearDown(self):
        self.store.close()

    def make_running(self, *, runner_pid=999999, output_dir="", exit_code=None, started=None):
        job_id = self.store.create_job(
            source_url="http://x", output_dir=str(output_dir),
            configuration={"job_type": "translation"}, command=["python", "run_webtoon.py"])
        job = {"id": job_id}
        self.store.transition(job["id"], JobStatus.CLAIMING, worker_id="w1")
        self.store.transition(job["id"], JobStatus.STARTING)
        self.store.transition(job["id"], JobStatus.RUNNING)
        self.store.update_fields(
            job["id"], runner_pid=runner_pid, runner_create_time=1.0,
            started_at=started if started is not None else time.time() - 45 * 3600,
            heartbeat_at=time.time() - 44 * 3600, exit_code=exit_code)
        return self.store.get_job(job["id"])

    @staticmethod
    def write_verified_manifest(
        output_dir: Path,
        pdf_path: Path,
        *,
        final_status: str = "finished",
        quality_passed: bool = True,
    ) -> None:
        manifest = build_run_manifest(
            run_id="reconcile-test",
            created_at="2026-07-19T00:00:00+00:00",
            source_url="https://example.test/chapter",
            commit_hash="abc123",
            branch="main",
            pipeline_version="test",
            model="fake",
            final_status=final_status,
            quality_passed=quality_passed,
            manual_review_count=0,
            rejected_count=0,
            pdf_path=str(pdf_path),
        )
        (output_dir / MANIFEST_FILENAME).write_text(
            json.dumps(manifest), encoding="utf-8"
        )

    def test_running_with_dead_pid_becomes_interrupted(self):
        job = self.make_running()
        with mock.patch.object(ui_bridge, "_runner_still_alive", return_value=False):
            out = self.bridge.reconcile_orphans()
        self.assertEqual(out[0]["status"], JobStatus.INTERRUPTED)
        self.assertEqual(out[0]["reason"], "process_not_found")
        row = self.store.get_job(job["id"])
        self.assertEqual(row["status"], JobStatus.INTERRUPTED)
        self.assertIsNotNone(row["finished_at"])         # clock frozen

    def test_live_process_is_left_alone(self):
        job = self.make_running()
        with mock.patch.object(ui_bridge, "_runner_still_alive", return_value=True):
            self.assertEqual(self.bridge.reconcile_orphans(), [])
        self.assertEqual(self.store.get_job(job["id"])["status"], JobStatus.RUNNING)

    def test_reused_pid_does_not_count_as_alive(self):
        # _runner_still_alive demands the cmdline carry job_runner.py AND the job id, so a
        # recycled pid belonging to another program fails the check.
        job = self.make_running(runner_pid=os.getpid())
        alive = ui_bridge._runner_still_alive(self.store.get_job(job["id"]))
        self.assertFalse(alive)

    def test_dead_without_manifest_is_never_marked_finished(self):
        job = self.make_running(exit_code=0, output_dir=self.tmp / "empty")
        (self.tmp / "empty").mkdir(exist_ok=True)
        with mock.patch.object(ui_bridge, "_runner_still_alive", return_value=False):
            self.bridge.reconcile_orphans()
        self.assertEqual(self.store.get_job(job["id"])["status"], JobStatus.INTERRUPTED)

    def test_dead_with_complete_evidence_is_finished(self):
        out = self.tmp / "done"
        out.mkdir()
        pdf = out / "cap.pdf"
        pdf.write_bytes(b"%PDF-" + b"x" * 2000)
        self.write_verified_manifest(out, pdf)
        job = self.make_running(exit_code=0, output_dir=out)
        with mock.patch.object(ui_bridge, "_runner_still_alive", return_value=False):
            self.bridge.reconcile_orphans()
        self.assertEqual(self.store.get_job(job["id"])["status"], JobStatus.FINISHED)

    def test_dead_with_review_required_manifest_preserves_quality_outcome(self):
        out = self.tmp / "quality-review"
        out.mkdir()
        pdf = out / "cap.pdf"
        pdf.write_bytes(b"%PDF-" + b"x" * 2000)
        self.write_verified_manifest(
            out, pdf, final_status="review_required", quality_passed=False)
        job = self.make_running(exit_code=0, output_dir=out)
        with mock.patch.object(ui_bridge, "_runner_still_alive", return_value=False):
            reconciled = self.bridge.reconcile_orphans()
        self.assertEqual(reconciled[0]["status"], JobStatus.REVIEW_REQUIRED)
        self.assertEqual(reconciled[0]["reason"], "quality_review_completed_before_shutdown")
        self.assertEqual(self.store.get_job(job["id"])["status"], JobStatus.REVIEW_REQUIRED)

    def test_legacy_manifest_filename_is_not_success_evidence(self):
        out = self.tmp / "legacy-manifest"
        out.mkdir()
        pdf = out / "cap.pdf"
        pdf.write_bytes(b"%PDF-" + b"x" * 2000)
        manifest = build_run_manifest(
            run_id="legacy-name",
            created_at="2026-07-19T00:00:00+00:00",
            source_url="https://example.test/chapter",
            commit_hash="abc123",
            branch="main",
            pipeline_version="test",
            model="fake",
            final_status="finished",
            quality_passed=True,
            manual_review_count=0,
            rejected_count=0,
            pdf_path=str(pdf),
        )
        (out / "output_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        job = self.make_running(exit_code=0, output_dir=out)
        with mock.patch.object(ui_bridge, "_runner_still_alive", return_value=False):
            self.bridge.reconcile_orphans()
        self.assertEqual(self.store.get_job(job["id"])["status"], JobStatus.INTERRUPTED)

    def test_nonzero_exit_is_not_success_even_with_verified_artifacts(self):
        out = self.tmp / "failed-exit"
        out.mkdir()
        pdf = out / "cap.pdf"
        pdf.write_bytes(b"%PDF-" + b"x" * 2000)
        self.write_verified_manifest(out, pdf)
        job = self.make_running(exit_code=1, output_dir=out)
        with mock.patch.object(ui_bridge, "_runner_still_alive", return_value=False):
            self.bridge.reconcile_orphans()
        self.assertEqual(self.store.get_job(job["id"])["status"], JobStatus.INTERRUPTED)

    def test_truncated_pdf_is_not_evidence(self):
        out = self.tmp / "bad"
        out.mkdir()
        pdf = out / "cap.pdf"
        pdf.write_bytes(b"not-a-pdf")
        self.write_verified_manifest(out, pdf)
        job = self.make_running(exit_code=0, output_dir=out)
        with mock.patch.object(ui_bridge, "_runner_still_alive", return_value=False):
            self.bridge.reconcile_orphans()
        self.assertEqual(self.store.get_job(job["id"])["status"], JobStatus.INTERRUPTED)

    def test_outputs_are_preserved(self):
        out = self.tmp / "keep"
        out.mkdir()
        for name in ("output_manifest.json", "session_context.json", "page_01.png", "run.log"):
            (out / name).write_text("data", encoding="utf-8")
        self.make_running(output_dir=out)
        with mock.patch.object(ui_bridge, "_runner_still_alive", return_value=False):
            self.bridge.reconcile_orphans()
        for name in ("output_manifest.json", "session_context.json", "page_01.png", "run.log"):
            self.assertTrue((out / name).is_file(), name)

    def test_restart_and_refresh_do_not_resurrect_processing(self):
        self.make_running()
        with mock.patch.object(ui_bridge, "_runner_still_alive", return_value=False):
            self.bridge.reconcile_orphans()
            self.store.close()
            restarted = _Bridge(self.tmp / "jobs.sqlite3")          # app_ui.py restart
            state = restarted.runtime_state()
            self.assertNotIn(state["status"], JobStatus.IN_FLIGHT)
            self.assertFalse(state["queue_running"])
            again = restarted.runtime_state()                        # browser F5
            self.assertFalse(again["queue_running"])
            self.store = restarted.store

    def test_runtime_state_freezes_the_label_for_a_ghost_job(self):
        self.make_running()
        with mock.patch.object(ui_bridge, "_runner_still_alive", return_value=False):
            progress = self.bridge.runtime_state()["progress"]
        self.assertNotIn("min", progress["elapsed_label"].replace("min 00s", "")
                         if progress["elapsed_label"].endswith("min 00s") else
                         progress["elapsed_label"] if "h " in progress["elapsed_label"] else "")
        self.assertNotEqual(progress["elapsed_label"], "")

    def test_download_counter_not_shown_under_translation(self):
        job = self.make_running()
        self.store.update_progress(job["id"], stage="Tradução NVIDIA", current=99, total=99,
                                   counter_stage="Baixando imagens")
        with mock.patch.object(ui_bridge, "_runner_still_alive", return_value=True):
            progress = self.bridge.runtime_state()["progress"]
        self.assertEqual((progress["current"], progress["total"]), (0, 0))
        self.assertIsNone(progress["fraction"])

    def test_matching_counter_is_shown(self):
        job = self.make_running()
        self.store.update_progress(job["id"], stage="Tradução NVIDIA", current=5, total=40,
                                   counter_stage="Tradução NVIDIA")
        with mock.patch.object(ui_bridge, "_runner_still_alive", return_value=True):
            progress = self.bridge.runtime_state()["progress"]
        self.assertEqual((progress["current"], progress["total"]), (5, 40))
        self.assertEqual(progress["counter_stage"], "Tradução NVIDIA")
        self.assertEqual(progress["pages"], 0)
        self.assertEqual(progress["groups"], 0)

    def test_stale_updates_are_labelled_not_invented(self):
        job = self.make_running()
        self.store._conn.execute("UPDATE jobs SET updated_at=? WHERE id=?",
                                 (time.time() - 600, job["id"]))
        with mock.patch.object(ui_bridge, "_runner_still_alive", return_value=True):
            progress = self.bridge.runtime_state()["progress"]
        self.assertTrue(progress["stale"])
        self.assertEqual(progress["stale_label"], "Sem atualização recente")


class CancelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bridge = _Bridge(self.tmp / "jobs.sqlite3")
        self.store = self.bridge.store

    def tearDown(self):
        self.store.close()

    def running_job(self):
        job_id = self.store.create_job(
            source_url="http://x", output_dir="", configuration={"job_type": "translation"},
            command=["python", "run_webtoon.py"])
        job = {"id": job_id}
        self.store.transition(job["id"], JobStatus.CLAIMING, worker_id="w1")
        self.store.transition(job["id"], JobStatus.STARTING)
        self.store.transition(job["id"], JobStatus.RUNNING)
        self.store.update_fields(job["id"], runner_pid=999999, started_at=time.time() - 100,
                                 heartbeat_at=time.time() - 90)
        return self.store.get_job(job["id"])

    @staticmethod
    def drive(coro):
        """Run a coroutine with no event loop (the offline guard blocks the self-pipe)."""
        try:
            coro.send(None)
        except StopIteration as stop:
            return stop.value
        raise AssertionError("cancel() unexpectedly awaited")

    def test_cancel_without_process_settles_the_job(self):
        job = self.running_job()
        with mock.patch.object(ui_bridge, "_runner_still_alive", return_value=False):
            self.drive(self.bridge.cancel())
        row = self.store.get_job(job["id"])
        self.assertEqual(row["status"], JobStatus.CANCELLED)
        self.assertIsNotNone(row["finished_at"])

    def test_cancelled_job_does_not_return_to_processing(self):
        self.running_job()
        with mock.patch.object(ui_bridge, "_runner_still_alive", return_value=False):
            self.drive(self.bridge.cancel())
            self.assertFalse(self.bridge.runtime_state()["queue_running"])
            self.assertFalse(self.bridge.runtime_state()["queue_running"])   # after refresh

    def test_cancel_with_live_process_only_flags_it(self):
        job = self.running_job()
        with mock.patch.object(ui_bridge, "_runner_still_alive", return_value=True):
            self.drive(self.bridge.cancel())
        row = self.store.get_job(job["id"])
        self.assertEqual(row["status"], JobStatus.CANCELLING)  # the runner owns the teardown
        self.assertTrue(self.store.cancel_requested(job["id"]))


class FrontendTimerTests(unittest.TestCase):
    def setUp(self):
        self.src = (Path(__file__).resolve().parent / "static" / "tradutor_ui.js").read_text(
            encoding="utf-8")

    def test_single_polling_interval_with_cleanup(self):
        self.assertEqual(self.src.count("setInterval("), 1)
        self.assertIn("clearInterval(getGlobal('__tradutorUiPollingTimer'))", self.src)
        self.assertIn("setGlobal('__tradutorUiPollingTimer'", self.src)

    def test_elapsed_comes_from_the_server(self):
        self.assertIn("progress.elapsed_label", self.src)
        self.assertIn("Tempo indisponível", self.src)

    def test_stale_marker_rendered(self):
        self.assertIn("Sem atualização recente", self.src)


if __name__ == "__main__":
    unittest.main()
