"""Crash-safe YK settlement: net exactly 1 YK only on valid output, 0 otherwise,
across failures, retries, duplicates and process restarts. No real YK / DeepL.
"""
from __future__ import annotations

import _test_bootstrap  # noqa: F401

import tempfile
import unittest
from pathlib import Path

import yk_reservation as ykr
from yomu_backend_provider import (BackendTranslationError, BatchItem, BatchRequest,
                                  BatchResponse, MockBackendClient, ResultStore)

AUTH = "jwt.test"
ITEMS = [{"item_id": "translation:00000", "text": "hello"}]


class _SettleProvider:
    """Minimal provider seam over the wallet-accounting mock (no auth envelope)."""

    def __init__(self, backend):
        self.backend = backend

    def settle_reservation(self, ctx, *, action, idempotency_key):
        return self.backend.settle_job(job_id=ctx["job_id"], reservation_id=ctx["reservation_id"],
                                       action=action, idempotency_key=idempotency_key, auth_token=AUTH)


def _job(backend, out, *, translate=True, job_id="job-1", request_id="req-1"):
    """Reserve + record + (optionally) deferred translation. Returns reservation_id."""
    rid = backend.reserve(job_id=job_id, request_id=request_id, auth_token=AUTH, device_id="d")["reservation_id"]
    ykr.record_reservation(out, job_id=job_id, reservation_id=rid, request_id=request_id,
                           finalize_items=ITEMS)  # legacy payload is deliberately ignored
    if translate:
        request = BatchRequest(request_id, job_id, "EN", "PT-BR",
                               (BatchItem(ITEMS[0]["item_id"], ITEMS[0]["text"]),))
        response = backend.translate_batch(request,
                                reservation_id=rid, auth_token=AUTH, finalize_job=False)
        results = ResultStore()
        results.configure_job_root(job_id, out)
        results.persist_for_job(job_id, response, request)
        ykr.mark_translation_ready(out, job_id=job_id, request_ids=[request_id], item_count=1,
                                   result_files=results.durable_evidence(job_id, [request_id]))
    return rid


class SettlementNetYK(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = self._tmp.name
        self.backend = MockBackendClient()
        self.provider = _SettleProvider(self.backend)

    def tearDown(self):
        self._tmp.cleanup()

    def _settle(self, valid):
        return ykr.settle(self.out, output_valid=valid, provider=self.provider)

    def test_valid_output_nets_one(self):
        rid = _job(self.backend, self.out)
        self.assertEqual(self.backend.net_yk(rid), 0, "held, not consumed before settle")
        self.assertEqual(self._settle(True)["net_yk"], 1)
        self.assertEqual(self.backend.net_yk(rid), 1)

    def test_handoff_persists_ids_only(self):
        _job(self.backend, self.out)
        payload = (Path(self.out) / "yk_reservation.json").read_text(encoding="utf-8")
        self.assertNotIn("finalize_items", payload)
        self.assertNotIn("hello", payload)
        self.assertEqual(set(ykr.read_reservation(self.out)),
                         {"schema_version", "job_id", "reservation_id", "request_id", "state"})

    def test_reconstruction_or_export_or_zero_or_mismatch_fail_after_provider_nets_one(self):
        rid = _job(self.backend, self.out)  # translation succeeded, output invalid downstream
        self.assertEqual(self._settle(False)["net_yk"], 1)
        self.assertEqual(self.backend.net_yk(rid), 1)

    def test_provider_fail_before_finalize_nets_zero(self):
        backend = MockBackendClient(fail="provider_rejected")
        rid = backend.reserve(job_id="j", request_id="r", auth_token=AUTH, device_id="d")["reservation_id"]
        ykr.record_reservation(self.out, job_id="j", request_id="r", reservation_id=rid, finalize_items=ITEMS)
        with self.assertRaises(BackendTranslationError):
            backend.translate_batch(BatchRequest("r", "j", "EN", "PT-BR", (BatchItem("a", "x"),)),
                                    reservation_id=rid, auth_token=AUTH, finalize_job=False)
        self.assertEqual(ykr.settle(self.out, output_valid=False, provider=_SettleProvider(backend))["net_yk"], 0)
        self.assertEqual(backend.net_yk(rid), 0)

    def test_preflight_fail_no_reservation_nets_zero(self):
        # No reservation was ever recorded (source invalid before reserve).
        self.assertEqual(self._settle(False), {"state": "no_reservation", "net_yk": 0})

    def test_zero_page_reserved_then_released_nets_zero(self):
        rid = _job(self.backend, self.out, translate=False)  # reserved, nothing translated
        self.assertEqual(self._settle(False)["net_yk"], 0)
        self.assertEqual(self.backend.net_yk(rid), 0)

    def test_duplicate_active_reservation_nets_one_total(self):
        rid1 = _job(self.backend, self.out, job_id="jx", request_id="rx")
        rid2 = _job(self.backend, self.out, job_id="jx", request_id="rx")  # double click / duplicate
        self.assertEqual(rid1, rid2)
        self.assertEqual(self.backend.reserve_count, 1)
        self.assertEqual(self._settle(True)["net_yk"], 1)
        self.assertEqual(self.backend.net_yk(rid1), 1)

    def test_retry_same_job_nets_one_total(self):
        rid = _job(self.backend, self.out)
        self.assertEqual(self._settle(True)["net_yk"], 1)
        self.assertEqual(self._settle(True)["net_yk"], 1)  # retry finalize
        self.assertEqual(self.backend.net_yk(rid), 1)

    # --- crash / restart safety (settle re-invoked from persisted state) ----------
    def test_crash_after_provider_before_output_consumes_once(self):
        rid = _job(self.backend, self.out)
        # Durable translated results are the commit point even if export did not run.
        self.assertEqual(self._settle(False)["net_yk"], 1)
        self.assertEqual(self.backend.net_yk(rid), 1)

    def test_crash_after_output_before_finalize_recovers_nets_one(self):
        rid = _job(self.backend, self.out)
        # output was valid but process died before finalize; recovery finalizes.
        self.assertEqual(self._settle(True)["net_yk"], 1)
        self.assertEqual(self.backend.net_yk(rid), 1)

    def test_crash_after_finalize_before_finished_nets_one(self):
        rid = _job(self.backend, self.out)
        self._settle(True)                                   # finalize committed
        # process died before persisting "finished"; recovery replays settle.
        self.assertEqual(self._settle(True)["net_yk"], 1)
        self.assertEqual(self.backend.net_yk(rid), 1)

    def test_double_finalize_nets_one(self):
        rid = _job(self.backend, self.out)
        self._settle(True)
        self._settle(True)
        self.assertEqual(self.backend.net_yk(rid), 1)

    def test_double_release_nets_zero(self):
        rid = _job(self.backend, self.out, translate=False)
        self._settle(False)
        self._settle(False)
        self.assertEqual(self.backend.net_yk(rid), 0)

    def test_release_then_never_consumes_even_if_output_later_seen(self):
        # A pre-provider release is terminal and cannot be reversed.
        rid = _job(self.backend, self.out, translate=False)
        self.assertEqual(self._settle(False)["net_yk"], 0)
        self.assertEqual(self._settle(True)["net_yk"], 0)  # terminal released wins
        self.assertEqual(self.backend.net_yk(rid), 0)


if __name__ == "__main__":
    unittest.main()
