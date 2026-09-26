"""Mechanism B: recover ordinary story text dropped as weak_unknown_text.

Real artifact this pins down (Absolute Regression - Episode 104, page 45):
"PERFECTLY FINE." (OCR conf 0.977) was lettered over artwork with no balloon, so
region detection returned ``unknown`` and it was dropped as ``weak_unknown_text``
before translation - while the adjacent dialogue balloon translated fine.

The recovery is deliberately conservative and combined: high confidence + a
sentence-final clause + >=2 real dictionary words + no sound-effect-shaped token +
horizontal reading layout + spatial adjacency to a real story region.  Every
listed sound effect fails it, and an isolated candidate with no story neighbour
is never promoted.  Nothing here touches the network.
"""
from __future__ import annotations

import _test_bootstrap  # noqa: F401

import unittest

import numpy as np

import ocr_balloon
from ocr_balloon import OCRLine, TextGroup


def _line(text, box, confidence):
    x, y, w, h = box
    polygon = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.int32)
    return OCRLine(text=text, confidence=confidence, polygon=polygon, box=(x, y, w, h),
                   raw_text=text, engine="rapidocr", page=1)


def _weak_unknown(text, box, *, confidence=0.95, group_id="CAND",
                  background_type="textured_art"):
    group = TextGroup(group_id=group_id, lines=[_line(text, box, confidence)], text=text,
                      classification="unknown", source_engine="rapidocr")
    group.ignored = True
    group.ignore_reason = "weak_unknown_text"
    group.background_type = background_type
    group.angle_degrees = 0.0
    group.alignment_score = 1.0
    return group


def _story_anchor(text, box, *, classification="narration", group_id="ANCHOR"):
    group = TextGroup(group_id=group_id, lines=[_line(text, box, 0.95)], text=text,
                      classification=classification, source_engine="rapidocr")
    group.ignored = False
    group.angle_degrees = 0.0
    group.alignment_score = 1.0
    return group


# Anchor sits just below the candidate, like page 45's dialogue balloon.
ANCHOR_BOX = (177, 403, 480, 184)
CANDIDATE_BOX = (251, 351, 333, 36)

SHORT_SPEECH_POSITIVES = [
    "PERFECTLY FINE.", "NOT YET.", "I SEE.", "OF COURSE.",
    "VERY WELL.", "COME HERE.", "WAIT HERE.", "THANK YOU.",
]
SFX_NEGATIVES = [
    "THUMP", "FLINCH", "FWHISH", "PRESS", "SCUFF", "WAVE", "PUNCH", "SCRRCH",
    "BAM", "BOOM", "WHOOSH", "TAP", "SLAM", "CRACK",
]


def _promote(candidate, anchors):
    groups = [candidate, *anchors]
    ocr_balloon._promote_unknown_story_on_adjacency(groups)
    return candidate


class AdjacencyPromotionPositives(unittest.TestCase):
    def test_page45_like_candidate_is_promoted_to_speech(self):
        cand = _weak_unknown("PERFECTLY FINE.", CANDIDATE_BOX)
        anchor = _story_anchor("I MERELY WISH TO KEEP YOU COMPANY.", ANCHOR_BOX)
        _promote(cand, [anchor])
        self.assertEqual(cand.classification, "speech")
        self.assertFalse(cand.ignored)
        self.assertEqual(cand.ignore_reason, "")
        self.assertEqual(cand.classification_evidence.get("unknown_story_promotion"),
                         "UNKNOWN_STORY_PROMOTION_ACCEPTED")
        # It now has story authority and would be sent to translation.
        self.assertTrue(ocr_balloon._should_translate_group(cand))

    def test_short_speech_positives_are_all_promoted(self):
        anchor = _story_anchor("I MERELY WISH TO KEEP YOU COMPANY.", ANCHOR_BOX)
        for text in SHORT_SPEECH_POSITIVES:
            cand = _weak_unknown(text, CANDIDATE_BOX)
            _promote(cand, [_story_anchor("I MERELY WISH TO KEEP YOU COMPANY.", ANCHOR_BOX)])
            self.assertFalse(cand.ignored, f"{text!r} should be promoted")
            self.assertEqual(cand.classification, "speech", f"{text!r}")

    def test_textured_art_background_does_not_block_promotion(self):
        cand = _weak_unknown("PERFECTLY FINE.", CANDIDATE_BOX, background_type="textured_art")
        _promote(cand, [_story_anchor("A REAL DIALOGUE LINE HERE.", ANCHOR_BOX)])
        self.assertTrue(ocr_balloon._should_translate_group(cand))


class SfxNegativesAreNeverPromoted(unittest.TestCase):
    def test_no_sfx_is_promoted_even_adjacent_to_story(self):
        promoted = []
        for text in SFX_NEGATIVES:
            cand = _weak_unknown(text, CANDIDATE_BOX)
            _promote(cand, [_story_anchor("A REAL DIALOGUE LINE HERE.", ANCHOR_BOX)])
            if not cand.ignored:
                promoted.append(text)
        self.assertEqual(promoted, [], f"SFX false positives: {promoted}")

    def test_repeated_onomatopoeia_pair_is_not_promoted(self):
        # Two-word SFX must not sneak past the word-count signal.
        for text in ("TAP TAP!", "BAM BAM!", "BOOM BOOM."):
            cand = _weak_unknown(text, CANDIDATE_BOX)
            _promote(cand, [_story_anchor("A REAL DIALOGUE LINE HERE.", ANCHOR_BOX)])
            self.assertTrue(cand.ignored, f"{text!r} must stay ignored")


class NegativeContexts(unittest.TestCase):
    def test_isolated_candidate_without_adjacency_is_not_promoted(self):
        cand = _weak_unknown("PERFECTLY FINE.", CANDIDATE_BOX)
        # A far-away anchor at the bottom of a tall page: no shared reading flow.
        far = _story_anchor("SOMEWHERE ELSE ENTIRELY.", (177, 3000, 480, 120))
        _promote(cand, [far])
        self.assertTrue(cand.ignored)
        self.assertEqual(cand.classification_evidence.get("unknown_story_promotion_reason"),
                         "no_adjacent_story_region")

    def test_no_story_anchor_on_page_means_no_promotion(self):
        cand = _weak_unknown("PERFECTLY FINE.", CANDIDATE_BOX)
        _promote(cand, [])  # nothing else on the page
        self.assertTrue(cand.ignored)

    def test_decorative_label_is_not_eligible(self):
        # Only unknown/weak_unknown_text is in scope; a decorative label is not.
        cand = _weak_unknown("PERFECTLY FINE.", CANDIDATE_BOX)
        cand.classification = "decorative"
        cand.ignore_reason = "decorative_text"
        _promote(cand, [_story_anchor("A REAL DIALOGUE LINE HERE.", ANCHOR_BOX)])
        self.assertTrue(cand.ignored)

    def test_title_without_sentence_shape_is_not_promoted(self):
        cand = _weak_unknown("THE BEGINNING", CANDIDATE_BOX)  # no final punctuation
        _promote(cand, [_story_anchor("A REAL DIALOGUE LINE HERE.", ANCHOR_BOX)])
        self.assertTrue(cand.ignored)


if __name__ == "__main__":
    unittest.main()
