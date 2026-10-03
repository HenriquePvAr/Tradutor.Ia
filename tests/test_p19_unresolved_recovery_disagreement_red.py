"""P19 10F safety RED: rejected recovery evidence must eventually route to review.

The test intentionally remains RED until the later 10G safety patch.  It only
asserts the safety outcome; it does not require the recovery text to win.
"""
import _test_bootstrap  # noqa: F401

import unittest
from unittest.mock import patch

import numpy as np

import config
import ocr_balloon
from ocr_balloon import (
    TextGroup,
    apply_rapidocr_region_recovery,
    enforce_rapidocr_quality_gate,
    score_group_ocr_quality,
)
from ocr_engine import OCRLine


def _line(text, confidence, box=(20, 20, 300, 40)):
    x, y, w, h = box
    polygon = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.int32)
    return OCRLine(text=text, confidence=confidence, polygon=polygon, box=box,
                   raw_text=text, engine="rapidocr", page=19)


class _RejectedRecoveryEngine:
    def __init__(self, result):
        self.result = result

    def __call__(self, *args, **kwargs):
        return self

    def detect_lines(self, crop, **kwargs):
        return [self.result]


class P19UnresolvedRecoverySafetyRedTests(unittest.TestCase):
    def test_rejected_credible_variant_is_not_silently_accepted(self):
        primary_text = "INTIME,THE AWAKENEDBECAME HUMANITY'SHOPE,"
        recovery_text = "IN TimE, ThE AWAKENED BECAME HuMANITY'S HOPE,"
        primary_line = _line(primary_text, 0.90)
        primary = TextGroup(
            group_id="P19",
            region_id="REGION_002",
            lines=[primary_line],
            text=primary_text,
            classification="speech",
            inside_balloon_like_region=True,
            source_engine="rapidocr",
        )
        primary.quality_score = 0.55
        primary.quality_reasons = ["long_token_without_spaces"]
        candidate_line = _line(recovery_text, 0.91, (20, 20, 300, 40))
        candidate = TextGroup(
            group_id="P19-recovery",
            lines=[candidate_line],
            text=recovery_text,
            classification="speech",
            inside_balloon_like_region=True,
            source_engine="rapidocr",
        )
        candidate.quality_score = 0.58
        candidate.quality_reasons = ["cross_line_lexical_confidence_disagreement"]
        image = np.zeros((160, 420, 3), dtype=np.uint8)
        image[20:60, 20:320] = 255

        with (
            patch.object(config, "OCR_ENGINE", "rapidocr"),
            patch.object(config, "RAPIDOCR_REGION_RECOVERY", True),
            patch.object(ocr_balloon, "OCREngine", _RejectedRecoveryEngine(candidate_line)),
            patch.object(ocr_balloon, "_candidate_groups_for_fallback", return_value=[candidate]),
        ):
            lines, records = apply_rapidocr_region_recovery(
                image, [primary_line], [primary], "eng", 19
            )

        self.assertEqual([line.text for line in lines], [primary_text])
        observation = records[0]["rejected_recovery_observation"]
        self.assertFalse(observation["accepted"])
        self.assertNotEqual(
            observation["primary"]["word_signature"],
            observation["recovery"]["word_signature"],
        )
        self.assertEqual(observation["agreement"]["reason"], "insufficient_variant_agreement")

        enforce_rapidocr_quality_gate([primary])
        self.assertTrue(primary.manual_review_required)


if __name__ == "__main__":
    unittest.main()
