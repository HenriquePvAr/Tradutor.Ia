"""BLOCO 6E.1 contracts for human font choice and precise segmentation.

Everything is synthetic/local: no network, no provider and no chapter-specific
runtime rules.
"""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

import font_fidelity as ff
import human_typography_decisions as htd


ROOT = Path(__file__).resolve().parent


def _synthetic_text_region(*, text="TEXTO", outline=True, line=False,
                           texture=False, shadow=False, diagonal=False) -> np.ndarray:
    region = np.full((140, 320, 3), 190, dtype=np.uint8)
    if texture:
        for x in range(-80, region.shape[1], 12):
            cv2.line(region, (x, 0), (x + 120, region.shape[0] - 1), (170, 170, 170), 1)
    if line:
        cv2.line(region, (0, 100 if not diagonal else 130),
                 (region.shape[1] - 1, 40 if not diagonal else 8), (70, 70, 70), 2)
    img = Image.fromarray(cv2.cvtColor(region, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(img)
    if shadow:
        draw.text((40, 49), text, fill=(80, 80, 80), stroke_width=1, stroke_fill=(80, 80, 80))
    draw.text((38, 45), text, fill=(20, 20, 20),
              stroke_width=2 if outline else 0, stroke_fill=(245, 245, 245))
    return cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)


class TypographyCandidateTests(unittest.TestCase):
    def test_authorized_font_inventory_records_real_loaded_fonts(self):
        inventory = ff.authorized_font_inventory(text="CAFÉ")
        loaded = [item for item in inventory if item["font_load_success"]]
        if not loaded:
            self.skipTest("no allow-listed local fonts available")
        self.assertTrue(all(item["font_file_hash"] for item in loaded))
        self.assertTrue(all(item["glyph_support"]["status"] == "complete" for item in loaded))

    def test_generates_between_three_and_five_ranked_real_candidates(self):
        reference = _synthetic_text_region(text="REAL", outline=True)
        candidates = ff.generate_typography_candidates(reference, "CAFÉ DE VERDADE.", max_candidates=5)
        if len(candidates) < 3:
            self.skipTest("fewer than three allow-listed local fonts available")
        self.assertGreaterEqual(len(candidates), 3)
        self.assertLessEqual(len(candidates), 5)
        scores = [item["overall_score"] for item in candidates]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(all(item["fallback_used"] is False for item in candidates))
        self.assertEqual(len({item["font_path_hash"] for item in candidates}), len(candidates))

    def test_missing_or_unavailable_fonts_do_not_fake_candidates(self):
        candidates = ff.generate_typography_candidates(np.zeros((1, 1), dtype=np.uint8), "texto")
        self.assertEqual(candidates, [])


class TypographyDecisionStoreTests(unittest.TestCase):
    def test_choice_is_idempotent_owner_scoped_and_reversible(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = htd.HumanTypographyDecisionStore(Path(tmp) / "jobs.sqlite3")
            try:
                candidate = {
                    "candidate_id": "candidate-a",
                    "actual_font": "Local Font",
                    "font_path_hash": "a" * 64,
                    "requested_font": "shout",
                    "font_size": 42,
                    "slant": -5.0,
                    "resolved_font_path": str(Path(tmp) / "font.ttf"),
                }
                first = store.upsert(
                    owner="user-a", job_id="job", run_id="run", revision_id="rev",
                    page_id="page", region_id="region", source_hash="source",
                    human_translation_decision_id="human", candidate=candidate)
                second = store.upsert(
                    owner="user-a", job_id="job", run_id="run", revision_id="rev",
                    page_id="page", region_id="region", source_hash="source",
                    human_translation_decision_id="human", candidate=candidate)
                self.assertEqual(first["font_choice_decision_id"], second["font_choice_decision_id"])
                self.assertIsNone(store.get(first["font_choice_decision_id"], owner="user-b"))
                discarded = store.discard(first["font_choice_decision_id"], owner="user-a")
                self.assertEqual(discarded["status"], "discarded")
            finally:
                store.close()

    def test_choice_requires_auditable_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = htd.HumanTypographyDecisionStore(Path(tmp) / "jobs.sqlite3")
            try:
                with self.assertRaisesRegex(ValueError, "font_candidate_not_auditable"):
                    store.upsert(
                        owner="user", job_id="job", run_id="run", revision_id="rev",
                        page_id="page", region_id="region", source_hash="source",
                        human_translation_decision_id="human", candidate={"candidate_id": ""})
            finally:
                store.close()


class HumanPreviewUiContractTests(unittest.TestCase):
    def test_dynamic_preview_buttons_are_bound_through_audit_list(self):
        ui_js = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        self.assertIn("$('#auditList')?.addEventListener('click'", ui_js)
        self.assertIn("event.target.closest('[data-preview-action]')", ui_js)
        self.assertIn("void previewAction(preview.dataset.previewAction", ui_js)
        self.assertIn("function bindPreviewActionButtons", ui_js)
        self.assertIn("event.stopPropagation()", ui_js)
        self.assertIn("button.dataset.previewBound = '1'", ui_js)
        self.assertIn("data-preview-action=\"font-options\"", ui_js)
        self.assertIn("ESCOLHER TIPOGRAFIA", ui_js)

    def test_font_choice_panel_actions_are_visible_and_non_blocking(self):
        ui_js = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        preview_block = ui_js[ui_js.index("async function previewAction"):
                              ui_js.index("function selectedTriageRegions")]
        app_py = (ROOT / "app_ui.py").read_text(encoding="utf-8")
        self.assertIn("/api/ui/human-translation/font-candidates", ui_js)
        self.assertIn("/api/ui/human-translation/font-choice", ui_js)
        self.assertIn("font_choice_base_page_unavailable", app_py)
        self.assertIn("A página base desta região não foi encontrada.", app_py)
        self.assertIn("ESCOLHER ESTA TIPOGRAFIA", ui_js)
        self.assertIn("ABRIR AMPLIADO", ui_js)
        self.assertIn("PEDIR OUTRAS OPÇÕES", ui_js)
        self.assertIn("MANTER PENDENTE", ui_js)
        self.assertNotIn("window.alert(", preview_block)
        self.assertNotIn("window.confirm(", preview_block)
        self.assertNotIn("window.prompt(", preview_block)

    def test_font_choice_layout_is_responsive(self):
        ui_css = (ROOT / "static" / "tradutor_ui.css").read_text(encoding="utf-8")
        self.assertIn(".font-choice-panel", ui_css)
        self.assertIn(".font-choice-grid", ui_css)
        self.assertIn("repeat(auto-fit,minmax(180px,1fr))", ui_css.replace(" ", ""))
        self.assertIn("overflow-wrap:anywhere", ui_css.replace(" ", ""))

    def test_font_context_accepts_existing_page_name_variants(self):
        bridge_py = (ROOT / "ui_bridge.py").read_text(encoding="utf-8")
        context_block = bridge_py[bridge_py.index("def _human_font_context"):
                                  bridge_py.index("def _font_candidate_cache_dir")]
        self.assertIn('f"p{page:03d}.png"', context_block)
        self.assertIn('f"page_{page:03d}.png"', context_block)
        self.assertLess(context_block.index('f"p{page:03d}.png"'),
                        context_block.index('f"page_{page:03d}.png"'))


if __name__ == "__main__":
    unittest.main()
