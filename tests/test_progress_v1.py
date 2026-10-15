"""Authoritative progress.v1 contract and bounded runner pump regressions."""

import test_support._test_bootstrap as _test_bootstrap  # noqa: F401

import io
import json
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

from yomu.runtime.job_runner import _OutputPump, _join_output_pump_and_drain
from yomu.runtime.job_store import JobStatus, JobStore, ProgressEventRejected, SCHEMA_VERSION
from yomu.runtime.progress_contract import (
    PROGRESS_V1_PREFIX, emit_progress_v1, parse_progress_v1_line,
)
import yomu.ui.ui_bridge as ui_bridge
import yomu.pipeline.benchmark_pipeline as benchmark_pipeline


class _ProgressBridge(ui_bridge.UiBridge):
    def __init__(self, db_path):
        self.store = JobStore(db_path)
        self.history_revision = 1

    def _is_translation_job(self, job):
        return bool(job)


class ProgressV1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = JobStore(self.tmp / "jobs.sqlite3")
        self.job_id = self.store.create_job(
            source_url="synthetic", output_dir=str(self.tmp / "out"), command=[],
            configuration={"trace_id": "trace-test"},
        )
        self.store.claim_next_job("worker", 1)
        self.store.transition(self.job_id, JobStatus.STARTING, expected_worker="worker")
        self.store.transition(self.job_id, JobStatus.RUNNING, expected_worker="worker")
        self.run_id = self.store.get_job(self.job_id)["run_id"]

    def tearDown(self):
        self.store.close()

    def event(self, seq, *, stage="ocr", current=1, total=3, **overrides):
        event = {
            "schema_version": 1, "job_id": self.job_id, "run_id": self.run_id,
            "trace_id": "trace-test", "event_id": f"event-{seq}", "event_seq": seq,
            "emitted_at": float(seq), "stage_id": stage, "phase": "work",
            "status": "running", "stage_current": current, "stage_total": total,
            "completed_page_ids": [], "current_page": None, "operation": "",
            "error_code": "", "retryable": False, "terminal": False,
        }
        event.update(overrides)
        return event

    def test_schema_migrates_and_late_ocr_does_not_move_visible_render_stage(self):
        version = self.store._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()["value"]
        self.assertEqual(int(version), SCHEMA_VERSION)
        self.store.apply_progress_event(self.event(
            1, stage="rendering", current=1, total=183, current_page=1,
            operation="render_page", completed_page_ids=["page:1"],
        ))
        row = self.store.apply_progress_event(self.event(2, stage="ocr", current=20, total=183))
        self.assertEqual(row["stage"], "rendering")
        self.assertEqual(row["progress_display_stage"], "rendering")
        self.assertEqual(row["progress_counter_stage"], "ocr")
        self.assertEqual(row["progress_current"], 1)
        self.assertEqual(row["progress_stage_counters"]["ocr"]["current"], 20)

    def test_rejected_counter_consumes_sequence_and_later_valid_event_recovers(self):
        self.store.apply_progress_event(self.event(1, current=20, total=183))
        with self.assertRaises(ProgressEventRejected) as caught:
            self.store.apply_progress_event(self.event(2, current=19, total=183))
        self.assertEqual(str(caught.exception), "progress_counter_regression")
        self.assertTrue(caught.exception.sequence_consumed)
        row = self.store.get_job(self.job_id)
        self.assertEqual(row["progress_last_event_seq"], 1)
        self.assertEqual(row["progress_received_seq"], 2)
        self.assertEqual(row["progress_rejected_event_id"], "event-2")
        accepted = self.store.apply_progress_event(self.event(3, current=21, total=183))
        self.assertEqual(accepted["progress_last_event_seq"], 3)
        self.assertEqual(accepted["progress_current"], 21)

    def test_parser_rejects_duplicate_keys_and_boolean_schema(self):
        payload = {
            "schema_version": 1, "stage_id": "ocr", "phase": "recognition",
            "status": "running", "stage_current": 0, "stage_total": 3,
            "completed_page_ids": [], "current_page": None, "operation": "",
            "error_code": "", "retryable": False, "terminal": False,
        }
        encoded = json.dumps(payload, separators=(",", ":"))
        self.assertIsNotNone(parse_progress_v1_line(PROGRESS_V1_PREFIX + encoded))
        self.assertIsNone(parse_progress_v1_line(PROGRESS_V1_PREFIX + encoded.replace(
            '"schema_version":1', '"schema_version":true')))
        duplicate = encoded.replace('"stage_id":"ocr"', '"stage_id":"ocr","stage_id":"rendering"')
        self.assertIsNone(parse_progress_v1_line(PROGRESS_V1_PREFIX + duplicate))
        malformed_status = dict(payload, status=[])
        self.assertIsNone(parse_progress_v1_line(
            PROGRESS_V1_PREFIX + json.dumps(malformed_status, separators=(",", ":"))
        ))

    def test_emitter_writes_parseable_bounded_machine_event(self):
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            emit_progress_v1(
                stage_id="ocr", phase="recognition", status="running",
                stage_current=2, stage_total=4, current_page=3,
            )
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        parsed = parse_progress_v1_line(output.getvalue().strip())
        self.assertEqual(parsed["stage_current"], 2)
        self.assertEqual(parsed["current_page"], 3)

    def test_pre_translation_progress_callback_observes_worker_completions(self):
        states = [{"index": index, "image_path": f"{index}.png", "raw_lines": []}
                  for index in (1, 2, 3)]
        seen = []

        def fake_page(context, **_kwargs):
            return SimpleNamespace(
                page_index=context.page_index, page_state={}, timings={}, counters={},
                page_recorder=None, page_profiler=None,
            )

        with mock.patch.object(benchmark_pipeline, "_pipeline_page_workers", return_value=2), \
                mock.patch.object(benchmark_pipeline, "process_pre_translation_page", side_effect=fake_page):
            results = benchmark_pipeline.process_pre_translation_pages(
                states, image_paths=[], ocr_lang="eng", fast_ocr_budget=None,
                errors_folder=Path(self.tmp), progress_callback=lambda result: seen.append(result.page_index),
            )
        self.assertEqual([result.page_index for result in results], [1, 2, 3])
        self.assertEqual(sorted(seen), [1, 2, 3])

    def test_legacy_updates_cannot_overwrite_terminal_or_canonical_snapshot(self):
        self.store.apply_progress_event(self.event(1, current=2, total=3))
        self.assertFalse(self.store.update_progress(
            self.job_id, stage="ocr", current=1, total=3, run_id=self.run_id,
        ))
        self.store.transition(
            self.job_id, JobStatus.FAILED, expected_worker="worker", stage="failed",
        )
        self.assertFalse(self.store.update_progress(
            self.job_id, stage="ocr", current=3, total=3, run_id=self.run_id,
        ))
        row = self.store.get_job(self.job_id)
        self.assertEqual(row["status"], JobStatus.FAILED)
        self.assertEqual(row["progress_display_stage"], "ocr")
        self.assertTrue(row["progress_terminal"])

    def test_runner_pump_drains_burst_and_parent_binds_host_identity(self):
        child = (
            "from yomu.runtime.progress_contract import emit_progress_v1; "
            "[emit_progress_v1(stage_id='ocr',phase='recognition',status='running',"
            "stage_current=i,stage_total=300) for i in range(1,301)]"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", child], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        pump = _OutputPump(
            proc.stdout, io.StringIO(), self.store, self.job_id,
            run_id=self.run_id, trace_id="trace-test",
        )
        pump.start()
        rejected = []

        def consume(events):
            for event in events:
                try:
                    self.store.apply_progress_event(event)
                except ProgressEventRejected as exc:
                    rejected.append(str(exc))

        _join_output_pump_and_drain(pump, consume)
        self.assertEqual(proc.wait(timeout=3), 0)
        self.assertFalse(rejected)
        row = self.store.get_job(self.job_id)
        self.assertEqual(row["progress_last_event_seq"], 300)
        self.assertEqual(row["progress_current"], 300)
        self.assertEqual(row["progress_trace_id"], "trace-test")

    def test_bridge_projects_progress_fields_and_terminal_snapshot(self):
        bridge = _ProgressBridge(self.tmp / "bridge.sqlite3")
        try:
            job_id = bridge.store.create_job(
                source_url="synthetic", output_dir=str(self.tmp / "bridge-out"), command=[],
                configuration={"trace_id": "bridge-trace", "job_type": "translation"},
            )
            bridge.store.claim_next_job("bridge-worker", 1)
            bridge.store.transition(job_id, JobStatus.STARTING, expected_worker="bridge-worker")
            bridge.store.transition(job_id, JobStatus.RUNNING, expected_worker="bridge-worker")
            row = bridge.store.get_job(job_id)
            event = self.event(
                1, stage="rendering", current=2, total=10, current_page=4,
                operation="render_page", completed_page_ids=["page:4"],
            )
            event.update(job_id=job_id, run_id=row["run_id"], trace_id="bridge-trace")
            bridge.store.apply_progress_event(event)
            record = bridge._job_record(bridge.store.get_job(job_id))
            self.assertEqual(record["progress_version"], 1)
            self.assertEqual(record["progress_completed_page_count"], 1)
            bridge.store.transition(
                job_id, JobStatus.FAILED, expected_worker="bridge-worker", stage="failed",
            )
            with mock.patch.object(ui_bridge, "_runner_still_alive", return_value=True):
                snapshot = bridge._runtime_state(cursor=0, owner_id=None)["progress"]
            self.assertTrue(snapshot["terminal"])
            self.assertEqual(snapshot["stage_key"], "rendering")
            self.assertEqual((snapshot["current"], snapshot["total"]), (2, 10))
            self.assertEqual(snapshot["completed_page_count"], 1)
        finally:
            bridge.store.close()
