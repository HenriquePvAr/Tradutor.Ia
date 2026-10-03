"""Bridge-level + anti-hardcoding tests for the audit review flow (BLOCO 3).

Proves the exact same code drives two structurally different synthetic chapters
with no shared pages, ids, texts or versions — nothing chapter-specific lives in
production logic.
"""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from audit_decisions import AuditDecisionStore
from job_store import JobStore
from ui_bridge import UiBridge


class _AuditBridge(UiBridge):
    def __init__(self, db: Path):
        self.history_store = mock.Mock()
        self.history = []
        self.profile = {}
        self.store = JobStore(db)
        self.audit_decisions = AuditDecisionStore(db)
        self.history_revision = 1

    def close(self):
        self.audit_decisions.close()
        self.store.close()


def _make_chapter(tmp: Path, *, pages, revision_id, pdf_name):
    """A self-contained synthetic chapter with a reviewed revision."""
    output = tmp / f"chapter_{uuid.uuid4().hex[:8]}"
    rev_dir = output / "quality_revision" / revision_id
    rev_dir.mkdir(parents=True)
    (output / "quality_report.json").write_text(json.dumps({"pages": pages}), encoding="utf-8")
    manifest = rev_dir / "revision_manifest.json"
    manifest.write_text(json.dumps({
        "revision_id": revision_id, "parent_job_id": "x", "parent_run_id": "y",
        "reviewed_pdf_path": f"output/whatever/{pdf_name}", "reviewed_pdf_sha256": "AAAA",
        "updated_at": "2026-01-01T00:00:00+00:00"}), encoding="utf-8")
    (rev_dir / "contextual_translation_review.json").write_text(json.dumps({"reviews": []}), encoding="utf-8")
    (rev_dir / "incremental_render_audit.json").write_text(json.dumps({"region_visual_states": {}}), encoding="utf-8")
    (output / "quality_revision" / "latest_revision.json").write_text(json.dumps({
        "revision_id": revision_id, "manifest_path": str(manifest)}), encoding="utf-8")
    return output


class AuditReviewBridgeContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bridge = _AuditBridge(self.tmp / "jobs.sqlite3")

    def tearDown(self):
        self.bridge.close()

    def _register_job(self, output: Path) -> tuple[str, str]:
        job_id = self.bridge.store.create_job(
            source_url="https://example.test/c", output_dir=str(output),
            command=["python", "run.py"], configuration={"job_type": "translation"})
        return job_id, self.bridge.store.get_job(job_id)["run_id"]

    # A page/item builder that shares nothing across chapters.
    def _chapterA(self):
        pages = [
            {"index": 3, "suspicious_groups": [
                {"id": "BALAO_1", "region_id": "REGION_001", "classification": "decorative",
                 "text": "REAL COFFEE.", "preserved_original": True}]},
            {"index": 8, "translation_terminal_items": [
                {"id": "SFX_1", "region_id": "REGION_002", "classification": "sfx",
                 "text": "BAM", "translation": "BAM", "preserved_original": True}]},
        ]
        return _make_chapter(self.tmp, pages=pages, revision_id="revA", pdf_name="alpha_reviewed_v8.pdf")

    def _chapterB(self):
        pages = [
            {"index": 42, "visual_validation_failures": [
                {"id": "B_1", "region_id": "ZONE_777", "classification": "unknown",
                 "text": "THE HIDDEN GARDEN", "quality_reasons": ["x"]}]},
            {"index": 91, "suspicious_groups": [
                {"id": "B_2", "region_id": "ZONE_778", "classification": "decorative",
                 "text": "WHOOSH", "preserved_original": True}]},
        ]
        return _make_chapter(self.tmp, pages=pages, revision_id="revB-9z", pdf_name="beta_reviewed_v3.pdf")

    def test_same_code_audits_two_unrelated_chapters(self):
        for build in (self._chapterA, self._chapterB):
            output = build()
            job_id, run_id = self._register_job(output)
            review = self.bridge.linguistic_audit_review(job_id, run_id, user_id="user-1")
            self.assertGreaterEqual(review["summary"]["total_regions_audited"], 2)
            self.assertTrue(review["audit_artifact_id"])
            # dynamic — numbers come from the report, not a constant
            self.assertEqual(sum(review["summary"]["by_normalized_category"].values()),
                             review["summary"]["total_regions_audited"])

    def test_decision_lifecycle_and_lineage(self):
        output = self._chapterA()
        job_id, run_id = self._register_job(output)
        review = self.bridge.linguistic_audit_review(job_id, run_id, user_id="user-1")
        region = review["records"][0]["region_id"]
        # create
        made = self.bridge.record_audit_decision(job_id, run_id, region_id=region,
                                                 decision="translate", user_id="user-1", reason="semantic")
        self.assertEqual(made["decision"], "translate")
        # visible on reload (F5-equivalent)
        again = self.bridge.linguistic_audit_review(job_id, run_id, user_id="user-1")
        overlaid = next(r for r in again["records"] if r["region_id"] == region)
        self.assertEqual(overlaid["human_decision"]["decision"], "translate")
        # update (idempotent)
        updated = self.bridge.record_audit_decision(job_id, run_id, region_id=region,
                                                   decision="preserve", user_id="user-1")
        self.assertEqual(updated["audit_decision_id"], made["audit_decision_id"])
        # delete
        removed = self.bridge.delete_audit_decision(job_id, run_id, decision_id=made["audit_decision_id"], user_id="user-1")
        self.assertTrue(removed["removed"])
        final = self.bridge.linguistic_audit_review(job_id, run_id, user_id="user-1")
        self.assertIsNone(next(r for r in final["records"] if r["region_id"] == region)["human_decision"])

    def test_region_not_in_audit_is_rejected(self):
        output = self._chapterA()
        job_id, run_id = self._register_job(output)
        with self.assertRaisesRegex(ValueError, "region_not_in_audit"):
            self.bridge.record_audit_decision(job_id, run_id, region_id="p999:NOPE",
                                              decision="translate", user_id="user-1")

    def test_unauthenticated_decision_is_rejected(self):
        output = self._chapterA()
        job_id, run_id = self._register_job(output)
        review = self.bridge.linguistic_audit_review(job_id, run_id, user_id="user-1")
        with self.assertRaisesRegex(ValueError, "authentication_required"):
            self.bridge.record_audit_decision(job_id, run_id, region_id=review["records"][0]["region_id"],
                                              decision="translate", user_id="")

    def test_wrong_run_id_is_rejected(self):
        output = self._chapterA()
        job_id, _ = self._register_job(output)
        with self.assertRaisesRegex(ValueError, "run_id_mismatch"):
            self.bridge.linguistic_audit_review(job_id, "not-the-run", user_id="user-1")

    def test_decision_from_another_user_cannot_be_deleted(self):
        output = self._chapterA()
        job_id, run_id = self._register_job(output)
        review = self.bridge.linguistic_audit_review(job_id, run_id, user_id="user-1")
        made = self.bridge.record_audit_decision(job_id, run_id, region_id=review["records"][0]["region_id"],
                                                decision="translate", user_id="user-1")
        with self.assertRaises(ValueError):
            self.bridge.delete_audit_decision(job_id, run_id, decision_id=made["audit_decision_id"], user_id="intruder")


if __name__ == "__main__":
    unittest.main()
