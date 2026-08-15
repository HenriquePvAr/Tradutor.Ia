"""Validator precision: preserved stutters, proven entities, OCR-unreadable sources.

Three false verdicts are covered here. A correctly translated line whose speech
disfluency prefix survived was reported as source-language residual; a token the
chapter itself proves is a name was reported as an untranslated source; and a
source the recogniser never read was counted as ordinary untranslated dialogue.

Real chapter texts appear only as fixtures. Nothing in production keys off any
specific word, page or chapter: every verdict below is reached from evidence the
chapter or the recogniser produced.
"""

import _test_bootstrap  # noqa: F401

import unittest

import numpy as np

from ocr_balloon import (
    OCR_UNINTELLIGIBLE_SOURCE_REASON,
    PROPER_NAME_ONLY_REASON,
    OCRLine,
    TextGroup,
    apply_group_translations,
    score_group_ocr_quality,
    validate_and_retry_translations,
    validate_translation_text,
)


class _NoRetryTranslator:
    """No ``translate_strict``: every verdict below is a first-pass verdict."""

    stats = {}


def _line(text, confidence=0.92):
    polygon = np.array([[10, 10], [210, 10], [210, 45], [10, 45]], dtype=np.int32)
    return OCRLine(
        text=text,
        confidence=confidence,
        polygon=polygon,
        box=(10, 10, 200, 35),
        raw_text=text,
        engine="rapidocr",
    )


def _group(text, group_id="T001"):
    group = TextGroup(
        group_id=group_id,
        lines=[_line(text)],
        text=text,
        classification="speech",
        inside_balloon_like_region=True,
        source_engine="rapidocr",
    )
    score, reasons = score_group_ocr_quality(group)
    group.quality_score = score
    group.quality_reasons = reasons
    return group


def _terminal(pairs):
    """Run the production quality path over a whole chapter of source/candidate."""
    groups = [
        _group(source, group_id=f"T{index:03d}")
        for index, (source, _) in enumerate(pairs)
    ]
    apply_group_translations(groups, [candidate for _, candidate in pairs])
    validate_and_retry_translations(groups, _NoRetryTranslator())
    return groups


class StutterPrefixPrecisionTests(unittest.TestCase):
    """A speech disfluency prefix is not untranslated source language."""

    def test_preserved_stutter_over_translated_body_is_accepted(self):
        for source, candidate in (
            ("G-GET AWAY FROM US!", "G-SAIA DAQUI!"),
            ("TH-THANK YOU!", "OB-OBRIGADO!"),
            ("I-I CAN'T.", "E-EU NÃO POSSO."),
            ("K-KILL!", "K-MATAR!"),
        ):
            with self.subTest(candidate=candidate):
                valid, reason = validate_translation_text(source, candidate, "speech")
                self.assertTrue(valid, f"{candidate}: {reason}")

    def test_stutter_prefix_never_excuses_an_untranslated_body(self):
        # Only the disfluency fragment is discounted. The lexical body must still
        # be translated, whether the stutter came from the source or was invented.
        for source, candidate in (
            ("GET AWAY!", "G-GET AWAY!"),
            ("STOP!", "S-STOP!"),
            ("S-STOP!", "S-STOP!"),
            ("Sh-She'S COMING!", "Sh-She'S VINDO!"),
            ("G-GET AWAY FROM US!", "G-GET AWAY FROM US!"),
        ):
            with self.subTest(candidate=candidate):
                valid, _ = validate_translation_text(source, candidate, "speech")
                self.assertFalse(valid, candidate)

    def test_translated_stutter_region_reaches_the_trusted_terminal_state(self):
        group = _terminal([("G-GET AWAY FROM US!", "G-SAIA DAQUI!")])[0]
        self.assertEqual(group.translation_final_state, "translated")
        self.assertEqual(group.translation, "G-SAIA DAQUI!")
        self.assertFalse(group.manual_review_required)


