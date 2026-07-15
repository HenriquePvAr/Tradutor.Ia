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

import numpy as np

from ocr_balloon import (
    OCRLine,
    TextGroup,
    PROPER_NAME_ONLY_REASON,
    _set_translation_terminal_state,
    render_analyzed_image,
)


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


if __name__ == "__main__":
    unittest.main()
