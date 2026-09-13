"""Hermetic contracts for workspace-scoped source authorization."""
from __future__ import annotations
import _test_bootstrap  # noqa: F401
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from job_store import JobStatus, JobStore
from source_readiness import (
    PIPELINE_OPERATIONS, WORKSPACE_POLICY_SCOPE_INDEX, SourceReadinessStore,
    default_workspace_id, download_fixture_assets,
)

STATEMENT = "Workspace restricted to authorized sources."


def corrupt_policy_scope_index(db_path: Path) -> None:
    """Damage exactly the policy scope index b-tree, leaving every row intact.

    This reproduces the observed production failure: one index root page becomes
    unreadable, so every scoped policy statement raises ``sqlite3.DatabaseError``
    while the table itself still holds the persisted authorization.
    """
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        root = conn.execute(
            "SELECT rootpage FROM sqlite_master WHERE type='index' AND name=?",
            (WORKSPACE_POLICY_SCOPE_INDEX,)).fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    finally:
        conn.close()
    with open(db_path, "r+b") as handle:
        handle.seek((root - 1) * page_size)
        handle.write(b"\xff" * page_size)


class WorkspacePolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "jobs.sqlite3"
        self.store = SourceReadinessStore(self.db)
        self.workspace = default_workspace_id(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def activate(self, owner="local"):
        return self.store.activate_workspace_policy(
            owner=owner, workspace_id=self.workspace, created_by=owner,
            authorization_statement="Workspace restricted to authorized sources.")

    def test_creation_persistence_hash_owner_workspace_and_publish_denial(self):
        policy = self.activate()
        restored = self.store.active_workspace_policy(owner="local", workspace_id=self.workspace)
        self.assertEqual(restored, policy)
        self.assertTrue(policy.all_submitted_sources_authorized)
        self.assertEqual(policy.default_rights_basis, "explicit_permission")
        self.assertEqual(set(policy.allowed_operations), set(PIPELINE_OPERATIONS))
        self.assertEqual(policy.denied_operations, ("publish",))
        self.assertEqual(len(policy.policy_hash), 64)

    def test_activation_is_idempotent_and_history_is_append_only(self):
        first, second = self.activate(), self.activate()
        self.assertEqual(first.policy_id, second.policy_id)
        self.assertEqual(len(self.store.workspace_policy_history(
            owner="local", workspace_id=self.workspace)), 1)

    def test_policy_is_owner_and_workspace_scoped(self):
        self.activate()
        self.assertIsNone(self.store.active_workspace_policy(
            owner="other", workspace_id=self.workspace))
        self.assertIsNone(self.store.active_workspace_policy(
            owner="local", workspace_id="ws_" + "f" * 32))

    def test_revocation_preserves_history_and_blocks_restore(self):
        active = self.activate()
        revoked = self.store.revoke_workspace_policy(
            owner="local", workspace_id=self.workspace, revoked_by="local")
        self.assertEqual(revoked.policy_id, active.policy_id)
        self.assertEqual(revoked.status, "revoked")
        self.assertIsNone(self.store.active_workspace_policy(
            owner="local", workspace_id=self.workspace))
        self.assertEqual(len(self.store.workspace_policy_history(
            owner="local", workspace_id=self.workspace)), 2)


class WorkspaceAuthorizedTransitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "jobs.sqlite3"
        self.jobs = JobStore(self.db)
        self.ready = SourceReadinessStore(self.db)
        self.workspace = default_workspace_id(self.db)
        self.policy = self.ready.activate_workspace_policy(
            owner="local", workspace_id=self.workspace, created_by="local",
            authorization_statement="Workspace restricted to authorized sources.")

    def tearDown(self):
        self.ready.close()
        self.jobs.close()
        self.tmp.cleanup()

    def create_ready(self, *, mode="fast", full=True, max_images=None,
                     download_only=False, requested=True):
        job_id = self.jobs.create_job(
            source_url="https://fixture.invalid/chapter",
            output_dir=str(Path(self.tmp.name) / "output"),
            command=["python", "fixture.py"], run_id="operation-a",
            configuration={
                "job_type": "translation", "workspace_owner": "local",
                "workspace_id": self.workspace, "user_requested_pipeline": requested,
                "requested_mode": mode,
                "requested_scope": "full" if full else str(max_images),
                "effective_scope": "full" if full else str(max_images),
                "scope_source": "user", "scope_hash": "c" * 64,
                "requested_operations": (
                    ["analyze_metadata", "download_assets"] if download_only
                    else list(PIPELINE_OPERATIONS)),
                "mode": mode, "download_only": download_only,
                "full": full, "max_images": max_images,
            })
        self.jobs.transition(job_id, JobStatus.CLAIMING)
        result = self.ready.persist_analysis({
            "owner": "local", "operation_id": "operation-a", "attempt": 1,
            "normalized_url_hash": "a" * 64, "adapter": "fixture",
            "source_kind": "local_test_fixture", "preflight_result_id": "fixture",
            "preflight_status": "ready", "status": "source_analysis_ready",
            "reason_code": "source_structure_compatible", "policy_hash": "b" * 64,
        })
        config = self.jobs.get_job(job_id)["configuration"]
        config["source_analysis_result_id"] = result.analysis_id
        self.jobs.transition(
            job_id, JobStatus.SOURCE_ANALYSIS_READY,
            configuration_json=json.dumps(config),
            source_selection_json=json.dumps({"candidate_ids": ["p1", "p2"]}),
            reason_code="workspace_policy_resolution_pending")
        return job_id, result

    def test_worker_handoff_keeps_claim_nonclaimable(self):
        job_id, _ = self.create_ready()
        self.jobs.update_fields(job_id, worker_id="worker-a", worker_pid=101)
        outcome = self.ready.resolve_ready_pipeline(job_id, handoff_worker_id="worker-a")
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["status"], JobStatus.CLAIMING)
        self.assertIsNone(self.jobs.claim_next_job("worker-b", 202))

    def test_ready_pipeline_is_authorized_and_enqueued_atomically(self):
        job_id, result = self.create_ready()
        outcome = self.ready.resolve_ready_pipeline(job_id)
        decision = self.ready.latest_authorization(
            owner="local", analysis_result_id=result.analysis_id)
        self.assertEqual(outcome["status"], "queued")
        self.assertEqual(self.jobs.get_job(job_id)["status"], JobStatus.QUEUED)
        self.assertEqual(decision.authorization_source, "workspace_policy")
        self.assertEqual(decision.policy_id, self.policy.policy_id)
        self.assertNotIn("publish", decision.allowed_operations)

    def test_repeated_resolution_creates_one_authorization_and_one_job(self):
        job_id, result = self.create_ready()
        first = self.ready.resolve_ready_pipeline(job_id)
        second = self.ready.resolve_ready_pipeline(job_id)
        self.assertEqual(first["authorization_id"], second["authorization_id"])
        self.assertEqual(len(self.jobs.list_jobs(limit=None)), 1)
        self.assertEqual(self.ready.authorization_count(
            owner="local", analysis_result_id=result.analysis_id), 1)

    def test_download_only_cannot_gain_ocr_translation_or_pdf(self):
        job_id, result = self.create_ready(download_only=True)
        self.ready.resolve_ready_pipeline(job_id)
        decision = self.ready.latest_authorization(
            owner="local", analysis_result_id=result.analysis_id)
        self.assertEqual(decision.allowed_operations, ("analyze_metadata", "download_assets"))

    def test_partial_scope_is_preserved(self):
        job_id, _ = self.create_ready(full=False, max_images=5)
        self.ready.resolve_ready_pipeline(job_id)
        config = self.jobs.get_job(job_id)["configuration"]
        self.assertEqual(config["requested_scope"], "5")
        self.assertEqual(config["effective_scope"], "5")

    def test_disabled_policy_blocks_without_authorization_or_enqueue(self):
        self.ready.revoke_workspace_policy(
            owner="local", workspace_id=self.workspace, revoked_by="local")
        job_id, result = self.create_ready()
        outcome = self.ready.resolve_ready_pipeline(job_id)
        self.assertEqual(outcome["reason_code"], "workspace_source_authorization_required")
        self.assertEqual(self.jobs.get_job(job_id)["status"], JobStatus.SOURCE_ANALYSIS_READY)
        self.assertEqual(self.ready.authorization_count(
            owner="local", analysis_result_id=result.analysis_id), 0)

    def test_background_analysis_without_user_intent_never_continues(self):
        job_id, result = self.create_ready(requested=False)
        outcome = self.ready.resolve_ready_pipeline(job_id)
        self.assertEqual(outcome["reason_code"], "pipeline_intent_required")
        self.assertEqual(self.jobs.get_job(job_id)["status"], JobStatus.SOURCE_ANALYSIS_READY)
        self.assertEqual(self.ready.authorization_count(
            owner="local", analysis_result_id=result.analysis_id), 0)

    def test_invalid_policy_hash_fails_closed(self):
        job_id, _ = self.create_ready()
        config = self.jobs.get_job(job_id)["configuration"]
        config["workspace_authorization_policy_hash"] = "0" * 64
        self.jobs.update_fields(job_id, configuration_json=json.dumps(config))
        outcome = self.ready.resolve_ready_pipeline(job_id)
        self.assertEqual(outcome["reason_code"], "workspace_policy_hash_mismatch")
        self.assertEqual(self.jobs.get_job(job_id)["status"], JobStatus.SOURCE_ANALYSIS_READY)

    def test_invalid_mode_and_scope_fail_closed(self):
        for field, value, reason in (
            ("requested_mode", "turbo", "invalid_requested_mode"),
            ("requested_scope", "0", "invalid_requested_scope"),
        ):
            with self.subTest(field=field):
                job_id, _ = self.create_ready()
                config = self.jobs.get_job(job_id)["configuration"]
                config[field] = value
                self.jobs.update_fields(job_id, configuration_json=json.dumps(config))
                outcome = self.ready.resolve_ready_pipeline(job_id)
                self.assertEqual(outcome["reason_code"], reason)
                self.assertEqual(
                    self.jobs.get_job(job_id)["status"], JobStatus.SOURCE_ANALYSIS_READY)
                self.jobs.transition(job_id, JobStatus.CANCELLED)

    def test_two_simultaneous_requests_resolve_to_one_effective_authorization(self):
        job_id, result = self.create_ready()
        barrier = threading.Barrier(2)
        outcomes, errors = [], []

        def resolve():
            store = SourceReadinessStore(self.db)
            try:
                barrier.wait(timeout=5)
                outcomes.append(store.resolve_ready_pipeline(job_id))
            except BaseException as exc:  # recorded for the assertion below
                errors.append(exc)
            finally:
                store.close()

        threads = [threading.Thread(target=resolve) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(len({item["authorization_id"] for item in outcomes}), 1)
        self.assertEqual(self.ready.authorization_count(
            owner="local", analysis_result_id=result.analysis_id), 1)
        self.assertEqual(len(self.jobs.list_jobs(limit=None)), 1)

    def test_revocation_after_enqueue_does_not_cancel_started_operation(self):
        job_id, _ = self.create_ready()
        self.ready.resolve_ready_pipeline(job_id)
        self.ready.revoke_workspace_policy(
            owner="local", workspace_id=self.workspace, revoked_by="local")
        self.assertEqual(self.jobs.get_job(job_id)["status"], JobStatus.QUEUED)

    def test_operational_fixture_reaches_one_content_addressed_manifest(self):
        job_id, result = self.create_ready(download_only=True)
        outcome = self.ready.resolve_ready_pipeline(job_id)
        assets = [
            {"sequence": 1, "reference": "fixture://page-1"},
            {"sequence": 2, "reference": "fixture://page-2"},
        ]
        output = Path(self.tmp.name) / "fixture-output"
        first = download_fixture_assets(
            store=self.ready, owner="local", analysis_result_id=result.analysis_id,
            operation_id="operation-a", assets=assets, output_dir=output,
            read_asset=lambda ref: ("image/png", b"fixture-png:" + ref.encode()),
        )
        second = download_fixture_assets(
            store=self.ready, owner="local", analysis_result_id=result.analysis_id,
            operation_id="operation-a", assets=assets, output_dir=output,
            read_asset=lambda ref: self.fail("idempotent manifest re-read asset"),
        )
        self.assertEqual(outcome["status"], "queued")
        self.assertEqual(first["manifest_id"], second["manifest_id"])
        self.assertEqual(first["asset_count"], 2)
        self.assertEqual(len(list(output.glob("*.png"))), 2)


class CorruptPolicyIndexRecoveryTests(unittest.TestCase):
    """SOURCE-POLICY-500-001: a damaged scope index must not brick the policy toggle."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "jobs.sqlite3"
        self.workspace = default_workspace_id(self.db)
        store = SourceReadinessStore(self.db)
        try:
            self.seeded = store.activate_workspace_policy(
                owner="local", workspace_id=self.workspace, created_by="local",
                authorization_statement=STATEMENT)
            store.activate_workspace_policy(
                owner="other", workspace_id=self.workspace, created_by="other",
                authorization_statement=STATEMENT)
        finally:
            store.close()
        corrupt_policy_scope_index(self.db)
        self.opened: list = []

    def tearDown(self):
        for handle in self.opened:
            handle.close()
        self.tmp.cleanup()

    def open_store(self) -> SourceReadinessStore:
        store = SourceReadinessStore(self.db)
        self.opened.append(store)
        return store

    def open_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db)
        self.opened.append(conn)
        return conn

    def test_corruption_is_actually_reproduced_at_the_sqlite_layer(self):
        conn = self.open_connection()
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute(
                "SELECT payload_json FROM workspace_source_authorization_policies "
                "INDEXED BY " + WORKSPACE_POLICY_SCOPE_INDEX + " "
                "WHERE owner=? AND workspace_id=?",
                ("local", self.workspace)).fetchone()

    def test_reading_the_policy_recovers_the_persisted_authorization(self):
        policy = self.open_store().active_workspace_policy(
            owner="local", workspace_id=self.workspace)
        self.assertIsNotNone(policy)
        self.assertEqual(policy.policy_id, self.seeded.policy_id)

    def test_revocation_and_reactivation_persist_across_reloads(self):
        revoked = self.open_store().revoke_workspace_policy(
            owner="local", workspace_id=self.workspace, revoked_by="local")
        self.assertEqual(revoked.status, "revoked")
        self.assertIsNone(self.open_store().active_workspace_policy(
            owner="local", workspace_id=self.workspace))

        reactivated = self.open_store().activate_workspace_policy(
            owner="local", workspace_id=self.workspace, created_by="local",
            authorization_statement=STATEMENT)
        self.assertEqual(reactivated.status, "active")
        restored = self.open_store().active_workspace_policy(
            owner="local", workspace_id=self.workspace)
        self.assertEqual(restored.policy_id, reactivated.policy_id)

    def test_repair_rebuilds_the_index_without_dropping_history_or_other_scopes(self):
        store = self.open_store()
        self.assertEqual(len(store.workspace_policy_history(
            owner="local", workspace_id=self.workspace)), 1)
        self.assertIsNotNone(store.active_workspace_policy(
            owner="other", workspace_id=self.workspace))
        conn = self.open_connection()
        self.assertEqual(
            conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='index' AND name=?",
                (WORKSPACE_POLICY_SCOPE_INDEX,)).fetchone()[0], 1)
        self.assertNotIn("malformed", str(
            conn.execute("PRAGMA quick_check(20)").fetchall()))


class _StubAuth:
    def require_authenticated(self, request):
        return SimpleNamespace(user_id="user-a", owner_id="user-a", authenticated=True)

    def require_csrf(self, request, principal):
        return None


class SourcePolicyRouteTests(unittest.TestCase):
    """The settings toggle contract, exercised over the real persistence layer."""

    def setUp(self):
        import app_ui
        from ui_bridge import UiBridge

        self.app_ui = app_ui
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict("os.environ", {
            "APP_ENV": "test",
            "ALLOW_LOCAL_TEST_IDENTITIES": "1",
            "TRADUTOR_TEST_RUNTIME_ROOT": str(self.root),
        })
        self.env.start()
        self.bridge = UiBridge()
        self.workspace = default_workspace_id(self.bridge.store.db_path)
        self.patcher = patch.multiple(app_ui, AUTH=_StubAuth(), BRIDGE=self.bridge)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.bridge.close()
        self.env.stop()
        self.tmp.cleanup()

    def update(self, payload):
        return self.app_ui.api_source_policy_update(SimpleNamespace(), payload=payload)

    def stored_status(self) -> str:
        store = SourceReadinessStore(self.bridge.store.db_path)
        try:
            policy = store.active_workspace_policy(
                owner="local", workspace_id=self.workspace)
        finally:
            store.close()
        return policy.status if policy else "inactive"

    def test_enable_succeeds_persists_and_is_read_back_by_the_settings_route(self):
        result = self.update({"active": True})
        self.assertTrue(result["ok"])
        self.assertEqual(result["policy"]["status"], "active")
        self.assertTrue(result["policy"]["all_submitted_sources_authorized"])
        self.assertEqual(self.stored_status(), "active")
        served = self.app_ui.api_source_policy(SimpleNamespace())
        self.assertEqual(served["policy"]["policy_id"], result["policy"]["policy_id"])

    def test_enable_then_disable_persists_the_revocation(self):
        self.update({"active": True})
        revoked = self.update({"active": False})
        self.assertEqual(revoked["policy"]["status"], "revoked")
        self.assertEqual(self.stored_status(), "inactive")

    def test_repeated_enable_and_disable_are_idempotent(self):
        first = self.update({"active": True})
        second = self.update({"active": True})
        self.assertEqual(first["policy"]["policy_id"], second["policy"]["policy_id"])
        self.assertEqual(self.stored_status(), "active")
        self.update({"active": False})
        with self.assertRaises(self.app_ui.HTTPException) as ctx:
            self.update({"active": False})
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.stored_status(), "inactive")

    def test_a_corrupt_scope_index_no_longer_answers_with_a_server_error(self):
        self.update({"active": True})
        corrupt_policy_scope_index(self.bridge.store.db_path)
        self.assertEqual(self.update({"active": False})["policy"]["status"], "revoked")
        corrupt_policy_scope_index(self.bridge.store.db_path)
        self.assertEqual(self.update({"active": True})["policy"]["status"], "active")
        self.assertEqual(self.stored_status(), "active")

    def test_non_boolean_intent_is_refused_without_touching_the_policy(self):
        for payload in ({}, {"active": "true"}, {"active": "false"},
                        {"active": 1}, {"active": 0}, {"active": None}):
            with self.subTest(payload=payload):
                with self.assertRaises(self.app_ui.HTTPException) as ctx:
                    self.update(payload)
                self.assertEqual(ctx.exception.status_code, 422)
                self.assertEqual(self.stored_status(), "inactive")

    def test_a_failed_write_never_reports_an_active_policy(self):
        broken = patch.object(
            SourceReadinessStore, "activate_workspace_policy",
            side_effect=sqlite3.OperationalError("disk I/O error"))
        with broken, self.assertRaises(sqlite3.OperationalError):
            self.update({"active": True})
        self.assertEqual(self.stored_status(), "inactive")
        self.assertEqual(
            self.app_ui.api_source_policy(SimpleNamespace())["policy"]["status"],
            "inactive")


class SettingsToggleErrorHandlingContract(unittest.TestCase):
    """The settings pane must never paint an activation the backend refused."""

    UI = (Path(__file__).resolve().parent / "static" / "tradutor_ui.js").read_text(
        encoding="utf-8")

    def submit_handler(self) -> str:
        start = self.UI.index("#workspaceSourcePolicyConfirmForm")
        return self.UI[start:start + 1400]

    def test_toggle_failure_restores_the_previous_state_and_reports_it(self):
        handler = self.submit_handler()
        self.assertIn("} catch (error) {", handler)
        self.assertIn("showToast(", handler)
        self.assertIn(
            "renderWorkspaceSourcePolicy(appState.settings?.workspace_source_policy || {})",
            handler)
        failure = handler[handler.index("} catch (error) {"):]
        self.assertNotIn("appState.settings.workspace_source_policy =", failure)


if __name__ == "__main__":
    unittest.main()
