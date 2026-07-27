import _test_bootstrap  # noqa: F401

import tempfile
import unittest
from pathlib import Path

import numpy as np

import art_text_inpainting as ati
from chapter_quality_revision import ChapterQualityRevision
from residual_component_reviews import ResidualComponentReviewStore
from residual_mask_revisions import MaskRevisionStore


class HumanOverrideRenderEligibilityTests(unittest.TestCase):
    def test_human_override_makes_preserved_region_render_eligible(self):
        page = {
            "index": 1,
            "debug_data": {
                "items": [{
                    "region_id": "REGION_001",
                    "bounding_box": [1, 2, 30, 12],
                    "translation": "",
                    "preserved_original": True,
                    "redrawn": False,
                    "sent_to_translation": False,
                }]
            },
        }
        engine = object.__new__(ChapterQualityRevision)
        changed = engine._apply_safe_changes_to_pages([page], [{
            "region_id": "p001:REGION_001",
            "page": 1,
            "revised_translation": "Texto humano",
            "reason_code": "human_override",
            "human_override": True,
        }])
        item = page["debug_data"]["items"][0]
        self.assertEqual(len(changed[1]), 1)
        self.assertTrue(item["sent_to_translation"])
        self.assertFalse(item["preserved_original"])


class PostReconstructionResidualTests(unittest.TestCase):
    def test_dark_source_pixels_reused_by_candidate_are_rejected(self):
        source = np.full((12, 12, 3), 230, np.uint8)
        source[4:7, 5:8] = 5
        candidate = np.full_like(source, 230)
        candidate[5, 6] = source[5, 6]
        source_mask = np.zeros(source.shape[:2], np.uint8)
        source_mask[4:7, 5:8] = 255
        result = ati.detect_post_reconstruction_residuals(
            source, candidate, source_mask=source_mask)
        self.assertTrue(result["post_reconstruction_residual_detected"])
        self.assertEqual(result["post_reconstruction_residual_pixels"], 1)

    def test_light_source_pixels_reused_by_candidate_are_rejected(self):
        source = np.full((12, 12, 3), 20, np.uint8)
        source[3:6, 4:7] = 250
        candidate = np.full_like(source, 20)
        candidate[4, 5] = source[4, 5]
        source_mask = np.zeros(source.shape[:2], np.uint8)
        source_mask[3:6, 4:7] = 255
        result = ati.detect_post_reconstruction_residuals(
            source, candidate, source_mask=source_mask)
        self.assertEqual(result["post_reconstruction_residual_pixels"], 1)

    def test_sampling_context_excludes_all_source_pixels(self):
        source_mask = np.zeros((16, 16), np.uint8)
        source_mask[6:10, 6:10] = 255
        protected = np.zeros_like(source_mask)
        protected[:, 1] = 255
        context = ati.build_reconstruction_context(
            source_mask=source_mask, protected_structure_mask=protected)
        self.assertEqual(
            np.count_nonzero(
                context["valid_context_mask"]
                & context["sampling_exclusion_mask"]),
            0,
        )
        self.assertEqual(
            np.count_nonzero(
                context["valid_context_mask"] & source_mask),
            0,
        )

    def test_selector_rejects_candidate_with_source_residual(self):
        selected = ati.select_best_candidate([{
            "method": "fake",
            "radius": 1,
            "overall_score": 1.0,
            "edge_continuity_score": 1.0,
            "texture_consistency_score": 1.0,
            "seam_score": 0.0,
            "artifact_score": 0.0,
            "changed_pixels_outside_change_mask": 0,
            "post_reconstruction_residual_pixels": 1,
            "source_contaminated_samples_used": 0,
        }])
        self.assertEqual(selected["status"], "needs_review")
        self.assertIn(
            "post_reconstruction_source_residual",
            selected["reason_codes"],
        )


