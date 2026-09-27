"""Backend-contract state machine tests. No real YK or provider/network calls."""
from __future__ import annotations

import _test_bootstrap  # noqa: F401

import unittest
from pathlib import Path
from yomu_backend_provider import BackendTranslationError, BatchItem, BatchRequest, MockBackendClient

AUTH = "jwt.test"


def reserve(backend, job="job-1", request="req-1"):
    return backend.reserve(job_id=job, request_id=request, auth_token=AUTH)["reservation_id"]


def batch(job="job-1", request="req-1"):
    return BatchRequest(request, job, "EN", "PT-BR", (BatchItem("i1", "hello"),))


def settle(backend, rid, action, job="job-1", key=None):
    return backend.settle_job(job_id=job, reservation_id=rid, action=action,
                              idempotency_key=key or f"yk-settlement-v1:{job}:{action}",
                              auth_token=AUTH)


class AtomicWalletSettlement(unittest.TestCase):
    def setUp(self):
        self.backend = MockBackendClient()
        self.rid = reserve(self.backend)

    def provider_success(self):
        self.backend.translate_batch(batch(), reservation_id=self.rid, auth_token=AUTH,
                                     finalize_job=False)

    def test_provider_success_stays_held_then_output_consume_nets_one(self):
        self.provider_success()
        self.assertEqual(self.backend.net_yk(self.rid), 0)
        self.assertEqual(settle(self.backend, self.rid, "consume")["status"], "consumed")
        self.assertEqual(self.backend.net_yk(self.rid), 1)

    def test_release_after_provider_completed_nets_zero(self):
        self.provider_success()
        with self.assertRaisesRegex(BackendTranslationError, "provider_success_cannot_be_released"):
            settle(self.backend, self.rid, "release")
        self.assertEqual(self.backend.net_yk(self.rid), 0)

    def test_consume_release_and_repeat_are_idempotent(self):
        self.provider_success()
        key = "yk-settlement-v1:stable-operation:consume"
        settle(self.backend, self.rid, "consume", key=key)
        self.assertTrue(settle(self.backend, self.rid, "consume", key=key)["idempotent"])
        with self.assertRaisesRegex(BackendTranslationError, "terminal_conflict"):
            settle(self.backend, self.rid, "release")
        self.assertEqual(self.backend.net_yk(self.rid), 1)

    def test_release_twice_and_consume_after_release_never_charge(self):
        settle(self.backend, self.rid, "release")
        self.assertEqual(settle(self.backend, self.rid, "release")["status"], "released")
        with self.assertRaisesRegex(BackendTranslationError, "terminal_conflict"):
            settle(self.backend, self.rid, "consume")
        self.assertEqual(self.backend.net_yk(self.rid), 0)

    def test_consume_requires_provider_success(self):
        with self.assertRaisesRegex(BackendTranslationError, "not_complete"):
            settle(self.backend, self.rid, "consume")
        self.assertEqual(self.backend.net_yk(self.rid), 0)

    def test_wrong_job_association_is_denied(self):
        with self.assertRaisesRegex(BackendTranslationError, "mismatch"):
            settle(self.backend, self.rid, "release", job="other-job")

    def test_wrong_authenticated_user_is_denied(self):
        with self.assertRaisesRegex(BackendTranslationError, "not_found"):
            self.backend.settle_job(job_id="job-1", reservation_id=self.rid,
                                    action="release", idempotency_key="yk-settlement-v1:wrong-owner:release",
                                    auth_token="different-user-jwt")

    def test_provider_cannot_implicitly_settle(self):
        with self.assertRaisesRegex(BackendTranslationError, "wallet_settlement_required"):
            self.backend.translate_batch(batch(), reservation_id=self.rid, auth_token=AUTH,
                                         finalize_job=True)

    def test_migration_requires_persisted_results_and_forbids_release_after_provider_success(self):
        migration = (Path(__file__).parent / "supabase/migrations/20260926175232_yk_job_atomic_settlement.sql").read_text(encoding="utf-8")
        self.assertIn("set search_path = ''", migration)
        self.assertIn("v_result_persisted <> v_requests", migration)
        self.assertIn("v_completed > 0 or v_result_persisted > 0", migration)
        self.assertIn("provider_success_cannot_be_released", migration)
        self.assertIn("if v_user is null then raise exception 'unauthorized'", migration)
        self.assertIn("from public, anon", migration)


if __name__ == "__main__":
    unittest.main()
