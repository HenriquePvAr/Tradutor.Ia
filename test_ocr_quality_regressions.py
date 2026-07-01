import unittest
from unittest.mock import patch

import cv2
import numpy as np
import config

from ocr_balloon import (
    TextGroup,
    _assign_visual_white_regions,
    _classify_background_region,
    _enforce_visual_bounds,
    _line_belongs_to_group,
    _group_lines,
    _refine_classification_with_background,
    _split_groups_at_sentence_boundaries,
    _white_patch_artifact_metrics,
    group_needs_selective_fallback,
    score_group_ocr_quality,
    validate_translation_text,
)
from benchmark_pipeline import _grouping_fallback_reason
from ocr_engine import (
    OCRLine,
    assess_ocr_repair,
    repair_ocr_text,
    segment_compact_english_word,
    suggest_english_word,
)
from translator_nvidia import TranslatorNvidiaBatch


def _line(text, confidence=0.92):
    polygon = np.array([[10, 10], [210, 10], [210, 45], [10, 45]], dtype=np.int32)
    return OCRLine(
        text=text,
        confidence=confidence,
        polygon=polygon,
        box=(10, 10, 200, 35),
        raw_text=text,
        engine="rapidocr",
        page=1,
    )


def _scored_group(text, confidence=0.92):
    group = TextGroup(
        group_id="T001",
        lines=[_line(text, confidence=confidence)],
        text=text,
        classification="speech",
        inside_balloon_like_region=True,
        source_engine="rapidocr",
    )
    score, reasons = score_group_ocr_quality(group)
    group.quality_score = score
    group.quality_reasons = reasons
    return group


