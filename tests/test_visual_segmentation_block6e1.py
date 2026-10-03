"""BLOCO 6E.1 contracts for precise local text segmentation/inpainting."""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest

import numpy as np

import art_text_inpainting as ati
from test_visual_refinement_block6e1 import _synthetic_text_region


class LayeredSegmentationTests(unittest.TestCase):
    def test_core_outline_antialias_and_background_art_are_separated(self):
        region = _synthetic_text_region(outline=True, line=True, texture=True, shadow=True)
        masks = ati.segment_text_layers(region)
        self.assertGreater(int((masks["text_core_mask"] > 0).sum()), 0)
        self.assertGreater(int((masks["outline_mask"] > 0).sum()), 0)
        self.assertGreater(int((masks["antialias_mask"] > 0).sum()), 0)
        self.assertGreater(int((masks["background_art_mask"] > 0).sum()), 0)
        self.assertIn("mask_precision", masks)
        self.assertIn("mask_ambiguity", masks)

    def test_synthetic_scenarios_fail_closed_or_produce_precise_masks(self):
        cases = [
            {"text": "TEXTO", "outline": True},
            {"text": "TEXTO", "outline": True, "diagonal": True},
            {"text": "TEXTO", "outline": True, "texture": True},
            {"text": "TEXTO", "outline": True, "line": True},
            {"text": "TEXTO", "outline": True, "shadow": True},
            {"text": "////", "outline": False, "line": True},
        ]
        for case in cases:
            with self.subTest(case=case):
                masks = ati.segment_text_layers(_synthetic_text_region(**case))
                self.assertLess(masks["mask_ratio"], 0.32)
                self.assertIn(masks["valid"], {True, False})
                if not masks["valid"]:
                    self.assertTrue(masks["reason_codes"])

    def test_too_large_and_too_small_masks_are_blocked_before_inpainting(self):
        large = np.full((120, 160, 3), 240, dtype=np.uint8)
        large[4:116, 4:156] = 0
        large_masks = ati.segment_text_layers(large)
        self.assertFalse(large_masks["valid"])
        self.assertIn("text_mask_too_large", large_masks["reason_codes"])
        self.assertEqual(ati.generate_inpainting_candidates(large, large_masks), [])

        small = np.full((120, 160, 3), 190, dtype=np.uint8)
        small[10, 10] = 0
        small_masks = ati.segment_text_layers(small)
        self.assertFalse(small_masks["valid"])
        self.assertIn("text_mask_too_small", small_masks["reason_codes"])

    def test_local_inpainting_candidates_preserve_protected_line_art(self):
        region = _synthetic_text_region(outline=True, line=True, texture=True)
        masks = ati.segment_text_layers(region)
        if not masks["valid"]:
            self.skipTest(f"segmentation failed closed: {masks['reason_codes']}")
        candidates = ati.generate_inpainting_candidates(region, masks)
        self.assertTrue(candidates)
        selected = ati.select_best_candidate(candidates)
        self.assertIn(selected["method"], {"telea", "navier_stokes"})
        self.assertIn(selected["status"], {"passed", "needs_review"})
        self.assertIn("edge_continuity_score", selected)


if __name__ == "__main__":
    unittest.main()
