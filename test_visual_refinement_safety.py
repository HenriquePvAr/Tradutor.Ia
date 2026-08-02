"""Synthetic local tests for visual refinement safety.

The offline guard makes accidental network use fail before any request.
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

import art_text_inpainting as ati
import font_fidelity as ff
import provider_execution
from ocr_balloon import get_font


def _font_path_for(role):
    for path in ff.role_font_paths(role):
        if Path(path).is_file():
            return path
    return ""


class ProviderGuardContract(unittest.TestCase):
    def test_visual_preview_reviewer_never_calls_provider(self):
        reviewer = provider_execution.NoProviderReviewer()
        with self.assertRaisesRegex(provider_execution.ProviderCallNotAuthorized,
                                    "provider_calls_forbidden_in_visual_refinement"):
            reviewer.translate_many(["texto"])


class FontFidelityContract(unittest.TestCase):
    def test_valid_font_runtime_records_actual_file_and_hash(self):
        path = _font_path_for("regular")
        if not path:
            self.skipTest("no local regular font available")
        font, runtime = ff.resolve_font("regular", 24, text="CAFÉ")
        self.assertTrue(runtime["font_load_success"])
        self.assertFalse(runtime["fallback_used"])
        self.assertTrue(Path(runtime["resolved_font_path"]).is_file())
        self.assertEqual(len(runtime["font_file_hash"]), 64)
        self.assertEqual(runtime["glyph_support"]["status"], "complete")
        raster = ff.render_text_raster("CAFÉ", font)
        self.assertGreater(ff.raster_fingerprint(raster)["ink_pixels"], 0)

    def test_missing_configured_font_falls_back_with_reason(self):
        font, runtime = ff.resolve_font(
            "regular",
            24,
            configured_font_path=str(Path(tempfile.gettempdir()) / "missing-font.ttf"),
            prefer_role=False,
            text="texto",
        )
        self.assertTrue(runtime["font_load_success"])
        self.assertEqual(runtime["requested_font"], "regular")
        self.assertTrue(runtime["fallback_used"])
        self.assertEqual(runtime["fallback_reason"], "configured_font_unavailable")
        self.assertGreater(ff.raster_fingerprint(ff.render_text_raster("texto", font))["ink_pixels"], 0)

    def test_explicit_preview_role_precedes_configured_regular_font(self):
        regular = _font_path_for("regular")
        shout = _font_path_for("shout")
        if not regular or not shout:
            self.skipTest("local role fonts unavailable")
        font = get_font(regular, 28, role="shout", prefer_role=True, text="TEXTO!")
        runtime = getattr(font, "tradutor_font_runtime", {})
        self.assertEqual(runtime.get("requested_font"), "shout")
        self.assertEqual(Path(runtime.get("resolved_font_path")).resolve(), Path(shout).resolve())

    def test_typography_gate_requires_no_fallback_and_complete_glyphs(self):
        gate = ff.typography_gate(
            {"font_load_success": True, "fallback_used": True,
             "glyph_support": {"status": "complete"}},
            {"style_similarity": 0.9, "stroke_similarity": 0.9, "slant_similarity": 0.9,
             "condensation_similarity": 0.9, "spacing_similarity": 0.9,
             "alignment_similarity": 1.0},
        )
        self.assertEqual(gate["status"], "needs_review")
        self.assertIn("font_fallback_detected", gate["reason_codes"])


class InpaintingContract(unittest.TestCase):
    def _synthetic_region(self, *, line=True, texture=False):
        region = np.full((120, 260, 3), 190, dtype=np.uint8)
        if texture:
            for x in range(0, region.shape[1], 8):
                cv2.line(region, (x, 0), (x + 80, region.shape[0] - 1), (175, 175, 175), 1)
        if line:
            cv2.line(region, (0, 80), (region.shape[1] - 1, 30), (80, 80, 80), 2)
        img = Image.fromarray(cv2.cvtColor(region, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(img)
        draw.text((35, 42), "TEXTO", fill=(20, 20, 20), stroke_width=2, stroke_fill=(245, 245, 245))
        return cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)

    def test_text_core_outline_antialias_masks_are_distinct(self):
        region = self._synthetic_region()
        masks = ati.build_text_masks(region)
        self.assertTrue(masks["valid"], masks["reason_codes"])
        self.assertGreater(int((masks["text_core_mask"] > 0).sum()), 0)
        self.assertGreater(int((masks["outline_mask"] > 0).sum()), 0)
        self.assertGreaterEqual(int((masks["combined_inpainting_mask"] > 0).sum()),
                                int((masks["text_core_mask"] > 0).sum()))
        self.assertEqual(len(masks["mask_hash"]), 64)

    def test_inpainting_candidates_are_local_and_scored(self):
        region = self._synthetic_region(texture=True)
        masks = ati.build_text_masks(region)
        candidates = ati.generate_inpainting_candidates(region, masks)
        self.assertTrue(candidates)
        methods = {item["method"] for item in candidates}
        self.assertIn("local_color", methods)
        self.assertIn("local_gradient", methods)
        self.assertIn("telea", methods)
        self.assertIn("navier_stokes", methods)
        self.assertTrue(
            {"local_patch", "verified_nearest_donor", "deterministic_multiscale_patch"} & methods
        )
        selected = ati.select_best_candidate(candidates)
        self.assertIn(selected["status"], {"passed", "needs_review"})
        self.assertIn("edge_continuity_score", selected)
        self.assertIn("texture_consistency_score", selected)

    def test_too_large_mask_fails_closed(self):
        region = np.full((100, 120, 3), 240, dtype=np.uint8)
        region[5:95, 5:115] = 0
        masks = ati.build_text_masks(region)
        self.assertFalse(masks["valid"])
        self.assertIn("text_mask_too_large", masks["reason_codes"])
        self.assertEqual(ati.generate_inpainting_candidates(region, masks), [])


if __name__ == "__main__":
    unittest.main()