class ChapterProvenEntityTests(unittest.TestCase):
    """Only the chapter's own evidence may excuse a preserved source token."""

    def test_entity_preserved_inside_a_translated_region_proves_the_name(self):
        groups = _terminal([
            ("PAEHYEOK...", "PAEHYEOK..."),
            (
                "WHY ARE YOU TWO STILL WITH PAEHYEOK?",
                "POR QUE VOCÊS DOIS AINDA ESTÃO COM O PAEHYEOK?",
            ),
        ])
        self.assertEqual(groups[0].translation_final_state, "preserved_original")
        self.assertEqual(groups[0].translation_final_reason, PROPER_NAME_ONLY_REASON)
        self.assertFalse(groups[0].manual_review_required)
        self.assertEqual(groups[1].translation_final_state, "translated")

    def test_unproven_capitalized_token_stays_in_review(self):
        # Same shape, same casing, no chapter evidence: capitalisation proves
        # nothing, so the region is held.
        group = _terminal([("PAEHYEOK...", "PAEHYEOK...")])[0]
        self.assertEqual(group.translation_final_state, "manual_review")
        self.assertTrue(group.manual_review_required)

    def test_proven_entity_plus_ordinary_english_stays_in_review(self):
        for source in ("HYEON IS HERE.", "HUNTER HYEON IS STRONG."):
            with self.subTest(source=source):
                groups = _terminal([
                    (source, source),
                    ("THERE YOU ARE, MISS HYEON.", "AÍ ESTÁ VOCÊ, SRTA. HYEON."),
                ])
                self.assertEqual(groups[0].translation_final_state, "manual_review")
                self.assertTrue(groups[0].manual_review_required)

    def test_proven_entity_inside_translated_context_is_trusted(self):
        groups = _terminal([
            ("HYEON IS HERE.", "HYEON ESTÁ AQUI."),
            ("THERE YOU ARE, MISS HYEON.", "AÍ ESTÁ VOCÊ, SRTA. HYEON."),
        ])
        self.assertEqual(groups[0].translation_final_state, "translated")

    def test_ordinary_english_source_equal_still_fails_closed(self):
        for source in ("THANK YOU!", "I WILL GO NOW.", "THE HUNTER IS STRONG."):
            with self.subTest(source=source):
                group = _terminal([(source, source)])[0]
                self.assertEqual(group.translation_final_state, "manual_review")
                self.assertTrue(group.manual_review_required)
                self.assertNotEqual(
                    group.translation_final_reason,
                    OCR_UNINTELLIGIBLE_SOURCE_REASON,
                )


class UnreadableSourceTaxonomyTests(unittest.TestCase):
    """An unreadable source is a review item, but not untranslated dialogue."""

    def test_internally_mixed_case_token_is_reported_as_unreadable(self):
        group = _terminal([("iHon", "iHon")])[0]
        self.assertEqual(group.translation_final_state, "manual_review")
        self.assertTrue(group.manual_review_required)
        self.assertEqual(
            group.translation_final_reason,
            OCR_UNINTELLIGIBLE_SOURCE_REASON,
        )

    def test_title_glued_to_a_name_is_reported_as_unreadable_not_a_name(self):
        # The recogniser lost the space: the token holds an ordinary source word
        # plus a name, so it is neither translatable dialogue nor an entity.
        groups = _terminal([
            ("...HUNTERHYEON.", "...HUNTERHYEON."),
            ("THERE YOU ARE, MISS HYEON.", "AÍ ESTÁ VOCÊ, SRTA. HYEON."),
        ])
        self.assertEqual(groups[0].translation_final_state, "manual_review")
        self.assertEqual(
            groups[0].translation_final_reason,
            OCR_UNINTELLIGIBLE_SOURCE_REASON,
        )

    def test_proven_entity_is_not_reported_as_unreadable(self):
        groups = _terminal([
            ("PAEHYEOK...", "PAEHYEOK..."),
            (
                "WHY ARE YOU TWO STILL WITH PAEHYEOK?",
                "POR QUE VOCÊS DOIS AINDA ESTÃO COM O PAEHYEOK?",
            ),
        ])
        self.assertNotEqual(
            groups[0].translation_final_reason,
            OCR_UNINTELLIGIBLE_SOURCE_REASON,
        )

    def test_title_cased_name_is_not_reported_as_unreadable(self):
        group = _terminal([("Hyeon", "Hyeon")])[0]
        self.assertNotEqual(
            group.translation_final_reason,
            OCR_UNINTELLIGIBLE_SOURCE_REASON,
        )


if __name__ == "__main__":
    unittest.main()
