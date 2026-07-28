"""Hermetic contracts for source readiness and explicitly authorized fixture downloads."""

import _test_bootstrap  # noqa: F401

import tempfile
import unittest
from pathlib import Path

from job_store import JobStatus, JobStore
from source_analysis_phase import SOURCE_READY, apply_source_analysis, should_spawn_runner
from source_readiness import (
    DOWNLOAD_AUTHORIZATION_REQUIRED,
    SourceReadinessStore,
    download_fixture_assets,
)


class _Candidate:
    def __init__(self, value):
        self.id = value


class _Analysis:
    outcome = "supported_specific_adapter"
    accepted = [_Candidate("opaque-1"), _Candidate("opaque-2")]

    def public(self):
        return {
            "adapter": "fixture_adapter",
            "adapter_version": "1",
            "title": "Synthetic chapter",
            "candidate_count": 2,
            "accepted_count": 2,
            "discarded_count": 0,
            "confidence": 1.0,
            "warnings": [],
            "accepted": [{"id": value.id} for value in self.accepted],
        }


def analysis_payload(owner="user-a", operation="operation-a", status="source_analysis_ready"):
    return {
        "owner": owner,
        "operation_id": operation,
        "previous_operation_id": "",
        "attempt": 1,
        "normalized_url_hash": "a" * 64,
        "adapter": "fixture_adapter",
        "source_kind": "local_test_fixture",
        "preflight_result_id": "preflight-a",
        "preflight_status": "preflight_ready",
        "preflight_reason_code": "",
        "browser_inspection_required": False,
        "browser_inspection_performed": False,
        "browser_runtime_id": "",
        "browser_engine": "",
        "browser_version": "",
        "document_ready_state": "complete",
        "public_title_present": True,
        "public_structure_indicators_present": True,
        "estimated_asset_count": 2,
        "estimate_kind": "fixture_exact",
        "authentication_required": False,
        "captcha_detected": False,
        "access_restricted": False,
        "status": status,
        "reason_code": "source_structure_compatible",
        "policy_hash": "b" * 64,
        "created_at": 1.0,
        "completed_at": 2.0,
    }


class SourceAnalysisResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = SourceReadinessStore(self.tmp / "state.sqlite3")

    def tearDown(self):
        self.store.close()

    def test_ready_blocked_and_failed_are_versioned(self):
        for index, status in enumerate((
            "source_analysis_ready", "source_analysis_blocked", "source_analysis_failed")):
            result = self.store.persist_analysis(
                analysis_payload(operation=f"operation-{index}", status=status))
            self.assertEqual(result.schema_version, 1)
            self.assertEqual(result.status, status)

    def test_identity_is_content_addressed_and_idempotent(self):
        first = self.store.persist_analysis(analysis_payload())
        second = self.store.persist_analysis(analysis_payload())
        self.assertEqual(first.analysis_id, second.analysis_id)
        self.assertEqual(first.result_hash, second.result_hash)
        count = self.store._conn.execute(
            "SELECT COUNT(*) FROM source_analysis_results").fetchone()[0]
        self.assertEqual(count, 1)

    def test_owner_scope_and_lineage(self):
        first = self.store.persist_analysis(analysis_payload())
        second_payload = analysis_payload(owner="user-b", operation="operation-b")
        second_payload["previous_operation_id"] = first.operation_id
        second = self.store.persist_analysis(second_payload)
        self.assertIsNone(self.store.get_analysis("user-a", second.analysis_id))
        self.assertEqual(second.previous_operation_id, first.operation_id)

    def test_payload_contains_no_html_or_images(self):
        result = self.store.persist_analysis(analysis_payload()).public()
        self.assertNotIn("html", result)
        self.assertNotIn("images", result)
        self.assertNotIn("cookies", result)

    def test_latest_restore_does_not_create_another_result(self):
        original = self.store.persist_analysis(analysis_payload())
        restored = self.store.latest_analysis("user-a")
        self.assertEqual(restored.analysis_id, original.analysis_id)
        self.assertEqual(
            self.store._conn.execute(
                "SELECT COUNT(*) FROM source_analysis_results").fetchone()[0], 1)


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = SourceReadinessStore(self.tmp / "state.sqlite3")
        self.result = self.store.persist_analysis(analysis_payload())

    def tearDown(self):
        self.store.close()

    def test_absent_authorization_fails_closed(self):
        with self.assertRaisesRegex(PermissionError, DOWNLOAD_AUTHORIZATION_REQUIRED):
            self.store.require_operation(
                owner="user-a", analysis_result_id=self.result.analysis_id,
                operation="download_assets")

    def test_rights_basis_and_scope_are_required(self):
        with self.assertRaisesRegex(ValueError, "download_rights_basis_required"):
            self.store.authorize(
                owner="user-a", analysis_result_id=self.result.analysis_id,
                rights_basis="", allowed_operations=["download_assets"], reviewer="user-a")
        with self.assertRaisesRegex(ValueError, "download_scope_denied"):
            self.store.authorize(
                owner="user-a", analysis_result_id=self.result.analysis_id,
                rights_basis="owned_content", allowed_operations=[], reviewer="user-a")

    def test_owner_mismatch_is_concealed(self):
        with self.assertRaisesRegex(ValueError, "analysis_result_not_found"):
            self.store.authorize(
                owner="user-b", analysis_result_id=self.result.analysis_id,
                rights_basis="owned_content", allowed_operations=["download_assets"],
                reviewer="user-b")

    def test_exact_scope_denies_ocr_and_translation(self):
        decision = self.store.authorize(
            owner="user-a", analysis_result_id=self.result.analysis_id,
            rights_basis="owned_content",
            allowed_operations=["analyze_metadata", "download_assets"],
            reviewer="user-a")
        self.assertIn("download_assets", decision.allowed_operations)
        self.assertIn("run_ocr", decision.denied_operations)
        self.assertIn("translate", decision.denied_operations)
        with self.assertRaisesRegex(PermissionError, DOWNLOAD_AUTHORIZATION_REQUIRED):
            self.store.require_operation(
                owner="user-a", analysis_result_id=self.result.analysis_id,
                operation="run_ocr")

    def test_authorization_is_append_only_and_idempotent(self):
        arguments = dict(
            owner="user-a", analysis_result_id=self.result.analysis_id,
            rights_basis="owned_content", allowed_operations=["download_assets"],
            reviewer="user-a")
        first = self.store.authorize(**arguments)
        second = self.store.authorize(**arguments)
        self.assertEqual(first.authorization_id, second.authorization_id)
        self.assertEqual(
            self.store._conn.execute(
                "SELECT COUNT(*) FROM download_authorization_decisions").fetchone()[0], 1)