class ResidualComponentReviewStoreTests(unittest.TestCase):
    def test_decision_is_content_bound_and_owner_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ResidualComponentReviewStore(Path(tmp) / "review.sqlite3")
            decision = store.persist(
                owner="owner",
                residual_analysis_id="analysis",
                component_id="component",
                component_bitmap_hash="a" * 64,
                pixel_decisions=[
                    {"coordinate": [2, 3], "value": [4, 5, 6],
                     "classification": "text", "confidence": 0.99}
                ],
                evidence={"source": "local_visual_review"},
                authorization="delegated_by_user",
                reviewer="codex",
                status="resolved",
            )
            self.assertEqual(
                decision["component_review_decision_id"],
                store.persist(
                    owner="owner",
                    residual_analysis_id="analysis",
                    component_id="component",
                    component_bitmap_hash="a" * 64,
                    pixel_decisions=[
                        {"coordinate": [2, 3], "value": [4, 5, 6],
                         "classification": "text", "confidence": 0.99}
                    ],
                    evidence={"source": "local_visual_review"},
                    authorization="delegated_by_user",
                    reviewer="codex",
                    status="resolved",
                )["component_review_decision_id"],
            )
            self.assertIsNone(store.latest(
                owner="other",
                residual_analysis_id="analysis",
                component_id="component",
            ))
            store.close()

    def test_lists_only_latest_decision_for_each_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ResidualComponentReviewStore(Path(tmp) / "review.sqlite3")
            common = {
                "owner": "owner-a",
                "residual_analysis_id": "analysis-a",
                "component_bitmap_hash": "bitmap-a",
                "evidence": {},
                "authorization": "delegated_by_user",
                "reviewer": "reviewer-a",
                "status": "resolved",
            }
            store.persist(
                **common,
                component_id="component-a",
                pixel_decisions=[{
                    "coordinate": [1, 1],
                    "value": [0, 0, 0],
                    "classification": "uncertain",
                }],
            )
            latest = store.persist(
                **common,
                component_id="component-a",
                pixel_decisions=[{
                    "coordinate": [1, 1],
                    "value": [0, 0, 0],
                    "classification": "text",
                }],
            )
            other = store.persist(
                **common,
                component_id="component-b",
                pixel_decisions=[{
                    "coordinate": [2, 2],
                    "value": [0, 0, 0],
                    "classification": "art",
                }],
            )
            reviews = store.list_latest_for_analysis(
                owner="owner-a", residual_analysis_id="analysis-a")
            self.assertEqual(
                [latest["component_review_decision_id"],
                 other["component_review_decision_id"]],
                [review["component_review_decision_id"]
                 for review in reviews],
            )
            store.close()


class MaskRevisionLineageTests(unittest.TestCase):
    def test_resolved_review_can_supersede_same_bitmap_without_rewriting_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MaskRevisionStore(Path(tmp) / "mask.sqlite3")
            common = {
                "supersedes_mask_decision_id": "decision",
                "owner": "owner", "job_id": "job", "run_id": "run",
                "revision_id": "revision", "page_id": "page",
                "region_id": "region", "source_hash": "source",
                "base_segmentation_hash": "base",
                "final_mask_hash": "same-mask",
            }
            blocked = store.upsert(
                **common,
                previous_mask_hash="previous",
                status="blocked_pending_component_review",
            )
            resolved = store.upsert(
                **common,
                previous_mask_hash="same-mask",
                supersedes_mask_revision_id=blocked["mask_revision_id"],
                status="confirmed",
            )
            self.assertNotEqual(
                blocked["mask_revision_id"], resolved["mask_revision_id"])
            rows = store._conn.execute(
                "SELECT COUNT(*) FROM human_mask_revisions").fetchone()[0]
            self.assertEqual(rows, 2)
            store.close()

    def test_uncertain_pixel_keeps_decision_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ResidualComponentReviewStore(Path(tmp) / "review.sqlite3")
            result = store.persist(
                owner="owner",
                residual_analysis_id="analysis",
                component_id="component",
                component_bitmap_hash="b" * 64,
                pixel_decisions=[
                    {"coordinate": [0, 0], "value": [0, 0, 0],
                     "classification": "uncertain", "confidence": 0.5}
                ],
                evidence={},
                authorization="delegated_by_user",
                reviewer="codex",
                status="blocked_pending_component_review",
            )
            self.assertEqual(
                result["status"], "blocked_pending_component_review")
            store.close()


if __name__ == "__main__":
    unittest.main()
