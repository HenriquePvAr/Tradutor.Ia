"""#84F41 - forensic fixes for four of the five review-required regions from
#84F40 (P21, P24, P29, P31).  P26 is diagnosed here but deliberately left
unfixed this pass - see the note at the bottom.

The #84F40 job (``dbe4328a``) shipped 35 correct pages except five review
regions.  Reading the persisted ``quality_report.json`` for that job found one
shared root cause for P21/P24/P29 and a second, related cause for P31:

* **P21 / P24 / P29 - undersized generic vocabulary.**  RapidOCR
  occasionally drops the space between two words on the same balloon line
  (``COULD WIELD`` -> ``COLLDWIELDINHERITED``, ``MY MOM HAD`` ->
  ``MYMOMHAD``).  The fused run should have scored as suspicious, but the
  generic English word list backing both the dictionary-suggestion and
  compact-word-segmentation checks only had 126 entries - missing ordinary
  words like "had", "so", "check", "attributes", "trial" and "during".  With
  no recognizable pieces, the segmenter returned nothing, the group scored a
  clean ``quality_score`` of 1.0 with zero ``quality_reasons``, and the
  fused/corrupted run sailed through untranslated (P21/P24) or the region
  never even got sent to translation (P29).

  The fix only adds the missing generic words to
  ``ocr_engine.COMMON_ENGLISH_WORD_SCORES``.  No page, phrase, translation or
  chapter is hardcoded - it is the same kind of chapter-agnostic vocabulary
  entry the file already carries dozens of.  Any resulting repair still goes
  through the existing ``assess_ocr_repair`` safety gate unchanged: an
  exact (space-only) segmentation is accepted, a segmentation that also
  changes a letter is rejected without a second engine's agreement, exactly
  as before.  ("aspect" and "receive" were deliberately *not* added: each,
  paired with the pre-existing word "spent", revives the segmenter's own
  single-letter ("a"/"i") filler-word scoring bias and wrongly re-flags an
  unrelated, already-passing fixture - ``test_high_story_score_compact_
  apostrophe_sentence_is_routed_to_translation`` in
  test_story_review_to_render_closure.py, which OCRs the same "an
  aspect"/"receive" shape as page 30's real text.  "attributes" and "check"
  alone are enough to flag the P29 group; "wield"/"inherited"/"magical"/
  "memories"/"echoes"/"visit"/"dream"/"realm" alone are enough for P21's
  tested group without "receive".)

* **P31 - fused verb-mood trigger.**  The fused ``DODURINGTHETRIALWILL``
  defeated the plain ``\\bWHAT\\s+YOU\\s+DO\\b`` regex that
  ``_repair_narrow_ptbr_verb_mood`` needs to fix the "voce fazer" bad mood
  (the compact-word segmenter's filler-word bias above keeps it from cleanly
  splitting a run this long, so growing the vocabulary alone does not
  recover it) - so a real, already-existing grammar repair never got a
  chance to run.  The fix adds a second, narrow match to that one function:
  the same literal "what you do" prefix glued directly to more letters,
  explicitly excluding "does"/"doing"/"done" so a genuinely different verb
  is never mistaken for the fused case.

* **P26 - not fixed this pass.**  The post-render OCR check re-reads the
  drawn Portuguese text and flags any token that looks like residual
  English.  The approved P26 candidate read (folded) ``"E SO ENTRAR NO
  REINO DOS SONHOS..."``; the checker's own ``forgiven_ocr_noise_tokens``
  mechanism already exists precisely to excuse the accent-folded PT-BR "SO"
  (pinned by ``NoiseForgivenessPolicyTests`` in
  test_physical_source_residual_trust.py) and does compute "SO" as
  forgivable here.  But forgiveness also requires the overall observed-vs-
  expected shape similarity to clear its 0.90 bar, and the checker's *own*
  re-OCR of the rendered crop introduced two extra misreads of its own
  (``NO`` -> ``RNO``, ``MONSTROS`` -> ``SMONSTROS``) that pushed the
  similarity to 0.8667 - just under the bar - so the render was discarded in
  favor of the untranslated English source.  An earlier attempt to fix this
  by adding "SO" to ``PORTUGUESE_MARKERS`` was reverted: it bypasses that
  provenance-aware mechanism entirely and breaks all three
  ``NoiseForgivenessPolicyTests`` (in particular
  ``test_expected_source_word_is_never_forgiven_as_noise``, which requires a
  genuine source-owned "SO" to still be caught).  Closing P26 needs a real
  second independent post-render OCR pass (the mission's own "segunda
  leitura" pattern) so the similarity check is not at the mercy of one
  read's own noise - that is a larger, riskier change than this pass's
  budget covers safely, so P26 stays REVIEW rather than ship a threshold or
  marker change that reopens a pinned safety contract.

Every fixture below is the real OCR text from job ``dbe4328a`` (see
``output/shadow_slave_chapter_1_5/dbe4328a-b300-44ac-95fc-609e5a73b7d6``), or
a synthetic word chosen only to prove the mechanism.  No page, chapter or
sentence is special-cased in production code.
"""