class FixtureDownloadTests(unittest.TestCase):
    PNG_A = b"\x89PNG\r\n\x1a\nsynthetic-a"
    PNG_B = b"\x89PNG\r\n\x1a\nsynthetic-b"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = SourceReadinessStore(self.tmp / "state.sqlite3")
        self.result = self.store.persist_analysis(analysis_payload())
        self.authorization = self.store.authorize(
            owner="user-a", analysis_result_id=self.result.analysis_id,
            rights_basis="local_test_fixture", allowed_operations=["download_assets"],
            reviewer="fixture-runner", authorization_source="hermetic_test")

    def tearDown(self):
        self.store.close()

    def read(self, reference):
        return "image/png", {"fixture://a": self.PNG_A, "fixture://b": self.PNG_B}[reference]

    def download(self, assets=None):
        return download_fixture_assets(
            store=self.store, owner="user-a",
            analysis_result_id=self.result.analysis_id,
            operation_id="fixture-download-a",
            assets=assets or [
                {"sequence": 2, "reference": "fixture://b"},
                {"sequence": 1, "reference": "fixture://a"},
                {"sequence": 3, "reference": "fixture://a"},
            ],
            output_dir=self.tmp / "assets", read_asset=self.read)

    def test_deterministic_content_addressed_manifest_and_deduplication(self):
        manifest = self.download()
        self.assertEqual(manifest["asset_count"], 2)
        self.assertEqual([item["sequence"] for item in manifest["assets"]], [1, 2])
        self.assertTrue(all(item["local_content_address"].startswith("sha256/")
                            for item in manifest["assets"]))
        self.assertEqual(len(list((self.tmp / "assets").glob("*.png"))), 2)
        self.assertEqual(list((self.tmp / "assets").glob("*.partial")), [])

    def test_manifest_restore_is_idempotent_without_new_reads(self):
        first = self.download()
        reads = 0

        def counted(reference):
            nonlocal reads
            reads += 1
            return self.read(reference)

        second = download_fixture_assets(
            store=self.store, owner="user-a",
            analysis_result_id=self.result.analysis_id,
            operation_id="fixture-download-a",
            assets=[
                {"sequence": 1, "reference": "fixture://a"},
                {"sequence": 2, "reference": "fixture://b"},
            ],
            output_dir=self.tmp / "assets", read_asset=counted)
        self.assertEqual(first["manifest_id"], second["manifest_id"])
        self.assertEqual(reads, 0)
        self.assertEqual(
            self.store._conn.execute(
                "SELECT COUNT(*) FROM downloaded_asset_manifests").fetchone()[0], 1)

    def test_mime_size_count_and_total_limits_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "content_type_invalid"):
            download_fixture_assets(
                store=self.store, owner="user-a",
                analysis_result_id=self.result.analysis_id, operation_id="bad-mime",
                assets=[{"sequence": 1, "reference": "fixture://text"}],
                output_dir=self.tmp / "bad", read_asset=lambda _: ("text/html", b"x"))
        with self.assertRaisesRegex(ValueError, "asset_too_large"):
            download_fixture_assets(
                store=self.store, owner="user-a",
                analysis_result_id=self.result.analysis_id, operation_id="too-large",
                assets=[{"sequence": 1, "reference": "fixture://a"}],
                output_dir=self.tmp / "large", read_asset=self.read, max_asset_bytes=2)
        with self.assertRaisesRegex(ValueError, "count_limit"):
            download_fixture_assets(
                store=self.store, owner="user-a",
                analysis_result_id=self.result.analysis_id, operation_id="too-many",
                assets=[{"sequence": 1, "reference": "fixture://a"}],
                output_dir=self.tmp / "many", read_asset=self.read, max_assets=0)
        with self.assertRaisesRegex(ValueError, "total_limit"):
            download_fixture_assets(
                store=self.store, owner="user-a",
                analysis_result_id=self.result.analysis_id, operation_id="total",
                assets=[{"sequence": 1, "reference": "fixture://a"}],
                output_dir=self.tmp / "total", read_asset=self.read, max_total_bytes=2)

    def test_non_fixture_rights_cannot_use_fixture_seam(self):
        other = self.store.persist_analysis(
            analysis_payload(operation="operation-owned"))
        self.store.authorize(
            owner="user-a", analysis_result_id=other.analysis_id,
            rights_basis="owned_content", allowed_operations=["download_assets"],
            reviewer="user-a")
        with self.assertRaisesRegex(PermissionError, "fixture_authorization_required"):
            download_fixture_assets(
                store=self.store, owner="user-a",
                analysis_result_id=other.analysis_id, operation_id="fixture-denied",
                assets=[], output_dir=self.tmp / "denied", read_asset=self.read)


