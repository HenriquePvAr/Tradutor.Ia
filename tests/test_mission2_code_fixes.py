import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import cv2

import ocr_balloon
import ocr_engine
from ocr_balloon import OCRLine, TextGroup


class Mission2ValidationFixTests(unittest.TestCase):
    def _group(self):
        line = OCRLine(
            text="HELLO", confidence=0.99,
            raw_text="HELLO",
            polygon=np.array([[4, 4], [44, 4], [44, 28], [4, 28]], dtype=np.int32),
            box=(4, 4, 40, 24),
        )
        return TextGroup(
            group_id="uniform",
            lines=[line],
            text="HELLO",
            translation="OLA",
            translation_candidate="OLA",
            translation_valid=True,
            sent_to_translation=True,
            classification="speech",
            draw_box=(4, 4, 40, 24),
            safe_area=(4, 4, 40, 24),
            translation_box=(4, 4, 40, 24),
            source_completeness={"status": "pass", "source_bbox": [4, 4, 40, 24]},
        )

    def test_uniform_render_has_positive_pixel_evidence_when_ocr_misses(self):
        image = np.full((40, 60, 3), 255, dtype=np.uint8)
        group = self._group()
        removal = {
            "measured": True,
            "source_text_coverage": 1.0,
            "largest_uncovered_source_component": 0,
            "largest_unmasked_source_component": 0,
        }
        with patch.object(ocr_balloon.OCREngine, "_detect_with_rapidocr", return_value=[]), \
             patch.object(ocr_balloon.source_completeness, "expected_physical_tokens", return_value=(
                 {"HELLO"}, {"status": "pass", "source_bbox": [4, 4, 40, 24]}
             )):
            result = ocr_balloon._post_render_source_text_check(
                image, group, original_bgr=image, cleanup_mask=np.ones((40, 60), np.uint8),
                removal=removal,
                rendered_occupancy={"translated_text_box_occupancy_ratio": 0.08},
            )
        self.assertTrue(result["target_text_found"])
        self.assertEqual(result["render_evidence"], "render_layer_pixel_delta")

    def test_missing_render_with_zero_occupancy_stays_review(self):
        image = np.full((40, 60, 3), 255, dtype=np.uint8)
        group = self._group()
        removal = {
            "measured": True,
            "source_text_coverage": 1.0,
            "largest_uncovered_source_component": 0,
            "largest_unmasked_source_component": 0,
        }
        with patch.object(ocr_balloon.OCREngine, "_detect_with_rapidocr", return_value=[]), \
             patch.object(ocr_balloon.source_completeness, "expected_physical_tokens", return_value=(
                 {"HELLO"}, {"status": "pass", "source_bbox": [4, 4, 40, 24]}
             )):
            result = ocr_balloon._post_render_source_text_check(
                image, group, original_bgr=image, cleanup_mask=np.ones((40, 60), np.uint8),
                removal=removal,
                rendered_occupancy={"translated_text_box_occupancy_ratio": 0.0},
            )
        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "translated_text_missing_after_render")

    def test_art_only_zero_lines_has_no_story_review_signal(self):
        image = np.zeros((200, 300, 3), dtype=np.uint8)
        with patch.object(ocr_engine, "_estimate_text_regions", return_value=0), \
             patch.object(ocr_engine, "_estimate_story_text_regions", return_value=1):
            suspicion = ocr_engine._rapidocr_suspicion(image, [])
        self.assertNotIn("zero_lines_on_story_like_page", suspicion["reasons"])

    def test_zero_lines_with_text_evidence_keeps_review_signal(self):
        image = np.zeros((200, 300, 3), dtype=np.uint8)
        with patch.object(ocr_engine, "_estimate_text_regions", return_value=2), \
             patch.object(ocr_engine, "_estimate_story_text_regions", return_value=1):
            suspicion = ocr_engine._rapidocr_suspicion(image, [])
        self.assertIn("zero_lines_on_story_like_page", suspicion["reasons"])

    def test_canonical_p029_art_only_does_not_create_false_review(self):
        image_path = Path(__file__).parent / ".local-audit/mission6d_corpus_recovery/incremental_pages/page_029.png"
        image = cv2.imread(str(image_path))
        self.assertIsNotNone(image)
        engine = ocr_engine.OCREngine("en", engine="rapidocr", fallback_engine="")
        with patch.object(engine, "_rapidocr_story_region_retry", return_value=([], [])):
            suspicion = ocr_engine._rapidocr_suspicion(image, [])
            engine._handle_rapidocr_suspicion(image, 29, [], suspicion, [])
        self.assertEqual(engine.last_run_metadata["ocr_sufficiency"], ocr_engine.OCR_SUFFICIENT)
        self.assertEqual(engine.last_run_metadata["fallback_reason"], "zero_lines_on_art_only_page")

    def test_ambiguous_empty_retry_contract_remains_review(self):
        group = self._group()
        group.translation = ""
        group.translation_candidate = ""
        group.translation_valid = False
        group.translation_validation_reason = "ambiguous_ocr"
        group.translation_final_state = ""
        ocr_balloon._ensure_translation_terminal_state(group)
        self.assertTrue(group.manual_review_required)
        self.assertIn(group.translation_final_state, {"manual_review", "translation_failed"})


if __name__ == "__main__":
    unittest.main()