import _test_bootstrap  # noqa: F401

import unittest

import numpy as np

from ocr_balloon import (
    TextGroup,
    _repair_narrow_ptbr_verb_mood,
    _score_group_quality,
    group_needs_selective_fallback,
)
from ocr_engine import (
    OCRLine,
    assess_ocr_repair,
    repair_ocr_text,
    segment_compact_english_word,
    suggest_english_word,
)


def _boxed_line(text, box, confidence=0.98):
    x, y, width, height = box
    polygon = np.array(
        [[x, y], [x + width, y], [x + width, y + height], [x, y + height]],
        dtype=np.int32,
    )
    return OCRLine(
        text=text,
        confidence=confidence,
        polygon=polygon,
        box=box,
        raw_text=text,
        engine="rapidocr",
        page=1,
    )


class FusedOcrRunDetectionTests(unittest.TestCase):
    """A. OCR corrupted/fused partially -> must not score as clean."""

    def _score(self, text):
        group = TextGroup(
            group_id="BALAO_1",
            lines=[_boxed_line(text, (80, 80, 520, 90))],
            text=text,
            classification="speech",
            inside_balloon_like_region=True,
            source_engine="rapidocr",
        )
        _score_group_quality([group])
        return group

    def test_p21_fused_could_wield_no_longer_scores_clean(self):
        # Real OCR text for P21:BALAO_1 (job dbe4328a). Previously scored
        # quality_score=1.0 with zero reasons and rendered untranslated.
        group = self._score(
            "COLLDWIELDINHERITED MAGICALMEMORIESOR ECHOESINTHEIRFIRST "
            "VISITTOTHEDREAM REALM."
        )
        self.assertLess(group.quality_score, 1.0)
        self.assertTrue(group.quality_reasons)
        self.assertTrue(group_needs_selective_fallback(group))

    def test_p24_fused_my_mom_had_no_longer_scores_clean(self):
        # Real OCR text for P24:BALAO_2 (job dbe4328a).
        group = self._score("MYMOMHAD APOETICSOUL YOU SEE.")
        self.assertLess(group.quality_score, 1.0)
        self.assertTrue(group.quality_reasons)
        self.assertTrue(group_needs_selective_fallback(group))

    def test_p31_fused_what_you_do_now_repairs_verb_mood(self):
        # Real OCR text for P31:BALAO_3 (job dbe4328a).  The fused
        # "DODURINGTHETRIALWILL" defeated the old plain \bWHAT YOU DO\b
        # regex, so the already-existing FAZER->FIZER grammar repair never
        # fired for this region.
        repaired = _repair_narrow_ptbr_verb_mood(
            "WHAT YOU DODURINGTHETRIALWILL DETERMINETHEREWARDS "
            "ANDPOWERSTHATWILL BEWAITINGFORYOU, IFYOUSUCCEED.",
            "O QUE VOCÊ FAZER DURANTE A PROVA DETERMINARÁ AS RECOMPENSAS.",
            "speech",
        )
        self.assertIn("FIZER", repaired.upper())
        self.assertNotIn("VOCÊ FAZER", repaired.upper())

    def test_fused_what_you_do_does_not_match_a_different_verb(self):
        # "WHAT YOU DOES/DOING/DONE" must never be mistaken for the fused
        # "WHAT YOU DO<more text>" case - only the literal DO prefix matches.
        for verb in ("DOES", "DOING", "DONE"):
            repaired = _repair_narrow_ptbr_verb_mood(
                f"WHAT YOU {verb} NOT MATTER HERE.",
                "O QUE VOCÊ FAZER AQUI.",
                "speech",
            )
            self.assertIn("VOCÊ FAZER", repaired.upper())

    def test_p29_check_and_attributes_recognized_as_real_words(self):
        # Real OCR text for P29:BALAO_1.  "CHECK" and "ATTRIBUTES" are
        # ordinary words the old 126-word list did not know, which is why a
        # bogus CHECK->HECK dictionary "repair" was even attempted, and why
        # the group scored low enough to be skipped rather than sent to
        # translation.  "CHECK" is now a recognized whole word (no bogus
        # suggestion), and "YOURATTRIBUTES" alone is still enough to flag
        # the group - "aspect" is deliberately not in the vocabulary (see
        # module docstring).
        self.assertEqual(segment_compact_english_word("CHECK"), ("", 0.0))
        self.assertEqual(suggest_english_word("CHECK"), ("", 0.0))
        group = self._score("CHECK YOURATTRIBUTES ANDASPECT.")
        self.assertLess(group.quality_score, 1.0)
        self.assertTrue(group.quality_reasons)


