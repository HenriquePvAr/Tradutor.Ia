"""BLOCO 6E.2 contracts for delegated font choice and safe mask editing."""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import tempfile
import unittest
from pathlib import Path

import font_fidelity as ff
import human_mask_decisions as hmd
import human_typography_decisions as htd


ROOT = Path(__file__).resolve().parent


def _candidate(candidate_id: str, *, style=0.7, stroke=0.5, condensation=0.7,
               spacing=0.7, fit=1.0, fallback=False, actual_font="Measured Font"):
    return {
        "candidate_id": candidate_id,
        "requested_font": "display",
        "actual_font": actual_font,
        "font_path_hash": candidate_id.rjust(64, "a")[:64],
        "font_load_success": True,
        "fallback_used": fallback,
        "glyph_support": {"status": "complete"},
        "font_size": 42,
        "tracking": 0,
        "slant": 5.0,
        "stroke_score": stroke,
        "condensation_score": condensation,
        "spacing_score": spacing,
        "fit_score": fit,
        "style_score": style,
        "raster_similarity": style,
        "overall_score": style * 0.55 + fit * 0.45,
    }


class DelegatedTypographySelectionTests(unittest.TestCase):
    def test_delegated_choice_uses_measured_visual_scores(self):
        winner = _candidate("winner", style=0.72, stroke=0.52, condensation=0.75, spacing=0.75)
        runner = _candidate("runner", style=0.41, stroke=0.15, condensation=0.36, spacing=0.36)
        result = ff.select_delegated_typography_candidate([runner, winner])
        self.assertEqual(result["status"], "selected_for_preview_generation")
        self.assertEqual(result["selected"]["candidate_id"], "winner")
        self.assertEqual(result["runner_up"]["candidate_id"], "runner")
        self.assertIn("delegated_by_user", result["reason_codes"])

    def test_delegated_choice_refuses_unsafe_or_fallback_candidates(self):
        result = ff.select_delegated_typography_candidate([
            _candidate("fallback", fallback=True),
            _candidate("does-not-fit", fit=0.4),
        ])
        self.assertEqual(result["status"], "no_acceptable_typography_candidate")

    def test_delegated_choice_metadata_is_persisted_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = htd.HumanTypographyDecisionStore(Path(tmp) / "jobs.sqlite3")
            try:
                selection = ff.select_delegated_typography_candidate([
                    _candidate("winner"), _candidate("runner", style=0.4)
                ])
                first = store.upsert(
                    owner="owner", job_id="job", run_id="run", revision_id="rev",
                    page_id="page", region_id="region", source_hash="source",
                    human_translation_decision_id="human",
                    candidate=selection["selected"],
                    status="selected_for_preview_generation",
                    visual_evidence={"reason_codes": selection["reason_codes"]},
                    selection_reason="best measured visual match",
                    runner_authorization="delegated_by_user",
                    confidence=selection["confidence"],
                    runner_up_candidate=selection["runner_up"],
                )
                second = store.upsert(
                    owner="owner", job_id="job", run_id="run", revision_id="rev",
                    page_id="page", region_id="region", source_hash="source",
                    human_translation_decision_id="human",
                    candidate=selection["selected"],
                    status="selected_for_preview_generation",
                    runner_authorization="delegated_by_user",
                    confidence=selection["confidence"],
                )
                self.assertEqual(first["font_choice_decision_id"], second["font_choice_decision_id"])
                self.assertEqual(first["runner_authorization"], "delegated_by_user")
                self.assertEqual(first["status"], "selected_for_preview_generation")
                self.assertGreater(first["confidence"], 0.0)
            finally:
                store.close()


