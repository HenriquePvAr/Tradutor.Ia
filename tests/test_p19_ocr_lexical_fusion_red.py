"""P19 RED characterization: RapidOCR can fuse adjacent lexical words.

This file intentionally captures the current failure without prescribing a
production repair.  The historical fixture is allowed here; production must
remain page/chapter agnostic.
"""
import _test_bootstrap  # noqa: F401

import pytest
import unittest

from ocr_engine import clean_ocr_text, repair_ocr_text


class P19OcrLexicalFusionRedTests(unittest.TestCase):
    @pytest.mark.xfail(
        strict=True,
        reason="P19 final OCR lexical-boundary recovery remains unresolved",
    )
    def test_historical_fused_read_must_not_cross_word_boundary(self):
        raw = "INTIME,THE AWAKENEDBECAME HUMANITY'SHOPE,"
        normalized = clean_ocr_text(raw)
        repaired, _reason = repair_ocr_text(normalized)

        # RED contract: the OCR/source boundary should preserve the distinct
        # lexical words represented by the page lettering.  Current behavior
        # keeps the fused run, so this assertion documents the gap.
        self.assertNotIn("INTIME", repaired.replace("IN TIME", ""))

    def test_true_single_token_control_remains_intact(self):
        self.assertEqual(clean_ocr_text("INTIME"), "INTIME")

    def test_punctuation_control_does_not_request_extra_spaces(self):
        self.assertEqual(clean_ocr_text("WORD, NEXT."), "WORD, NEXT.")


if __name__ == "__main__":
    unittest.main()