class RiskySegmentationStillGatedTests(unittest.TestCase):
    """Expanding the vocabulary must not let a spelling-changing guess ship
    without a second engine's agreement - the existing safety gate must
    still catch it, exactly as it did before this change."""

    def test_fuzzy_segmentation_without_agreement_is_still_rejected(self):
        # The segmenter's own filler-word scoring quirk turns "ANDASPECT"
        # into "AND A SPENT" (a real letter change, not just a space
        # insertion) both before and after this change - it must still be
        # rejected without a second engine's agreement.
        original = "ANDASPECT."
        repaired, reason = repair_ocr_text(original)
        self.assertEqual(repaired, "AND A SPENT.")
        decision = assess_ocr_repair(original, repaired, reason)
        self.assertFalse(decision["accepted"])
        self.assertEqual(
            decision["rejection_reason"],
            "segmentation_spelling_change_requires_engine_agreement",
        )

    def test_exact_space_only_segmentation_is_still_accepted(self):
        # A segmentation that only inserts spaces (no letter changes) must
        # keep shipping without needing a second engine's agreement.
        original = "ABOUTTHEMAS"
        repaired, reason = repair_ocr_text(original)
        self.assertEqual(repaired, "ABOUT THEM AS")
        decision = assess_ocr_repair(original, repaired, reason)
        self.assertTrue(decision["accepted"])
        self.assertEqual(decision["edit_distance"], 0)


class UnaffectedBehaviorTests(unittest.TestCase):
    """F. SFX / branding must not start entering this detection path."""

    def test_sfx_token_untouched_by_new_vocabulary(self):
        group = TextGroup(
            group_id="SFX_1",
            lines=[_boxed_line("STAGGER", (80, 80, 200, 90))],
            text="STAGGER",
            classification="sfx",
            inside_balloon_like_region=False,
            source_engine="rapidocr",
        )
        _score_group_quality([group])
        self.assertFalse(group_needs_selective_fallback(group))


if __name__ == "__main__":
    unittest.main()