class PhaseGateTests(unittest.TestCase):
    def test_successful_analysis_persists_ready_and_never_spawns(self):
        root = Path(tempfile.mkdtemp())
        jobs = JobStore(root / "jobs.sqlite3")
        try:
            job_id = jobs.create_job(
                source_url="https://fixture.invalid/chapter?id=1",
                output_dir=str(root / "out"),
                configuration={
                    "job_type": "translation",
                    "community_owner_id": "user-a",
                    "mode": "fast",
                    "full": True,
                },
                command=["python", "runner.py"])
            jobs.update_fields(job_id, source_type="url")
            jobs.transition(job_id, JobStatus.CLAIMING, worker_id="worker-a")
            result = apply_source_analysis(jobs, jobs.get_job(job_id), _Analysis())
            row = jobs.get_job(job_id)
            self.assertEqual(result.outcome, SOURCE_READY)
            self.assertEqual(row["status"], JobStatus.SOURCE_ANALYSIS_READY)
            self.assertEqual(row["stage"], "source_analysis_ready")
            self.assertFalse(result.should_spawn_runner)
            self.assertFalse(should_spawn_runner(row, result))
            self.assertTrue(row["configuration"]["source_analysis_result_id"].startswith("sa_"))
        finally:
            jobs.close()


class UiContractTests(unittest.TestCase):
    def test_ready_ui_uses_workspace_policy_without_per_source_modal(self):
        root = Path(__file__).resolve().parent
        html = (root / "ui" / "ui_shell.html").read_text(encoding="utf-8")
        script = (root / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        self.assertIn("Fonte analisada", html)
        self.assertIn("Política das fontes", html)
        self.assertIn("workspaceSourcePolicyDialog", html)
        self.assertNotIn("downloadAuthorizationDialog", html)
        self.assertNotIn("window.alert(", script)
        self.assertIn("source_analysis_ready", script)
        self.assertIn("workspace_source_policy", script)

    def test_polling_preserves_ready_state_and_isolates_unrelated_terminal_results(self):
        root = Path(__file__).resolve().parent
        script = (root / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        self.assertIn("applyStandaloneSourceReady(data)", script)
        self.assertIn("&& !runtime.source_ready", script)
        self.assertIn("if (!runtime.source_ready && !draftOnly", script)
        self.assertIn("if (!runtime.source_ready && resultRecord", script)
        self.assertNotIn("'awaiting_source_review', 'source_analysis_ready'", script)
        self.assertIn("else if (runtime.source_ready)", script)

    def test_opening_new_translation_hides_unrelated_terminal_artifacts(self):
        root = Path(__file__).resolve().parent
        script = (root / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        self.assertIn("appState.newTranslationDraft = true;", script)
        self.assertIn("clearNewTranslationDraftPanels();", script)
        reset = script.split("function resetActivePipelineIdentity", 1)[1].split(
            "function persistPipelineIdentity", 1)[0]
        self.assertIn("clearNewTranslationDraftPanels();", reset)
        self.assertNotIn("resetRunPreview();", reset)
        clear = script.split("function clearNewTranslationDraftPanels", 1)[1].split(
            "function resetActivePipelineIdentity", 1)[0]
        self.assertIn("appState.qualityReview = null;", clear)
        self.assertIn("reviewedPdf.innerHTML = '';", clear)

    def test_quality_review_must_belong_to_the_current_operation(self):
        root = Path(__file__).resolve().parent
        script = (root / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        render = script.split("const reviewDismissed =", 1)[1].split(
            "const resultRecord =", 1)[0]
        self.assertIn("qualityReviewJobId", render)
        self.assertIn("reviewOwnerJobId", render)
        self.assertIn("qualityReviewJobId === reviewOwnerJobId", render)


if __name__ == "__main__":
    unittest.main()
