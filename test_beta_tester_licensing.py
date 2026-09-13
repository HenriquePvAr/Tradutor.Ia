"""TDD #77: fail-closed Scan Beta licensing foundation."""

from __future__ import annotations

import _test_bootstrap  # noqa: F401

import tempfile
import threading
import unittest
import os
import json
from pathlib import Path
from unittest import mock

import beta_license
import job_runner
import ui_bridge
from beta_license import (
    BetaAccessDecision,
    LicenseState,
    LocalDevelopmentBetaAuthorizer,
    SQLiteBetaLicenseStore,
    StaticBetaAuthorizer,
    SupabaseBetaLicenseAuthorizer,
    SupabaseBetaLicenseConfig,
    build_beta_license_authorizer,
    stable_install_fingerprint_hash,
)
from community_auth import RequestPrincipal
from job_store import JobStatus
from test_translation_start import WEBTOON_URL, _Bridge


def drive(coro):
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("unexpected await")


class BetaLicenseStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = SQLiteBetaLicenseStore(self.tmp / "license.sqlite3")
        self.user = "00000000-0000-0000-0000-000000000001"
        self.device_a = stable_install_fingerprint_hash("install-a", "windows-pc")
        self.device_b = stable_install_fingerprint_hash("install-b", "windows-pc")
        self.device_c = stable_install_fingerprint_hash("install-c", "windows-pc")

    def tearDown(self):
        self.store.close()

    def grant(self, **overrides):
        payload = {
            "user_id": self.user,
            "status": "active",
            "starts_at": "2026-08-01T00:00:00Z",
            "expires_at": "2026-09-30T23:59:59Z",
            "max_devices": 2,
        }
        payload.update(overrides)
        return self.store.upsert_entitlement(**payload)

    def auth(self, device=None, now="2026-08-24T12:00:00Z"):
        return self.store.authorize(
            user_id=self.user,
            device_fingerprint_hash=device or self.device_a,
            now=now,
        )

    def test_authenticated_without_entitlement_denies(self):
        decision = self.auth()
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.state, LicenseState.NOT_ENTITLED)

    def test_active_entitlement_allows_until_exclusive_expiry(self):
        self.grant()
        before = self.auth(now="2026-09-30T23:59:58Z")
        self.assertTrue(before.allowed)
        boundary = self.auth(now="2026-09-30T23:59:59Z")
        self.assertFalse(boundary.allowed)
        self.assertEqual(boundary.state, LicenseState.EXPIRED)

    def test_future_start_denies(self):
        self.grant(starts_at="2026-09-01T00:00:00Z")
        decision = self.auth()
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.state, LicenseState.NOT_STARTED)

    def test_revoked_takes_precedence_over_future_expiry(self):
        self.grant(status="revoked", revoked_at="2026-08-20T00:00:00Z")
        decision = self.auth()
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.state, LicenseState.REVOKED)

    def test_device_limit_new_device_denied_existing_allowed(self):
        self.grant(max_devices=2)
        self.assertTrue(self.auth(self.device_a).allowed)
        self.assertTrue(self.auth(self.device_b).allowed)
        denied = self.auth(self.device_c)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.state, LicenseState.DEVICE_LIMIT_REACHED)
        self.assertTrue(self.auth(self.device_a).allowed)

    def test_revoked_device_slot_does_not_consume_active_limit(self):
        self.grant(max_devices=2)
        self.assertTrue(self.auth(self.device_a).allowed)
        self.assertTrue(self.auth(self.device_b).allowed)
        self.store.revoke_device(user_id=self.user, device_fingerprint_hash=self.device_a)
        revoked = self.auth(self.device_a)
        self.assertEqual(revoked.state, LicenseState.DEVICE_REVOKED)
        self.assertTrue(self.auth(self.device_c).allowed)

    def test_duplicate_device_registration_is_idempotent(self):
        self.grant(max_devices=1)
        first = self.auth(self.device_a)
        second = self.auth(self.device_a)
        self.assertTrue(first.allowed)
        self.assertTrue(second.allowed)
        rows = self.store._conn.execute("select count(*) as n from beta_tester_devices").fetchone()
        self.assertEqual(rows["n"], 1)

    def test_concurrent_last_slot_allows_exactly_one_new_device(self):
        self.grant(max_devices=1)
        results: list[BetaAccessDecision] = []
        barrier = threading.Barrier(2)

        def attempt(device):
            barrier.wait()
            results.append(self.auth(device))

        threads = [
            threading.Thread(target=attempt, args=(self.device_a,)),
            threading.Thread(target=attempt, args=(self.device_b,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(1 for item in results if item.allowed), 1)
        self.assertEqual(
            sum(1 for item in results if item.state is LicenseState.DEVICE_LIMIT_REACHED),
            1,
        )

    def test_malformed_and_unavailable_fail_closed(self):
        malformed = beta_license.validate_authorization_response({"allowed": True, "state": "bogus"})
        self.assertFalse(malformed.allowed)
        self.assertEqual(malformed.state, LicenseState.MALFORMED_LICENSE)
        unavailable = beta_license.fail_closed_from_authority_error(user_id=self.user)
        self.assertFalse(unavailable.allowed)
        self.assertTrue(unavailable.retryable)
        self.assertEqual(unavailable.state, LicenseState.LICENSE_UNAVAILABLE)

    def test_cached_active_cannot_grant_when_online_check_unavailable(self):
        cached_active = BetaAccessDecision.allow(
            user_id=self.user,
            device_fingerprint_hash=self.device_a,
            entitlement_id="cached",
            expires_at="2026-09-30T23:59:59Z",
            source="cache",
        )
        self.assertTrue(cached_active.allowed)
        online_failure = beta_license.fail_closed_from_authority_error(
            user_id=self.user, device_fingerprint_hash=self.device_a)
        self.assertFalse(online_failure.allowed)


class BetaJobGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bridge = _Bridge(self.tmp / "jobs.sqlite3")

    def tearDown(self):
        self.bridge.store.close()

    def payload(self):
        return {
            "url": WEBTOON_URL,
            "chapter_name": "Serie - Ep 1",
            "slug": "serie_ep_1",
            "mode": "fast",
            "full": True,
            "use_cache": False,
            "force": True,
            "use_context": True,
            "open_output": False,
            "pipeline_intent": {"requested": True, "mode": "fast", "scope": "full"},
        }

    def test_start_translation_denies_before_job_creation(self):
        self.bridge.beta_access_authorizer = StaticBetaAuthorizer(
            BetaAccessDecision.deny(LicenseState.EXPIRED, "license_expired")
        )
        with mock.patch.object(ui_bridge, "env_status",
                               return_value={"env_exists": True, "nvidia_configured": True}):
            with self.assertRaisesRegex(ValueError, "license_expired"):
                drive(self.bridge.start(self.payload()))
        self.assertEqual(self.bridge.store.list_jobs(limit=None), [])
        self.assertEqual(self.bridge.worker_calls, 0)

    def test_start_translation_persists_safe_authorization_metadata(self):
        self.bridge.beta_access_authorizer = StaticBetaAuthorizer(
            BetaAccessDecision.allow(
                user_id="tester",
                device_fingerprint_hash="hash",
                entitlement_id="entitlement",
                expires_at="2026-09-30T23:59:59Z",
            )
        )
        with mock.patch.object(ui_bridge, "env_status",
                               return_value={"env_exists": True, "nvidia_configured": True}):
            result = drive(self.bridge.start(self.payload()))
        job = self.bridge.store.get_job(result["job_id"])
        authorization = job["configuration"]["beta_license_authorization"]
        self.assertTrue(authorization["allowed"])
        self.assertEqual(authorization["state"], LicenseState.ACTIVE.value)
        self.assertNotIn("token", str(authorization).lower())
        self.assertNotIn("service_role", str(authorization).lower())

    def test_resume_is_protected_before_new_attempt_creation(self):
        original = self.bridge.store.create_job(
            source_url=WEBTOON_URL,
            output_dir=str(self.tmp / "out"),
            configuration={"job_type": "translation"},
            command=["python", "-c", "pass"],
            initial_status=JobStatus.QUEUED,
        )
        self.bridge.store.transition(original, JobStatus.CLAIMING, worker_id="w")
        self.bridge.store.transition(original, JobStatus.STARTING, expected_worker="w")
        self.bridge.store.transition(original, JobStatus.RUNNING, expected_worker="w")
        self.bridge.store.transition(original, JobStatus.INTERRUPTED, expected_worker="w")
        self.bridge.store.mark_resumable(original, resume_from_stage="ocr")
        status_before = self.bridge.store.get_job(original)["status"]
        self.bridge.beta_access_authorizer = StaticBetaAuthorizer(
            BetaAccessDecision.deny(LicenseState.REVOKED, "license_revoked")
        )
        with self.assertRaisesRegex(ValueError, "license_revoked"):
            self.bridge.resume(original)
        self.assertIsNone(self.bridge.store.retry_for_job(original))
        self.assertEqual(self.bridge.store.get_job(original)["status"], status_before)

    def test_runner_rejects_protected_job_without_allowed_metadata(self):
        job = {
            "configuration": {
                "beta_license_authorization": {
                    "required": True,
                    "allowed": False,
                    "state": LicenseState.EXPIRED.value,
                }
            }
        }
        self.assertEqual(
            job_runner._assert_beta_authorization_metadata(job),
            "beta_license_not_authorized",
        )


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.content = json.dumps(payload).encode("utf-8") if payload is not None else b""


class _FakeTransport:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def request(self, method, url, *, headers=None, data=None):
        self.calls.append({
            "method": method, "url": url, "headers": dict(headers or {}), "data": data})
        if self.error:
            raise self.error
        return self.response


class SupabaseBetaLicenseAuthorizerTests(unittest.TestCase):
    def principal(self):
        return RequestPrincipal(
            user_id="00000000-0000-0000-0000-000000000001",
            authenticated=True,
            auth_source="supabase",
        )

    def config(self):
        return SupabaseBetaLicenseConfig(
            url="https://example.supabase.co",
            publishable_key="sb_publishable_test",
        )

    def test_calls_rpc_with_public_key_and_user_bearer_only(self):
        transport = _FakeTransport(_FakeResponse(200, {
            "allowed": False,
            "state": "not_entitled",
            "reason_code": "not_entitled",
            "user_id": "00000000-0000-0000-0000-000000000001",
            "device_fingerprint_hash": "a" * 64,
            "checked_at": "2026-08-24T12:00:00Z",
            "retryable": False,
        }))
        authorizer = SupabaseBetaLicenseAuthorizer(
            self.config(), device_fingerprint_hash="a" * 64, transport=transport)
        decision = authorizer.authorize(principal=self.principal(), access_token="jwt.secret")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.state, LicenseState.NOT_ENTITLED)
        call = transport.calls[0]
        self.assertIn("/rest/v1/rpc/authorize_beta_tester_device", call["url"])
        self.assertEqual(call["headers"]["apikey"], "sb_publishable_test")
        self.assertEqual(call["headers"]["Authorization"], "Bearer jwt.secret")
        self.assertNotIn("service_role", str(call).lower())

    def test_missing_token_fails_closed_without_network(self):
        transport = _FakeTransport(_FakeResponse(200, {}))
        authorizer = SupabaseBetaLicenseAuthorizer(
            self.config(), device_fingerprint_hash="a" * 64, transport=transport)
        decision = authorizer.authorize(principal=self.principal())
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.state, LicenseState.LICENSE_UNAVAILABLE)
        self.assertEqual(transport.calls, [])

    def test_malformed_response_denies_without_crash(self):
        transport = _FakeTransport(_FakeResponse(200, {"allowed": True, "state": "bogus"}))
        authorizer = SupabaseBetaLicenseAuthorizer(
            self.config(), device_fingerprint_hash="a" * 64, transport=transport)
        decision = authorizer.authorize(principal=self.principal(), access_token="jwt.secret")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.state, LicenseState.MALFORMED_LICENSE)

    def test_transport_error_redacts_token_from_decision(self):
        transport = _FakeTransport(error=RuntimeError("Authorization: Bearer jwt.secret"))
        authorizer = SupabaseBetaLicenseAuthorizer(
            self.config(), device_fingerprint_hash="a" * 64, transport=transport)
        decision = authorizer.authorize(principal=self.principal(), access_token="jwt.secret")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, "license_service_unavailable")
        self.assertNotIn("jwt.secret", str(decision))

    def test_supabase_provider_selected_only_with_public_config_and_device_hash(self):
        authorizer = build_beta_license_authorizer({
            "BETA_LICENSE_PROVIDER": "supabase",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_test",
        }, device_fingerprint_hash="a" * 64, transport=_FakeTransport(_FakeResponse(200, {})))
        self.assertIsInstance(authorizer, SupabaseBetaLicenseAuthorizer)

    def test_production_fail_closed_provider_never_allows_by_default(self):
        with self.assertRaisesRegex(RuntimeError, "remote provider required"):
            build_beta_license_authorizer({"BETA_LICENSE_PROVIDER": "production"})


