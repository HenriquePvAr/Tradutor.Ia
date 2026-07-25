"""Hermetic tests for the offline linguistic audit (BLOCO 2). No network, no PDF."""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import json
import tempfile
import unittest
from pathlib import Path

import linguistic_audit as audit
import region_taxonomy as tax


class LinguisticAuditContracts(unittest.TestCase):
    def _fixture(self, root: Path) -> Path:
        output = root / "output"
        (output / "quality_revision" / "rev1").mkdir(parents=True)
        quality = {"pages": [
            {"index": 3, "suspicious_groups": [
                {"id": "BALAO_1", "region_id": "REGION_001", "classification": "decorative",
                 "text": "REAL COFFEE.", "translation": "", "preserved_original": True},
            ]},
            {"index": 8, "translation_terminal_items": [
                {"id": "BALAO_1", "region_id": "REGION_001", "classification": "narration",
                 "text": "SINCE I HAD SPENT IT ALL.", "translation": "Já que gastei tudo."},
                {"id": "SFX_1", "region_id": "REGION_002", "classification": "sfx",
                 "text": "BAM", "translation": "BAM", "preserved_original": True},
                {"id": "WM_1", "region_id": "REGION_003", "classification": "speech",
                 "text": "VORTEXSCANS.COM", "translation": ""},
            ]},
        ]}
        (output / "quality_report.json").write_text(json.dumps(quality), encoding="utf-8")
        manifest = {"revision_id": "rev1", "parent_job_id": "job", "parent_run_id": "run",
                    "reviewed_pdf_path": "output/x/chapter_reviewed_v8.pdf",
                    "reviewed_pdf_sha256": "DEAD", "updated_at": "2026-01-01T00:00:00+00:00"}
        (output / "quality_revision" / "rev1" / "revision_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (output / "quality_revision" / "rev1" / "contextual_translation_review.json").write_text(json.dumps(
            {"reviews": [{"region_id": "p008:REGION_001", "action": "rewrite", "risk": "low", "reason_code": "T01"}]}),
            encoding="utf-8")
        (output / "quality_revision" / "rev1" / "incremental_render_audit.json").write_text(json.dumps(
            {"region_visual_states": {"p008:REGION_001": {"state": "applied"}}}), encoding="utf-8")
        return output

    def test_audit_is_deterministic_and_reclassifies_semantic_text(self):
        with tempfile.TemporaryDirectory() as folder:
            output = self._fixture(Path(folder))
            report = audit.audit_chapter(str(output), "job", "run", pdf_name="chapter_reviewed_v8.pdf")
            self.assertEqual(report["revision_id"], "rev1")
            by_id = {r["region_id"]: r for r in report["records"]}
            # Semantic decorative that was preserved is now translatable + flagged.
            coffee = by_id["p003:REGION_001"]
            self.assertEqual(coffee["classification_normalized"], tax.DECORATIVE_SEMANTIC_TRANSLATE)
            self.assertEqual(coffee["suggested_action"], "translate")
            self.assertTrue(coffee["needs_human_review"])
            self.assertTrue(coffee["report_only"])          # not in the revision
            # Real SFX stays preserved; a watermark URL is preserved even if
            # the legacy label was "speech" (which would have translated it).
            self.assertEqual(by_id["p008:REGION_002"]["classification_normalized"], tax.SFX_PRESERVE)
            self.assertEqual(by_id["p008:REGION_003"]["classification_normalized"], tax.URL_PRESERVE)
            # Determinism: same inputs → identical report.
            again = audit.audit_chapter(str(output), "job", "run", pdf_name="chapter_reviewed_v8.pdf")
            self.assertEqual(json.dumps(report, sort_keys=True), json.dumps(again, sort_keys=True))

    def test_report_only_reclassification_is_a_derived_view_only(self):
        with tempfile.TemporaryDirectory() as folder:
            output = self._fixture(Path(folder))
            before = (output / "quality_report.json").read_bytes()
            manifest_before = (output / "quality_revision" / "rev1" / "revision_manifest.json").read_bytes()
            report = audit.audit_chapter(str(output), "job", "run", pdf_name="chapter_reviewed_v8.pdf")
            paths = audit.write_report(report, str(Path(folder) / "audit-out"))
            # The audit never touches the chapter's own data.
            self.assertEqual((output / "quality_report.json").read_bytes(), before)
            self.assertEqual((output / "quality_revision" / "rev1" / "revision_manifest.json").read_bytes(), manifest_before)
            self.assertTrue(Path(paths["json"]).is_file())
            self.assertTrue(Path(paths["md"]).is_file())
            self.assertGreaterEqual(report["report_only_now_translatable"], 1)

    def test_missing_revision_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as folder:
            output = self._fixture(Path(folder))
            with self.assertRaisesRegex(ValueError, "reviewed_revision_not_found"):
                audit.audit_chapter(str(output), "job", "run", pdf_name="nonexistent_v99.pdf")


if __name__ == "__main__":
    unittest.main()
