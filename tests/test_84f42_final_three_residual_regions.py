"""#84F42 - forensic fixes for the final three review-required regions from
#84F41 (P21, P26, P29).

#84F41 fixed P24 and P31 via generic OCR-vocabulary additions and a grammar
regex extension; three regions still leaked into the render or never reached
translation.  This module diagnoses and closes all three with generic,
class-level fixes - no page, phrase, chapter or translation is hardcoded.

* **P21 - fused OCR run never gets a second, independent read.**  The compact-
  word DP segmenter (``segment_compact_english_word``) already finds the
  right structure for a fused run like ``COLLDWIELDINHERITED`` -> "could
  wield inherited" - but the runtime safety gate (``assess_ocr_repair``)
  correctly refuses to ship a segmentation that also changes a letter (here
  "COLLD" -> "COULD") without a second, independent read agreeing.  That
  second read already exists - ``apply_rapidocr_region_recovery`` re-reads
  the same pixels once more, with RapidOCR alone, at the scale the
  recogniser prefers, never touching Paddle - but it was only requested for
  a group whose ``quality_score`` fell under a fixed numeric floor
  (``RAPIDOCR_RECOVERY_MIN_QUALITY_SCORE``). A group carrying
  ``compact_word_segmentation_candidate`` can legitimately score just above
  that floor (0.44 in the real job) and so never got the second read at all.
  The fix adds this reason to ``RAPIDOCR_CORRUPTION_REASONS`` - the existing,
  reason-based (not score-based) set that already forces a second read for
  its siblings ("long_token_without_spaces", "long_consonant_run", ...). No
  global threshold moves, no dictionary word is invented, and the retry
  still fails closed exactly as it does for every other reason in that set:
  a re-read that reproduces the same corruption, errors, or finds nothing
  changes nothing.

* **P26 - the post-render checker validated the wrong crop.** The checker
  already runs two comparisons from one OCR read of one padded crop: (a) is
  there leftover source text anywhere near the region (needs the wide crop,
  since a bigger source font can extend past the newly drawn text), and
  (b) does the rendered text match the expected translation (only ever true
  of the box the renderer actually drew into). Padding the crop with the
  *source*'s bounding box to serve concern (a) let concern (b)'s shape
  comparison pick up art or a neighbouring line as a stray extra glyph
  ("NO" -> "RNO", "MONSTROS" -> "SMONSTROS" in the real job), which dragged a
  correct render's similarity just under the acceptance bar and threw away a
  legitimately translated page in favour of the untranslated English source.
  The fix adds one more, independent OCR read scoped only to the box the
  renderer actually drew the target text into, and uses that read - not the
  wide crop - for the expected/source shape comparison. The wide crop keeps
  doing exactly what it did before for residual-token search; nothing about
  when a real leftover source glyph gets caught changes, and a failed or
  empty tight read falls back to the previous (wide-crop) text, never to a
  smaller amount of scrutiny.

* **P29 - a recoverable fused run never reached translation.** ``CHECK
  YOURATTRIBUTES ANDASPECT.`` was flagged ``compact_word_segmentation_
  candidate`` (real ordinary words, just glued together) but
  ``ocr_suspicious_but_translatable`` - the routing function that lets a
  suspicious-but-workable read still reach the translator - only recognised
  whole tokens already in the dialogue-word dictionary. "CHECK" matched,
  but "YOURATTRIBUTES" and "ANDASPECT" never can as single tokens, so the
  group counted as having only one ordinary word and was held back before
  ever reaching translation. The fix reuses the same compact-word segmenter
  already trusted elsewhere: a token that fails the whole-word lookup but
  segments into known dialogue words (score >= 0.58, the same bar the repair
  pipeline itself uses) now contributes its recovered words as evidence.
  This only changes *routing* - whether the group is worth handing to the
  translator - never whether its eventual candidate is trusted; the fidelity,
  terminology and residual gates downstream are untouched.

Every fixture below is either the real OCR text from the referenced job or a
synthetic case chosen only to prove the mechanism generically. No page,
chapter or sentence is special-cased in production code.
"""

import _test_bootstrap  # noqa: F401

import unittest
from unittest.mock import patch

import numpy as np

