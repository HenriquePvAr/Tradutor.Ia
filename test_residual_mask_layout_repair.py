import _test_bootstrap  # noqa: F401
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

import residual_mask_revisions as rmr


class ResidualComponentDetectorTests(unittest.TestCase):
    def test_glyph_fragment_outside_mask_becomes_safe_delta(self):
        previous = np.zeros((40, 80), dtype=np.uint8)
        previous[10:30, 10:30] = 255
        core = previous.copy()
        core[12:28, 32:38] = 255
        report = rmr.detect_residual_components(
            previous_mask=previous,
            text_core_mask=core,
            outline_mask=np.zeros_like(core),
            antialias_mask=np.zeros_like(core),
            protected_edge_mask=np.zeros_like(core),
        )
        self.assertTrue(report["residual_detected"])
        self.assertEqual(report["safe_component_count"], 1)
        self.assertIn("glyph_fragment_outside_mask", report["reason_codes"])
        revised = rmr.build_revised_mask(previous, report)
        self.assertEqual(revised["status"], "valid_for_local_preview_candidate")
        self.assertGreater(revised["delta_area"], 0)

    def test_protected_line_intersection_blocks_automatic_delta(self):
        previous = np.zeros((40, 80), dtype=np.uint8)
        core = np.zeros_like(previous)
        core[12:28, 32:38] = 255
        protected = np.zeros_like(previous)
        protected[12:28, 32:38] = 255
        report = rmr.detect_residual_components(
            previous_mask=previous,
            text_core_mask=core,
            outline_mask=np.zeros_like(core),
            antialias_mask=np.zeros_like(core),
            protected_edge_mask=protected,
        )
        self.assertEqual(report["safe_component_count"], 0)
        self.assertIn("protected_line_intersection", report["reason_codes"])
        revised = rmr.build_revised_mask(previous, report, protected_edge_mask=protected)
        self.assertEqual(revised["status"], "blocked_by_mask_revision_gate")
        self.assertIn("unresolved_residual_components", revised["reason_codes"])

    def test_excessive_revision_is_blocked(self):
        previous = np.zeros((50, 50), dtype=np.uint8)
        previous[:45, :45] = 255
        report = {"suggested_mask_delta": np.zeros_like(previous), "unresolved_residual_components": 0}
        revised = rmr.build_revised_mask(previous, report)
        self.assertEqual(revised["status"], "blocked_by_mask_revision_gate")
        self.assertIn("mask_area_excessive", revised["reason_codes"])


class MaskRevisionStoreTests(unittest.TestCase):
    def test_revision_is_owner_scoped_and_requires_delegated_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = rmr.MaskRevisionStore(Path(tmp) / "jobs.sqlite3")
            with self.assertRaises(ValueError):
                store.upsert(
                    supersedes_mask_decision_id="m1",
                    owner="owner",
                    job_id="job",
                    run_id="run",
                    revision_id="rev",
                    page_id="p001",
                    region_id="p001:R",
                    source_hash="src",
                    base_segmentation_hash="seg",
                    previous_mask_hash="old",
                    final_mask_hash="new",
                    authorization="implicit",
                )
            row = store.upsert(
                supersedes_mask_decision_id="m1",
                owner="owner",
                job_id="job",
                run_id="run",
                revision_id="rev",
                page_id="p001",
                region_id="p001:R",
                source_hash="src",
                base_segmentation_hash="seg",
                previous_mask_hash="old",
                final_mask_hash="new",
                final_mask_asset="mask_revisions/x/final_mask.png",
                residual_evidence={"residual_component_count": 1},
                validation={"status": "valid_for_local_preview_candidate"},
                status="confirmed",
            )
            self.assertEqual(row["status"], "confirmed")
            self.assertEqual(row["residual_evidence"]["residual_component_count"], 1)
            latest = store.latest_for_region(
                owner="owner",
                job_id="job",
                run_id="run",
                revision_id="rev",
                region_id="p001:R",
                supersedes_mask_decision_id="m1",
            )
            self.assertEqual(latest["mask_revision_id"], row["mask_revision_id"])
            store.close()


class LayoutCandidateTests(unittest.TestCase):
    def test_multiline_candidate_fits_without_changing_text(self):
        text = "Já que gastei todo o dinheiro que me restava nisso..."
        candidates = rmr.layout_candidates_for_text(
            text,
            available_width=420,
            available_height=180,
            font_identity="test-font",
            font_file_hash="hash",
            max_size=48,
        )
        self.assertTrue(candidates)
        selected = candidates[0]
        self.assertFalse(selected["overflow"])
        self.assertFalse(selected["clipping"])
        self.assertEqual(" ".join(selected["line_breaks"]), text)

    def test_empty_text_is_rejected(self):
        with self.assertRaises(ValueError):
            rmr.layout_candidates_for_text(
                "",
                available_width=100,
                available_height=40,
                font_identity="test-font",
            )


if __name__ == "__main__":
    unittest.main()
