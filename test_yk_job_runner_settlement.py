"""job_runner YK settlement hook: the output contract decides finalize vs release.

Proves _settle_yk_reservation consumes on valid output and releases on failure,
is guarded (no reservation / no provider are safe no-ops), and never raises.
Uses the wallet-accounting mock; no real YK / DeepL.
"""
from __future__ import annotations

import _test_bootstrap  # noqa: F401

import tempfile
import unittest
import json
from pathlib import Path

import job_runner
import yk_reservation as ykr
from yomu_backend_provider import BatchItem, BatchRequest, MockBackendClient, ResultStore
from job_store import JobStatus, JobStore

AUTH = "jwt.test"
ITEMS = [{"item_id": "translation:00000", "text": "hello"}]


class _SettleProvider:
    def __init__(self, backend):
        self.backend = backend

    def settle_reservation(self, ctx, *, action, idempotency_key):
        return self.backend.settle_job(job_id=ctx["job_id"], reservation_id=ctx["reservation_id"],
                                       action=action, idempotency_key=idempotency_key, auth_token=AUTH)


class JobRunnerSettlement(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        self.backend = MockBackendClient()
        self._orig = job_runner._build_yk_settlement_provider
        self._orig_contract = job_runner._requested_output_contract
        self._orig_commit_mismatch = job_runner._pipeline_commit_mismatch
        job_runner._build_yk_settlement_provider = lambda job: _SettleProvider(self.backend)

    def tearDown(self):
        job_runner._build_yk_settlement_provider = self._orig
        job_runner._requested_output_contract = self._orig_contract
        job_runner._pipeline_commit_mismatch = self._orig_commit_mismatch
        self._tmp.cleanup()

    def _held(self, job_id="j", request_id="r"):
        rid = self.backend.reserve(job_id=job_id, request_id=request_id, auth_token=AUTH, device_id="d")["reservation_id"]
        ykr.record_reservation(self.out, job_id=job_id, reservation_id=rid,
                               request_id=request_id, finalize_items=ITEMS)
        request = BatchRequest(request_id, job_id, "EN", "PT-BR",
                               (BatchItem(ITEMS[0]["item_id"], ITEMS[0]["text"]),))
        response = self.backend.translate_batch(request,
                                     reservation_id=rid, auth_token=AUTH, finalize_job=False)
        self._mark_ready(job_id, request_id, response, self.out)
        return rid

    def _mark_ready(self, job_id, request_id, response, output):
        cache = ResultStore(); cache.configure_job_root(job_id, output); cache.persist_for_job(job_id, response)
        ykr.mark_translation_ready(output, job_id=job_id, request_ids=[request_id], item_count=1,
                                   result_files=cache.durable_evidence(job_id, [request_id]))

    def test_valid_output_consumes(self):
        rid = self._held()
        result = job_runner._settle_yk_reservation({}, self.out, output_valid=True)
        self.assertEqual(result["net_yk"], 1)
        self.assertEqual(self.backend.net_yk(rid), 1)

    def test_invalid_output_still_consumes_after_provider_result_is_durable(self):
        rid = self._held()
        result = job_runner._settle_yk_reservation({}, self.out, output_valid=False)
        self.assertEqual(result["net_yk"], 1)
        self.assertEqual(self.backend.net_yk(rid), 1)

    def test_no_reservation_is_noop(self):
        self.assertEqual(job_runner._settle_yk_reservation({}, self.out, output_valid=True)["state"],
                         "no_reservation")

    def test_provider_unavailable_leaves_reservation_recoverable(self):
        self._held()
        job_runner._build_yk_settlement_provider = lambda job: None
        result = job_runner._settle_yk_reservation({}, self.out, output_valid=True)
        self.assertEqual(result["state"], "settlement_deferred")
        # State remains held -> a later settlement can still consume it.
        self.assertEqual(ykr.read_reservation(self.out)["state"], "held")

    def test_settlement_never_raises_on_provider_error(self):
        self._held()
        class _Boom:
            def settle_reservation(self, ctx, *, action, idempotency_key): raise RuntimeError("boom")
        job_runner._build_yk_settlement_provider = lambda job: _Boom()
        result = job_runner._settle_yk_reservation({}, self.out, output_valid=True)
        self.assertEqual(result["state"], "settle_error")  # guarded, no exception

    def test_runner_crash_before_output_recovery_consumes_and_preserves_recovery(self):
        rid = self._held()
        job_runner._requested_output_contract = lambda *_: (False, "export_zero_output")
        job_runner._pipeline_commit_mismatch = lambda *_: {}
        job = {"id": "j", "output_dir": str(self.out), "configuration": {"output_format": "pdf"}}
        result = job_runner.recover_yk_settlement(job)
        self.assertEqual(result["state"], "consumed")
        self.assertTrue(result["output_recovery_available"])
        self.assertEqual(self.backend.net_yk(rid), 1)

    def test_runner_crash_after_valid_output_recovers_consume(self):
        rid = self._held()
        job_runner._requested_output_contract = lambda *_: (True, "")
        job_runner._pipeline_commit_mismatch = lambda *_: {}
        job = {"id": "j", "output_dir": str(self.out), "configuration": {"output_format": "pdf"}}
        result = job_runner.recover_yk_settlement(job)
        self.assertEqual(result["state"], "consumed")
        self.assertEqual(self.backend.net_yk(rid), 1)

    def test_recovery_after_consume_before_terminal_is_idempotent(self):
        rid = self._held()
        self.assertEqual(job_runner._settle_yk_reservation({}, self.out, output_valid=True)["net_yk"], 1)
        job_runner._requested_output_contract = lambda *_: (True, "")
        job_runner._pipeline_commit_mismatch = lambda *_: {}
        job = {"id": "j", "output_dir": str(self.out), "configuration": {"output_format": "pdf"}}
        job_runner.recover_yk_settlement(job)
        self.assertEqual(self.backend.net_yk(rid), 1)

    def _running_store_job(self, output_dir):
        store = JobStore(self.out / "jobs.sqlite3")
        job_id = store.create_job(source_url="https://reader.example/chapter",
                                  output_dir=str(output_dir), command=["fake"],
                                  configuration={"mode": "quality", "output_format": "pdf"})
        store.claim_next_job("worker", 1)
        store.transition(job_id, JobStatus.STARTING, expected_worker="worker")
        store.transition(job_id, JobStatus.RUNNING, expected_worker="worker")
        return store, store.get_job(job_id)

    def test_full_provider_to_finalize_valid_output_consumes_once(self):
        output = self.out / "integrated-output"
        store, job = self._running_store_job(output)
        rid = self.backend.reserve(job_id=job["id"], request_id="req", auth_token=AUTH)["reservation_id"]
        ykr.record_reservation(output, job_id=job["id"], reservation_id=rid)
        response = self.backend.translate_batch(BatchRequest("req", job["id"], "EN", "PT-BR",
                                                  (BatchItem("i", "source"),)),
                                     reservation_id=rid, auth_token=AUTH, finalize_job=False)
        self._mark_ready(job["id"], "req", response, output)
        output.mkdir(parents=True, exist_ok=True)
        pdf = output / "chapter.pdf"
        pdf.write_bytes(b"%PDF-1.4\nphysical-result")
        (output / "timing_report.json").write_text(json.dumps({
            "pdf_path": str(pdf), "quality_validation": {"passed": True},
        }), encoding="utf-8")
        (output / "downloaded_images.json").write_text("{}", encoding="utf-8")
        job_runner._build_yk_settlement_provider = lambda job: _SettleProvider(self.backend)
        try:
            rc = job_runner._finalize(store, job["id"], job, output, 0, False,
                                      str(self.out / "runner.log"))
            self.assertEqual(rc, 0)
            self.assertEqual(store.get_job(job["id"])["status"], JobStatus.FINISHED)
            self.assertEqual(self.backend.net_yk(rid), 1)
            self.assertEqual(self.backend.provider_execution_count, 1)
        finally:
            store.close()

    def test_full_provider_to_finalize_missing_output_is_recoverable_after_provider_success(self):
        output = self.out / "integrated-failure"
        store, job = self._running_store_job(output)
        rid = self.backend.reserve(job_id=job["id"], request_id="req-fail", auth_token=AUTH)["reservation_id"]
        ykr.record_reservation(output, job_id=job["id"], reservation_id=rid)
        response = self.backend.translate_batch(BatchRequest("req-fail", job["id"], "EN", "PT-BR",
                                                  (BatchItem("i", "source"),)),
                                     reservation_id=rid, auth_token=AUTH, finalize_job=False)
        self._mark_ready(job["id"], "req-fail", response, output)
        output.mkdir(parents=True, exist_ok=True)
        (output / "timing_report.json").write_text("{}", encoding="utf-8")
        (output / "downloaded_images.json").write_text("{}", encoding="utf-8")
        job_runner._build_yk_settlement_provider = lambda job: _SettleProvider(self.backend)
        try:
            job_runner._finalize(store, job["id"], job, output, 1, False,
                                 str(self.out / "runner.log"))
            self.assertEqual(store.get_job(job["id"])["status"], JobStatus.INTERRUPTED)
            self.assertEqual(self.backend.net_yk(rid), 1)
            self.assertEqual(store.get_job(job["id"])["recoverable"], 1)
        finally:
            store.close()

    def test_cancel_with_valid_artifact_still_releases_reservation(self):
        output = self.out / "integrated-cancel"
        store, job = self._running_store_job(output)
        store.transition(job["id"], JobStatus.CANCELLING, expected_worker="worker")
        job = store.get_job(job["id"])
        rid = self.backend.reserve(job_id=job["id"], request_id="req-cancel", auth_token=AUTH)["reservation_id"]
        ykr.record_reservation(output, job_id=job["id"], reservation_id=rid)
        response = self.backend.translate_batch(BatchRequest("req-cancel", job["id"], "EN", "PT-BR",
                                                  (BatchItem("i", "source"),)),
                                     reservation_id=rid, auth_token=AUTH, finalize_job=False)
        self._mark_ready(job["id"], "req-cancel", response, output)
        output.mkdir(parents=True, exist_ok=True)
        pdf = output / "chapter.pdf"
        pdf.write_bytes(b"%PDF-1.4\nphysical-result")
        (output / "timing_report.json").write_text(json.dumps({
            "pdf_path": str(pdf), "quality_validation": {"passed": True},
        }), encoding="utf-8")
        (output / "downloaded_images.json").write_text("{}", encoding="utf-8")
        job_runner._build_yk_settlement_provider = lambda job: _SettleProvider(self.backend)
        try:
            job_runner._finalize(store, job["id"], job, output, 130, True,
                                 str(self.out / "runner.log"))
            self.assertEqual(store.get_job(job["id"])["status"], JobStatus.CANCELLED)
            self.assertEqual(self.backend.net_yk(rid), 1)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
