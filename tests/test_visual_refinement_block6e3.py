"""BLOCO 6E.3 contracts for delegated visual approval and reviewed masks."""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

import art_text_inpainting
import human_mask_decisions as hmd
import visual_review_decisions as vrd
from chapter_quality_revision import ChapterQualityRevision
from visual_refinement_provider_guard import (
    FORBIDDEN_REASON,
    NoProviderReviewer,
    ProviderCallsForbiddenInVisualRefinement,
)


class VisualReviewDecisionTests(unittest.TestCase):
    def test_delegated_visual_decision_is_owner_scoped_and_asset_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = vrd.VisualReviewDecisionStore(Path(tmp) / "jobs.sqlite3")
            try:
                first = store.upsert(
                    owner="owner", job_id="job", run_id="run", revision_id="rev",
                    page_id="p001", region_id="p001:REGION_001",
                    page_revision_id="draft", source_hash="source",
                    preview_asset_hash="asset", decision="approved",
                    authorization="delegated_by_user", reviewer="codex",
                    reason_codes=["visual_inspection_passed"],
                    visual_evidence={"visual_gate": {"status": "passed"}},
                )
                second = store.upsert(
                    owner="owner", job_id="job", run_id="run", revision_id="rev",
                    page_id="p001", region_id="p001:REGION_001",
                    page_revision_id="draft", source_hash="source",
                    preview_asset_hash="asset", decision="approved",
                    authorization="delegated_by_user", reviewer="codex",
                )
                self.assertEqual(first["visual_review_decision_id"], second["visual_review_decision_id"])
                self.assertIsNone(store.latest_for_preview(
                    owner="other", job_id="job", run_id="run",
                    page_revision_id="draft", region_id="p001:REGION_001",
                    preview_asset_hash="asset"))
            finally:
                store.close()

    def test_visual_decision_requires_delegated_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = vrd.VisualReviewDecisionStore(Path(tmp) / "jobs.sqlite3")
            try:
                with self.assertRaisesRegex(ValueError, "visual_review_authorization_required"):
                    store.upsert(
                        owner="owner", job_id="job", run_id="run", revision_id="rev",
                        page_id="p001", region_id="p001:REGION_001",
                        page_revision_id="draft", source_hash="source",
                        preview_asset_hash="asset", decision="approved",
                        authorization="implicit", reviewer="codex")
            finally:
                store.close()


class ReviewedMaskTests(unittest.TestCase):
    def test_protected_mask_is_allowed_when_it_does_not_overlap_final_text(self):
        result = hmd.validate_mask_payload({
            "base_segmentation_hash": "seg",
            "source_hash": "source",
            "include_mask": [[10, 10, 10, 10]],
            "protected_mask": [[40, 40, 10, 10]],
            "final_mask_metrics": {
                "final_mask_area": 100,
                "connected_components": 1,
                "protected_overlap": 0,
                "mask_hash": "hash",
            },
        }, region_box=[0, 0, 100, 100],
            base_segmentation_hash="seg", source_hash="source")
        self.assertEqual(result["status"], "valid_for_local_preview_candidate")
        self.assertEqual(result["protected_area"], 100)

    def test_protected_overlap_still_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "mask_protected_overlap"):
            hmd.validate_mask_payload({
                "base_segmentation_hash": "seg",
                "source_hash": "source",
                "include_mask": [[10, 10, 10, 10]],
                "protected_mask": [[10, 10, 10, 10]],
            }, region_box=[0, 0, 100, 100],
                base_segmentation_hash="seg", source_hash="source")

    def test_confirmed_mask_drives_local_inpainting_without_provider(self):
        image = np.full((96, 220, 3), 242, dtype=np.uint8)
        cv2.putText(image, "OLD", (28, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.8, (8, 8, 8), 5, cv2.LINE_AA)
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        text_mask = (gray < 80).astype(np.uint8) * 255
        text_mask = cv2.dilate(text_mask, np.ones((3, 3), np.uint8), iterations=1)
        engine = object.__new__(ChapterQualityRevision)
        cleaned, metrics, reason = engine._clean_previous_translation(
            image.copy(), [(0, 0, image.shape[1], image.shape[0])],
            mask_overrides_by_box={
                (0, 0, image.shape[1], image.shape[0]): {
                    "human_mask_decision_id": "mask",
                    "combined_inpainting_mask": text_mask,
                    "validation_halo": cv2.dilate(text_mask, np.ones((5, 5), np.uint8), iterations=1),
                    "protected_edge_mask": np.zeros(text_mask.shape, dtype=np.uint8),
                    "mask_hash": art_text_inpainting.mask_hash(text_mask),
                }
            })
        self.assertEqual(reason, "")
        self.assertIsNotNone(cleaned)
        self.assertEqual(metrics[0]["mask_source"], "confirmed_human_mask")
        self.assertEqual(metrics[0]["selected_inpainting"]["status"], "passed")


class ProviderGuardTests(unittest.TestCase):
    def test_visual_refinement_provider_guard_fails_before_requests(self):
        reviewer = NoProviderReviewer()
        with self.assertRaises(ProviderCallsForbiddenInVisualRefinement) as ctx:
            reviewer.translate_many(["hello"])
        self.assertEqual(str(ctx.exception), FORBIDDEN_REASON)
        self.assertEqual(reviewer.requests, 0)


if __name__ == "__main__":
    unittest.main()