import config
import ocr_balloon
from ocr_balloon import (
    RAPIDOCR_CORRUPTION_REASONS,
    TextGroup,
    _ordinary_dialogue_words,
    _post_render_source_text_check,
    _score_group_quality,
    ocr_suspicious_but_translatable,
    rapidocr_region_decision,
)
from ocr_engine import OCRLine
from test_physical_source_residual_trust import (
    LINE_BOX,
    SOURCE_TEXT,
    TRANSLATION,
    _dark_panel,
    _line,
    _speech_group,
    _write_light_text,
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


class P21FusedRunGetsSecondReadTests(unittest.TestCase):
    """A. A fused run that scores above the numeric floor still asks for a
    second, independent RapidOCR read instead of shipping unresolved."""

    def test_compact_word_segmentation_candidate_is_a_corruption_reason(self):
        self.assertIn(
            "compact_word_segmentation_candidate", RAPIDOCR_CORRUPTION_REASONS
        )

    def test_p21_fused_could_wield_requests_retry_above_the_score_floor(self):
        text = (
            "COLLDWIELDINHERITED MAGICAL MEMORIES OR ECHOESINTHEIRFIRST "
            "VISIT TO THE DREAM REALM."
        )
        group = TextGroup(
            group_id="BALAO_1",
            lines=[_boxed_line(text, (80, 80, 520, 90))],
            text=text,
            classification="speech",
            inside_balloon_like_region=True,
            source_engine="rapidocr",
        )
        _score_group_quality([group])
        self.assertIn(
            "compact_word_segmentation_candidate", group.quality_reasons
        )
        # The real job (dbe4328a/ccd6de6c, page 21) scored this exact group
        # 0.44 - above the retry floor of 0.35 - which is the gap this fix
        # closes, not the already-covered low-score path. This synthetic
        # single-line reconstruction scores lower on its own, so the score is
        # pinned to the real job's value to exercise that exact gap.
        group.quality_score = 0.44
        self.assertGreater(
            group.quality_score, config.RAPIDOCR_RECOVERY_MIN_QUALITY_SCORE
        )
        with patch.object(config, "OCR_ENGINE", "rapidocr"), patch.object(
            config, "RAPIDOCR_REGION_RECOVERY", True
        ):
            self.assertEqual(rapidocr_region_decision(group), "retry")

    def test_clean_text_above_the_floor_is_still_accepted(self):
        # The new reason must not turn every merely-imperfect group into a
        # retry candidate - only one that actually carries the segmentation
        # signal.
        group = TextGroup(
            group_id="BALAO_2",
            lines=[_boxed_line("HEY! WAIT FOR ME!", (80, 80, 300, 60))],
            text="HEY! WAIT FOR ME!",
            classification="speech",
            inside_balloon_like_region=True,
            source_engine="rapidocr",
        )
        _score_group_quality([group])
        self.assertNotIn(
            "compact_word_segmentation_candidate", group.quality_reasons
        )
        with patch.object(config, "OCR_ENGINE", "rapidocr"), patch.object(
            config, "RAPIDOCR_REGION_RECOVERY", True
        ):
            self.assertEqual(rapidocr_region_decision(group), "accept")


class P21NoInventedTokenTests(unittest.TestCase):
    """B. Without a second read agreeing, the segmentation still stays
    review-required rather than shipping an invented spelling change."""

    def test_letter_changing_segmentation_still_needs_agreement(self):
        from ocr_engine import assess_ocr_repair, repair_ocr_text

        original = "COLLDWIELDINHERITED"
        repaired, reason = repair_ocr_text(original)
        self.assertEqual(repaired, "COULD WIELD INHERITED")
        decision = assess_ocr_repair(original, repaired, reason)
        self.assertFalse(decision["accepted"])
        self.assertEqual(
            decision["rejection_reason"],
            "segmentation_spelling_change_requires_engine_agreement",
        )


class P26TightCropMatchTests(unittest.TestCase):
    """The expected-translation shape comparison must read the box the
    renderer actually drew into, not the wider residual-search crop."""

    def _case(self, *, wide_text, tight_text):
        original = _dark_panel()
        x, y, w, h = LINE_BOX
        _write_light_text(original, SOURCE_TEXT, (x + 6, y + h - 10), scale=0.8)
        group = _speech_group([_line(SOURCE_TEXT, LINE_BOX)], SOURCE_TEXT, TRANSLATION)
        mask = np.zeros(original.shape[:2], dtype=np.uint8)
        mask[y - 2 : y + h + 2, x - 2 : x + w + 2] = 255
        rendered = original.copy()
        rendered[mask > 0] = (14, 14, 14)
        _write_light_text(rendered, TRANSLATION, (x + 4, y + h - 12), scale=0.66)

        class _StubLine:
            def __init__(self, text):
                self.text = text

        draw_box = tuple(group.draw_box)

        class _CropAwareEngine:
            def __init__(self, *_args, **_kwargs):
                pass

            def _detect_with_rapidocr(self, crop):
                # The tight, per-draw-box crop is exactly ``draw_box``'s own
                # size; anything larger is the padded/union crop this test
                # simulates picking up extra noise from.
                if crop.shape[1] <= draw_box[2] + 1 and crop.shape[0] <= draw_box[3] + 1:
                    return [_StubLine(tight_text)] if tight_text else []
                return [_StubLine(wide_text)] if wide_text else []

        with patch.object(ocr_balloon, "OCREngine", _CropAwareEngine):
            return _post_render_source_text_check(
                rendered, group, page_index=2, original_bgr=original, cleanup_mask=mask
            )

    def test_wide_crop_noise_no_longer_sinks_a_correct_render(self):
        # The real job's shape: the wide crop's own re-OCR glued a stray
        # leading glyph onto the translation ("NO" -> "RNO") that alone was
        # enough to drop expected_similarity under the acceptance bar; the
        # tight, draw-box-only read has no such noise.
        result = self._case(
            wide_text="EU PERDI RO CONTROLE",
            tight_text=TRANSLATION.replace(" ", ""),
        )
        self.assertTrue(result["passed"], result.get("reason"))

    def test_tight_read_failure_falls_back_to_the_wide_crop_safely(self):
        # An empty/failed tight read must not silently relax scrutiny - it
        # falls back to the previous (wide-crop) behaviour, same as before
        # this change existed.
        result = self._case(wide_text=SOURCE_TEXT.replace(" ", ""), tight_text="")
        self.assertFalse(result["passed"])


class P29RecoverableGroupReachesTranslationTests(unittest.TestCase):
    """C. A story-shaped, recoverable-but-flagged group can now enter the
    translation attempt path; it is still never auto-approved."""

    def test_fused_ordinary_words_are_recognised_via_segmentation(self):
        words = _ordinary_dialogue_words("CHECK YOURATTRIBUTES ANDASPECT.")
        self.assertGreaterEqual(len(words), 2)
        self.assertIn("CHECK", words)
        self.assertIn("ATTRIBUTES", words)

    def test_p29_group_now_routes_to_translation(self):
        text = "CHECK YOURATTRIBUTES ANDASPECT."
        group = TextGroup(
            group_id="BALAO_1",
            lines=[_boxed_line(text, (80, 80, 400, 90))],
            text=text,
            classification="speech",
            inside_balloon_like_region=True,
            source_engine="rapidocr",
        )
        _score_group_quality([group])
        self.assertLess(group.quality_score, 1.0)
        self.assertTrue(ocr_suspicious_but_translatable(group))

    def test_routing_is_not_auto_approval(self):
        # Reaching the translator is not the same as being trusted: the
        # source stays flagged, and this function only ever decides routing.
        text = "CHECK YOURATTRIBUTES ANDASPECT."
        group = TextGroup(
            group_id="BALAO_1",
            lines=[_boxed_line(text, (80, 80, 400, 90))],
            text=text,
            classification="speech",
            inside_balloon_like_region=True,
            source_engine="rapidocr",
        )
        _score_group_quality([group])
        ocr_suspicious_but_translatable(group)
        self.assertTrue(group.quality_reasons)
        self.assertLess(group.quality_score, 1.0)

    def test_sfx_and_branding_do_not_enter_this_path(self):
        group = TextGroup(
            group_id="SFX_1",
            lines=[_boxed_line("STAGGER", (80, 80, 200, 90))],
            text="STAGGER",
            classification="sfx",
            inside_balloon_like_region=False,
            source_engine="rapidocr",
        )
        _score_group_quality([group])
        self.assertFalse(ocr_suspicious_but_translatable(group))


class ManualReviewNeverSilentlyDisappearsTests(unittest.TestCase):
    """GLOBAL - a flagged group's review signal survives the routing change."""

    def test_p29_group_still_carries_its_quality_reasons(self):
        text = "CHECK YOURATTRIBUTES ANDASPECT."
        group = TextGroup(
            group_id="BALAO_1",
            lines=[_boxed_line(text, (80, 80, 400, 90))],
            text=text,
            classification="speech",
            inside_balloon_like_region=True,
            source_engine="rapidocr",
        )
        _score_group_quality([group])
        ocr_suspicious_but_translatable(group)
        self.assertIn(
            "compact_word_segmentation_candidate", group.quality_reasons
        )


if __name__ == "__main__":
    unittest.main()
