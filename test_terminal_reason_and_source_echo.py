"""Regressions for the three residual defects the real Villainess run exposed.

1. The render stage overwrote a proven ``proper_name_only`` terminal reason with its
   generic source-echo reason, so the accounting stopped recognising the name and the
   chapter stayed in review.
2. OCR case noise on a common word was treated as proper-name evidence, so the word
   was frozen untranslated instead of being retried.
3. A title used as a common noun ("the count's ...") survived untranslated because the
   validator only knew a fixed list of English words, none of which was that title.

Real chapter text appears only as fixtures; nothing in production keys off any word.
"""

import unittest
from unittest.mock import patch

import numpy as np

import config
from ocr_balloon import (
    OCRLine,
    TextGroup,
    PROPER_NAME_ONLY_REASON,
    _set_translation_terminal_state,
    _source_token_is_name_like,
    apply_group_translations,
    render_analyzed_image,
    validate_and_retry_translations,
    validate_translation_text,
)


class _Fake:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def translate_strict(self, text, previous_translation="", validation_reason="",
                         force=False, allow_proper_names=True, proper_names=None):
        self.calls.append({"allow_proper_names": allow_proper_names})
        return self.responses.pop(0) if self.responses else previous_translation


def _line(text, box=(10, 400, 200, 40)):
    x, y, w, h = box
    poly = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.int32)
    return OCRLine(text=text, confidence=0.92, polygon=poly, box=box,
                   raw_text=text, engine="rapidocr", page=1)


def _group(text, cls="speech", names=(), box=(10, 400, 200, 40)):
    grp = TextGroup(group_id="T", lines=[_line(text, box)], text=text, classification=cls,
                    inside_balloon_like_region=True, source_engine="rapidocr")
    grp.detected_proper_names = list(names)
    return grp


class TerminalReasonPrecedenceTests(unittest.TestCase):
    """Case 1: a proven reason survives a later generic overwrite."""

    def test_proper_name_only_survives_source_echo_overwrite(self):
        grp = _group("ARSKAN...")
        _set_translation_terminal_state(grp, "preserved_original",
                                        PROPER_NAME_ONLY_REASON, preserved_original=True)
        # The render stage preserves original pixels for an echo too, and tries to
        # relabel the group with its generic reason.
        _set_translation_terminal_state(grp, "preserved_original",
                                        "source_echo_preserved_original",
                                        preserved_original=True)
        self.assertEqual(grp.translation_final_reason, PROPER_NAME_ONLY_REASON)
        self.assertEqual(grp.translation_final_state, "preserved_original")
        self.assertEqual(grp.translation_quality_impact, "none")

    def test_generic_reason_is_kept_when_nothing_was_proven(self):
        grp = _group("Ah...")
        _set_translation_terminal_state(grp, "translated", "ok")
        _set_translation_terminal_state(grp, "preserved_original",
                                        "source_echo_preserved_original",
                                        preserved_original=True)
        self.assertEqual(grp.translation_final_reason, "source_echo_preserved_original")

    def test_review_state_still_wins_over_a_proven_reason(self):
        # The precedence must never hide a genuine failure behind a neutral reason.
        grp = _group("ARSKAN...")
        _set_translation_terminal_state(grp, "preserved_original",
                                        PROPER_NAME_ONLY_REASON, preserved_original=True)
        _set_translation_terminal_state(grp, "manual_review", "some_failure")
        self.assertEqual(grp.translation_final_state, "manual_review")
        self.assertEqual(grp.translation_quality_impact, "review_required")

    def test_render_keeps_proper_name_only_end_to_end(self):
        # Drive the real renderer: a name-only group whose translation echoes the
        # source must come out of render still labelled proper_name_only.
        image = np.full((900, 400, 3), 255, dtype=np.uint8)
        grp = _group("ARSKAN...", box=(40, 400, 160, 40))
        grp.translation = "ARSKAN..."
        grp.translation_candidate = "ARSKAN..."
        grp.translation_valid = True
        grp.sent_to_translation = True
        grp.redrawn = True
        _set_translation_terminal_state(grp, "preserved_original",
                                        PROPER_NAME_ONLY_REASON, preserved_original=True)
        render_analyzed_image(image, [], [], [grp], page_index=1)
        self.assertEqual(grp.translation_final_reason, PROPER_NAME_ONLY_REASON)
        self.assertTrue(grp.preserved_original)
        self.assertFalse(grp.redrawn)
        self.assertTrue(grp.visual_validation.get("render_preserved_source_echo"))


class MixedCaseNoiseTests(unittest.TestCase):
    """Case 2: OCR case noise is not proper-name evidence."""

    def test_mixed_case_token_is_not_name_like(self):
        for raw in ("STeP", "WaIT", "STOp", "PaNT", "HuFF", "PuLL", "SaVE"):
            self.assertFalse(_source_token_is_name_like({"raw": raw}), raw)

    def test_all_caps_token_is_not_name_like(self):
        self.assertFalse(_source_token_is_name_like({"raw": "STEP"}))

    def test_mixed_case_common_word_echo_is_rejected(self):
        valid, reason = validate_translation_text("STeP", "STeP", "speech", [])
        self.assertFalse(valid)
        self.assertEqual(reason, "candidate_equals_source")

    def test_mixed_case_word_is_translated_when_the_model_translates_it(self):
        grp = _group("STeP")
        apply_group_translations([grp], ["STeP"])
        self.assertFalse(grp.translation_valid)
        translator = _Fake("STeP", "PASSO")  # strict echoes, isolated translates
        with patch.object(config, "TRANSLATION_MAX_RETRIES", 1):
            validate_and_retry_translations([grp], translator)
        self.assertEqual(grp.translation, "PASSO")
        self.assertEqual(grp.translation_final_state, "translated")
        self.assertTrue(any(not c["allow_proper_names"] for c in translator.calls))

    def test_mixed_case_word_reaches_the_model_evidence_path_when_unresolved(self):
        # When even the forced retry echoes, the lone token is settled by the model,
        # exactly like any other lone token - not by its casing.
        grp = _group("STeP")
        apply_group_translations([grp], ["STeP"])
        translator = _Fake("STeP", "STeP")
        with patch.object(config, "TRANSLATION_MAX_RETRIES", 1):
            validate_and_retry_translations([grp], translator)
        self.assertTrue(any(not c["allow_proper_names"] for c in translator.calls))
        self.assertIn(grp.translation_final_state, {"preserved_original", "manual_review"})


if __name__ == "__main__":
    unittest.main()