class OCRQualityRegressionTests(unittest.TestCase):
    def test_repeated_word_typo_is_generic_repair(self):
        repaired, reason = repair_ocr_text("REALLY RFALIY")
        self.assertEqual(repaired, "REALLY REALLY")
        self.assertIn("adjacent_common_word_ocr_typo", reason)

    def test_compact_word_is_segmented_with_generic_vocabulary(self):
        repaired, reason = repair_ocr_text("HAVETHESEPLNKSCOME")
        self.assertEqual(repaired, "HAVE THESE PLANKS COME")
        self.assertIn("segment_compact_english_word", reason)

        segmented, confidence = segment_compact_english_word("HAVETHESEPLNKSCOME")
        self.assertEqual(segmented.upper(), "HAVE THESE PLANKS COME")
        self.assertGreaterEqual(confidence, 0.58)

    def test_short_ocr_typo_is_suspicious_and_better_candidate_wins(self):
        bad = _scored_group("BLT")
        good = _scored_group("BUT")
        self.assertTrue(group_needs_selective_fallback(bad))
        self.assertGreater(good.quality_score, bad.quality_score)
        self.assertEqual(repair_ocr_text("BLT")[0], "BUT")

    def test_planks_repair_is_not_phrase_specific(self):
        repaired, reason = repair_ocr_text("PLNKS")
        self.assertEqual(repaired, "PLANKS")
        self.assertIn("dictionary_edit_distance_repair", reason)

        suggestion, score = suggest_english_word("PLNKS")
        self.assertEqual(suggestion, "planks")
        self.assertGreaterEqual(score, 0.62)

    def test_no_runtime_ready_made_translation(self):
        translated = TranslatorNvidiaBatch._postprocess_translation(
            "I WILL DO BETTER FOR MOM",
            "SENTINEL_TRANSLATION_FROM_NVIDIA",
        )
        self.assertEqual(translated, "SENTINEL_TRANSLATION_FROM_NVIDIA")

    def test_mixed_english_translation_is_rejected_generically(self):
        valid, reason = validate_translation_text(
            "I WILL DO BETTER FOR MOM",
            "EU VOU FAZER MELHOR FOR MOM",
        )
        self.assertFalse(valid)
        self.assertIn("mixed_language_tokens", reason)

        valid, reason = validate_translation_text(
            "I WILL DO BETTER FOR MOM",
            "EU VOU FAZER MELHOR PELA MINHA MAE",
        )
        self.assertTrue(valid, reason)

    def test_spelling_repair_requires_engine_agreement_at_runtime(self):
        assessment = assess_ocr_repair(
            "PLNKS",
            "PLANKS",
            "dictionary_edit_distance_repair",
            confidence=0.94,
            source_engine="rapidocr",
        )
        self.assertFalse(assessment["accepted"])
        self.assertEqual(
            assessment["rejection_reason"],
            "dictionary_change_requires_engine_agreement",
        )

    def test_strict_visual_guard_rejects_old_broad_mask_behavior(self):
        original = np.full((120, 120, 3), 255, dtype=np.uint8)
        damaged = original.copy()
        damaged[20:100, 55:65] = 0
        allowed = np.zeros((120, 120), dtype=np.uint8)
        allowed[45:75, 45:75] = 255
        restored, summary = _enforce_visual_bounds(
            original,
            damaged,
            allowed,
            mask_metrics={"mask_to_text_area_ratio": 5.0},
        )
        self.assertFalse(summary["visual_validation_passed"])
        self.assertIn("mask_area_exceeds_text_area_limit", summary["reason"])
        self.assertIn("outside_component_too_large", summary["reason"])
        self.assertTrue(np.array_equal(restored, original))

    def test_large_graphic_line_does_not_merge_into_speech_group(self):
        lines = [
            OCRLine(
                text="SMALL",
                confidence=0.95,
                polygon=np.array([[100, 100], [220, 100], [220, 126], [100, 126]]),
                box=(100, 100, 120, 26),
                raw_text="SMALL",
                engine="rapidocr",
            ),
            OCRLine(
                text="TEXT",
                confidence=0.95,
                polygon=np.array([[110, 128], [210, 128], [210, 154], [110, 154]]),
                box=(110, 128, 100, 26),
                raw_text="TEXT",
                engine="rapidocr",
            ),
        ]
        group = TextGroup(group_id="T", lines=lines, text="SMALL TEXT")
        graphic = OCRLine(
            text="SFX",
            confidence=0.9,
            polygon=np.array([[0, 170], [320, 170], [320, 370], [0, 370]]),
            box=(0, 170, 320, 200),
            raw_text="SFX",
            engine="rapidocr",
        )
        self.assertFalse(_line_belongs_to_group(graphic, group))

    def test_lines_in_separate_white_regions_do_not_merge(self):
        image = np.zeros((220, 400, 3), dtype=np.uint8)
        cv2.ellipse(image, (200, 84), (105, 20), 0, 0, 360, (255, 255, 255), -1)
        cv2.ellipse(image, (200, 140), (105, 20), 0, 0, 360, (255, 255, 255), -1)
        first = OCRLine(
            text="FIRST THOUGHT",
            confidence=0.95,
            polygon=np.array([[130, 72], [270, 72], [270, 97], [130, 97]]),
            box=(130, 72, 140, 25),
            raw_text="FIRST THOUGHT",
            engine="rapidocr",
        )
        second = OCRLine(
            text="SECOND THOUGHT",
            confidence=0.95,
            polygon=np.array([[125, 128], [275, 128], [275, 153], [125, 153]]),
            box=(125, 128, 150, 25),
            raw_text="SECOND THOUGHT",
            engine="rapidocr",
        )
        _assign_visual_white_regions(image, [first, second])
        self.assertNotEqual(
            first.metadata.get("visual_white_region_id"),
            second.metadata.get("visual_white_region_id"),
        )
        self.assertEqual(len(_group_lines([first, second])), 2)

    def test_multisentence_group_splits_at_punctuated_visual_gaps(self):
        texts = ["FIRST", "THOUGHT.", "SECOND", "THOUGHT.", "THIRD", "THOUGHT."]
        y_positions = [10, 36, 78, 104, 146, 172]
        lines = []
        for text, y in zip(texts, y_positions):
            lines.append(
                OCRLine(
                    text=text,
                    confidence=0.95,
                    polygon=np.array([[50, y], [250, y], [250, y + 20], [50, y + 20]]),
                    box=(50, y, 200, 20),
                    raw_text=text,
                    engine="rapidocr",
                )
            )
        group = TextGroup(
            group_id="T",
            lines=lines,
            text=" ".join(texts),
        )
        split = _split_groups_at_sentence_boundaries([group])
        self.assertEqual(len(split), 3)
        self.assertEqual([item.text for item in split], [
            "FIRST THOUGHT.",
            "SECOND THOUGHT.",
            "THIRD THOUGHT.",
        ])

    def test_speed_lines_are_not_classified_as_white_balloon(self):
        image = np.full((240, 320, 3), 218, dtype=np.uint8)
        for offset in range(-120, 320, 18):
            cv2.line(image, (offset, 0), (offset + 180, 239), (90, 90, 90), 2)
        line = OCRLine(
            text="TEXT",
            confidence=0.95,
            polygon=np.array([[60, 80], [250, 80], [250, 140], [60, 140]]),
            box=(60, 80, 190, 60),
            raw_text="TEXT",
            engine="rapidocr",
        )
        group = TextGroup(
            group_id="T",
            lines=[line],
            text="TEXT",
            classification="speech",
            inside_balloon_like_region=True,
        )
        background_type, _ = _classify_background_region(image, group)
        self.assertIn(background_type, {"speed_lines", "textured_art"})

    def test_white_balloon_with_dark_styled_border_remains_white(self):
        image = np.full((240, 320, 3), 255, dtype=np.uint8)
        cv2.ellipse(image, (160, 120), (112, 72), 0, 0, 360, (0, 0, 0), 10)
        cv2.putText(
            image,
            "STORY TEXT",
            (82, 128),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.72,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )
        line = OCRLine(
            text="STORY TEXT",
            confidence=0.95,
            polygon=np.array([[78, 92], [242, 92], [242, 142], [78, 142]]),
            box=(78, 92, 164, 50),
            raw_text="STORY TEXT",
            engine="rapidocr",
        )
        group = TextGroup(
            group_id="T",
            lines=[line],
            text="STORY TEXT",
            classification="speech",
            inside_balloon_like_region=True,
        )
        background_type, metrics = _classify_background_region(image, group)
        self.assertEqual(background_type, "white_balloon")
        self.assertTrue(metrics["dominant_white_enclosure"])

    def test_small_stylized_white_balloon_is_not_treated_as_speed_lines(self):
        image = np.zeros((240, 320, 3), dtype=np.uint8)
        cv2.ellipse(image, (160, 120), (86, 68), 0, 0, 360, (255, 255, 255), -1)
        cv2.ellipse(image, (160, 120), (86, 68), 0, 0, 360, (15, 15, 15), 18)
        cv2.putText(
            image,
            "I CAN!",
            (112, 130),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.72,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )
        line = OCRLine(
            text="I CAN!",
            confidence=0.95,
            polygon=np.array([[100, 80], [220, 80], [220, 155], [100, 155]]),
            box=(100, 80, 120, 75),
            raw_text="I CAN!",
            engine="rapidocr",
        )
        group = TextGroup(
            group_id="T",
            lines=[line],
            text="I CAN!",
            classification="speech",
            inside_balloon_like_region=True,
        )
        background_type, metrics = _classify_background_region(image, group)
        self.assertEqual(background_type, "white_balloon")
        self.assertTrue(metrics["stylized_white_enclosure"])

    def test_low_quality_mixed_group_can_receive_regional_fallback(self):
        group = _scored_group("CAN TOO!")
        group.source_engine = "mixed"
        group.quality_score = 0.16
        group.quality_reasons = ["ignored_line_inside_text_region"]
        self.assertTrue(group_needs_selective_fallback(group))

    def test_incomplete_low_quality_group_requests_page_fallback(self):
        group = _scored_group("CAN TOO!")
        group.quality_score = 0.16
        group.cleanup_lines = [_line("I CAN DEFEAT")]
        with (
            patch.object(config, "OCR_ENGINE", "rapidocr"),
            patch.object(config, "OCR_HYBRID_FALLBACK", True),
            patch.object(config, "OCR_FALLBACK_ENGINE", "paddle"),
        ):
            self.assertEqual(
                _grouping_fallback_reason({"raw_lines": group.lines}, [group]),
                "incomplete_group_after_selective_fallback",
            )

    def test_open_white_narration_is_detected_without_balloon_flag(self):
        image = np.full((180, 640, 3), 255, dtype=np.uint8)
        cv2.putText(
            image,
            "I DID NOT CHANGE MYSELF",
            (90, 100),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )
        line = OCRLine(
            text="I DID NOT CHANGE MYSELF",
            confidence=0.95,
            polygon=np.array([[80, 70], [560, 70], [560, 110], [80, 110]]),
            box=(80, 70, 480, 40),
            raw_text="I DID NOT CHANGE MYSELF",
            engine="rapidocr",
        )
        group = TextGroup(
            group_id="T",
            lines=[line],
            text="I DID NOT CHANGE MYSELF",
            classification="unknown",
        )
        background_type, metrics = _classify_background_region(image, group)
        self.assertEqual(background_type, "narration_box")
        self.assertTrue(metrics["open_white_narration"])

    def test_short_text_embedded_in_saturated_art_is_preserved(self):
        group = _scored_group("5DOLLARS")
        group.classification = "speech"
        group.background_type = "textured_art"
        group.background_metrics = {
            "white_pixel_ratio": 0.0,
            "saturation_mean": 77.0,
        }
        _refine_classification_with_background(group)
        self.assertEqual(group.classification, "decorative")

    def test_large_white_patch_on_textured_art_is_rejected(self):
        original = np.full((180, 240, 3), 135, dtype=np.uint8)
        cleaned = original.copy()
        cleaned[45:135, 50:190] = 255
        mask = np.zeros((180, 240), dtype=np.uint8)
        mask[45:135, 50:190] = 255
        group = TextGroup(group_id="T", lines=[_line("TEXT")], text="TEXT")
        group.lines[0].box = (50, 45, 140, 90)
        metrics = _white_patch_artifact_metrics(
            original,
            cleaned,
            group,
            mask,
            "textured_art",
        )
        self.assertTrue(metrics["white_patch_rejected"])


if __name__ == "__main__":
    unittest.main()