class UiBetaAuthorizerCompositionTests(unittest.TestCase):
    def test_ui_defaults_to_explicit_local_development_authorizer(self):
        with tempfile.TemporaryDirectory() as folder:
            authorizer = ui_bridge._build_ui_beta_access_authorizer(
                Path(folder), env={})
        self.assertIsInstance(authorizer, LocalDevelopmentBetaAuthorizer)

    def test_ui_uses_provider_aware_supabase_authorizer(self):
        with tempfile.TemporaryDirectory() as folder:
            authorizer = ui_bridge._build_ui_beta_access_authorizer(
                Path(folder),
                env={
                    "BETA_LICENSE_PROVIDER": "supabase",
                    "SUPABASE_URL": "https://example.supabase.co",
                    "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_test",
                    "TRADUTOR_INSTALL_ID": "test-install-id",
                },
                transport=_FakeTransport(_FakeResponse(200, {})),
            )
        self.assertIsInstance(authorizer, SupabaseBetaLicenseAuthorizer)

    def test_ui_supabase_provider_fails_closed_when_public_config_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, "missing: SUPABASE_URL"):
                ui_bridge._build_ui_beta_access_authorizer(
                    Path(folder),
                    env={
                        "BETA_LICENSE_PROVIDER": "supabase",
                        "TRADUTOR_INSTALL_ID": "test-install-id",
                    },
                )


