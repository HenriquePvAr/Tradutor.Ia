"""Hermetic contracts for workspace-scoped source authorization."""
from __future__ import annotations
import _test_bootstrap  # noqa: F401
import json
import tempfile
import threading
import unittest
from pathlib import Path
from job_store import JobStatus, JobStore
from source_readiness import (
    PIPELINE_OPERATIONS, SourceReadinessStore, default_workspace_id,
    download_fixture_assets,
)


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


if __name__ == "__main__":
    unittest.main()