class HumanMaskDecisionTests(unittest.TestCase):
    def test_mask_store_is_owner_scoped_idempotent_and_reversible(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = hmd.HumanMaskDecisionStore(Path(tmp) / "jobs.sqlite3")
            try:
                first = store.upsert(
                    owner="owner", job_id="job", run_id="run", revision_id="rev",
                    page_id="page", region_id="region", source_hash="source",
                    base_segmentation_hash="seg", include_mask=[[1, 1, 4, 4]])
                second = store.upsert(
                    owner="owner", job_id="job", run_id="run", revision_id="rev",
                    page_id="page", region_id="region", source_hash="source",
                    base_segmentation_hash="seg", include_mask=[[1, 1, 4, 4]])
                self.assertEqual(first["human_mask_decision_id"], second["human_mask_decision_id"])
                self.assertIsNone(store.latest_for_region(
                    owner="other", job_id="job", run_id="run",
                    revision_id="rev", region_id="region"))
            finally:
                store.close()

    def test_mask_confirmation_guards_fail_closed(self):
        kwargs = {"region_box": [0, 0, 100, 100], "base_segmentation_hash": "seg", "source_hash": "source"}
        valid = hmd.validate_mask_payload({
            "base_segmentation_hash": "seg",
            "source_hash": "source",
            "include_mask": [[10, 10, 20, 20]],
        }, **kwargs)
        self.assertEqual(valid["status"], "valid_for_local_preview_candidate")
        cases = [
            ({"base_segmentation_hash": "old", "source_hash": "source", "include_mask": [[1, 1, 2, 2]]},
             "mask_segmentation_hash_mismatch"),
            ({"base_segmentation_hash": "seg", "source_hash": "old", "include_mask": [[1, 1, 2, 2]]},
             "mask_source_hash_mismatch"),
            ({"base_segmentation_hash": "seg", "source_hash": "source", "include_mask": []},
             "mask_empty"),
            ({"base_segmentation_hash": "seg", "source_hash": "source", "include_mask": [[0, 0, 80, 80]]},
             "mask_area_excessive"),
            ({"base_segmentation_hash": "seg", "source_hash": "source", "include_mask": [[1, 1, 2, 2]],
              "protected_mask": [[1, 1, 2, 2]]},
             "mask_protected_overlap"),
            ({"base_segmentation_hash": "seg", "source_hash": "source", "include_mask": [[1, 1, 2, 2]],
              "uncertain_mask": [[1, 1, 2, 2]]},
             "mask_uncertain_pixels_unresolved"),
        ]
        for payload, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ValueError, reason):
                    hmd.validate_mask_payload(payload, **kwargs)


class Block6E2UiContractTests(unittest.TestCase):
    def test_mask_editor_ui_is_visible_accessible_and_non_destructive(self):
        ui_js = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        ui_css = (ROOT / "static" / "tradutor_ui.css").read_text(encoding="utf-8")
        app_py = (ROOT / "app_ui.py").read_text(encoding="utf-8")
        self.assertIn("/api/ui/human-mask/editor-state", ui_js)
        self.assertIn("REFINAR MÁSCARA", ui_js)
        self.assertIn("blocked_pending_human_mask", ui_js)
        self.assertIn("INCLUIR TEXTO", ui_js)
        self.assertIn("EXCLUIR ARTE", ui_js)
        self.assertIn("PROTEGER LINHAS", ui_js)
        self.assertIn("MARCAR INCERTO", ui_js)
        self.assertIn("DESFAZER", ui_js)
        self.assertIn("REFAZER", ui_js)
        self.assertIn("RESTAURAR AUTOMÁTICA", ui_js)
        self.assertIn("validation_halo", ui_js)
        bridge_py = (ROOT / "ui_bridge.py").read_text(encoding="utf-8")
        self.assertIn('layers.get("combined_inpainting_mask")', bridge_py)
        self.assertIn('"combined_text_mask"', bridge_py)
        self.assertIn("hm-refine-panel", ui_css)
        self.assertIn("hm-refine-tools", ui_css)
        self.assertIn("hm-layer-grid", ui_css)
        self.assertIn("repeat(auto-fit,minmax(160px,1fr))", ui_css.replace(" ", ""))
        self.assertIn("/api/ui/human-mask/save", app_py)
        self.assertIn("/api/ui/human-mask/confirm", app_py)
        self.assertNotIn("window.alert(", ui_js[ui_js.index("async function previewAction"):
                                                ui_js.index("function selectedTriageRegions")])


if __name__ == "__main__":
    unittest.main()