class RemoteSqlContractTests(unittest.TestCase):
    def test_authorization_rpc_is_hardened_and_atomic(self):
        root = Path(__file__).resolve().parent
        sql = (root / "supabase" / "migrations" /
               "20260824192601_beta_tester_authorization_rpc.sql").read_text(
                   encoding="utf-8")
        lowered = sql.lower()
        self.assertIn("create or replace function public.authorize_beta_tester_device", lowered)
        self.assertIn("security definer", lowered)
        self.assertIn("set search_path = pg_catalog, public", lowered)
        self.assertIn("v_user_id uuid := auth.uid()", lowered)
        self.assertIn("v_checked_at timestamptz := timezone('utc', now())", lowered)
        self.assertIn("for update", lowered)
        self.assertIn("revoke all on function public.authorize_beta_tester_device", lowered)
        self.assertIn("from anon", lowered)
        self.assertIn("grant execute on function public.authorize_beta_tester_device", lowered)
        self.assertIn("to authenticated", lowered)
        self.assertIn("revoke insert, update, delete on public.beta_tester_devices", lowered)
        self.assertNotIn("p_user_id", lowered)

    def test_grants_hardening_revokes_raw_table_privileges(self):
        root = Path(__file__).resolve().parent
        sql = (root / "supabase" / "migrations" /
               "20260824192729_beta_tester_grants_hardening.sql").read_text(
                   encoding="utf-8").lower()
        self.assertIn(
            "revoke all privileges on public.beta_tester_entitlements from anon, authenticated",
            sql,
        )
        self.assertIn(
            "revoke all privileges on public.beta_tester_devices from anon, authenticated",
            sql,
        )
        self.assertIn(
            "revoke all privileges on public.beta_tester_license_events from anon, authenticated",
            sql,
        )
        self.assertIn("grant select on public.beta_tester_devices to authenticated", sql)


class BetaSecurityStaticScanTests(unittest.TestCase):
    def test_client_code_has_no_service_role_or_admin_secret_runtime_use(self):
        root = Path(__file__).resolve().parent
        suspicious = []
        needles = ("SUPABASE_SERVICE_ROLE", "service_role", "private signing key")
        allowed_files = {"test_beta_tester_licensing.py", "supabase_auth.py", "community_auth.py"}
        client_paths = [
            root / "beta_license.py",
            root / "ui_bridge.py",
            root / "job_runner.py",
            root / "app_ui.py",
        ]
        for path in client_paths:
            text = "\n".join(
                line for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
                if not line.lstrip().startswith("#")
            )
            for needle in needles:
                if needle.lower() in text.lower() and path.name not in allowed_files:
                    suspicious.append((path.name, needle))
        self.assertEqual(suspicious, [])


if __name__ == "__main__":
    unittest.main()
