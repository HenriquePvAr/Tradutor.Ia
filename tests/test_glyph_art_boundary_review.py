import _test_bootstrap  # noqa: F401

import unittest
from pathlib import Path

import numpy as np

import glyph_art_boundary_review as gabr


class ConflictArtifactTests(unittest.TestCase):
    def test_artifact_and_segments_are_deterministic(self):
        mask = np.zeros((30, 40), np.uint8)
        mask[2:5, 3:7] = 255
        mask[20:24, 30:35] = 255
        kwargs = dict(
            identity={"owner": "owner", "source_hash": "source"},
            glyph_envelope_id="glyph", protected_art_hash="art",
            previous_mask_hash="mask")
        first = gabr.build_conflict_artifact(mask, **kwargs)
        second = gabr.build_conflict_artifact(mask, **kwargs)
        self.assertEqual(first["conflict_artifact_id"],
                         second["conflict_artifact_id"])
        self.assertEqual(
            [item["segment_id"] for item in first["segments"]],
            [item["segment_id"] for item in second["segments"]])
        self.assertEqual(len(first["segments"]), 2)

    def test_segment_identity_does_not_use_component_order(self):
        mask = np.zeros((20, 20), np.uint8)
        mask[2:4, 2:4] = 255
        artifact = gabr.build_conflict_artifact(
            mask, identity={"owner": "owner"}, glyph_envelope_id="g",
            protected_art_hash="a", previous_mask_hash="m")
        self.assertEqual(len(artifact["segments"][0]["segment_id"]), 64)


class ReviewDraftTests(unittest.TestCase):
    def test_uncertain_decision_stays_blocked(self):
        draft = gabr.normalize_review_draft(
            conflict_artifact_id="artifact", owner="owner",
            segment_decisions={"segment": "uncertain"})
        self.assertEqual(draft["status"], "blocked_with_uncertainty")

    def test_confirmation_with_uncertainty_fails_closed(self):
        with self.assertRaisesRegex(
                ValueError, "boundary_review_uncertainty_remaining"):
            gabr.normalize_review_draft(
                conflict_artifact_id="artifact", owner="owner",
                segment_decisions={"segment": "uncertain"}, confirm=True)

    def test_confirmed_classifications_are_content_addressed(self):
        kwargs = dict(
            conflict_artifact_id="artifact", owner="owner",
            segment_decisions={"a": "glyph_outline", "b": "protected_art"},
            brush_operations=[{"tool": "glyph_outline", "points": [[1, 2]]}],
            view_state={"opacity": 50, "zoom": 2})
        first = gabr.normalize_review_draft(**kwargs, confirm=True)
        second = gabr.normalize_review_draft(**kwargs, confirm=True)
        self.assertEqual(first["status"], "confirmed")
        self.assertEqual(first["review_decision_id"],
                         second["review_decision_id"])

    def test_invalid_classification_is_rejected(self):
        with self.assertRaisesRegex(
                ValueError, "boundary_review_classification_invalid"):
            gabr.normalize_review_draft(
                conflict_artifact_id="artifact", owner="owner",
                segment_decisions={"a": "invented"})


class BoundaryEditorUiContractTests(unittest.TestCase):
    def test_editor_has_layers_segments_opacity_and_fail_closed_copy(self):
        root = Path(__file__).resolve().parent
        script = (root / "static" / "tradutor_ui.js").read_text(
            encoding="utf-8")
        css = (root / "static" / "tradutor_ui.css").read_text(
            encoding="utf-8")
        for token in (
            "REVISAR TEXTO × ARTE", "data-boundary-opacity",
            "data-boundary-segment", "MARCAR COMO TEXTO",
            "MARCAR COMO ARTE", "MANTER INCERTO",
            "data-boundary-tool=\"undo\"", "data-boundary-tool=\"redo\"",
            "tradutor-boundary-review:",
        ):
            self.assertIn(token, script)
        self.assertIn("image-rendering:pixelated", css)

    def test_bridge_exposes_content_addressed_conflict(self):
        source = Path(__file__).resolve().parent.joinpath(
            "ui_bridge.py").read_text(encoding="utf-8")
        self.assertIn("build_conflict_artifact(", source)
        self.assertIn('"first_pending_segment_id"', source)
        self.assertNotIn('page_id == "p004"', source)


if __name__ == "__main__":
    unittest.main()
