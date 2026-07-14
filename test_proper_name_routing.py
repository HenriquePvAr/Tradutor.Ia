"""Regressions for proper-name routing in translation.

Two production defects are covered here. A source auxiliary opening a tag question
was taken for a character name and adapted into a target-language name, and a
balloon holding only a character's name was reported as an untranslated speech,
which sent a whole chapter to review.

The real chapter texts appear only as fixtures. Nothing in production keys off any
specific word, page or chapter.
"""

import unittest
from unittest.mock import patch

import numpy as np

import config
from benchmark_pipeline import _translation_quality_accounting
from ocr_balloon import (
    OCRLine,
    TextGroup,
    apply_group_translations,
    detect_proper_name_spans,
    group_is_proper_name_only,
    group_proper_name_spans,
    validate_and_retry_translations,
    validate_translation_text,
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


def _group(text, classification="speech", names=()):
    group = TextGroup(
        group_id="T001",
        lines=[_line(text)],
        text=text,
        classification=classification,
        inside_balloon_like_region=True,
        source_engine="rapidocr",
    )
    group.detected_proper_names = list(names)
    return group


class _RecordingTranslator:
    """Fake translator that records the spans it was allowed to preserve."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def translate_strict(
        self,
        text,
        previous_translation="",
        validation_reason="",
        force=False,
        allow_proper_names=True,
        proper_names=None,
    ):
        self.calls.append(
            {
                "text": text,
                "allow_proper_names": allow_proper_names,
                "proper_names": list(proper_names or []),
            }
        )
        if self.responses:
            return self.responses.pop(0)
        return previous_translation


class GrammaticalFunctionDetectionTests(unittest.TestCase):
    """Phase 5/6: an auxiliary is never offered to the model as a name."""

    def test_auxiliary_opening_a_tag_question_is_not_a_name(self):
        self.assertEqual(detect_proper_name_spans("SHUT IT, WILL YOU?"), [])

    def test_auxiliary_followed_by_pronoun_is_not_a_name(self):
        self.assertEqual(detect_proper_name_spans("CAN YOU HEAR ME?"), [])

    def test_mixed_case_ocr_noise_does_not_create_a_name(self):
        # The real page was recognised with case noise: the auxiliary came back
        # capitalised, which used to read as name evidence.
        self.assertEqual(detect_proper_name_spans("SHUT IT, Will YoU?"), [])

    def test_interrogative_opening_token_is_not_a_name(self):
        self.assertEqual(detect_proper_name_spans("WILL THEY COME BACK?"), [])

    def test_verb_followed_by_pronoun_is_not_a_name(self):
        self.assertEqual(detect_proper_name_spans("SHUT IT DOWN."), [])

    def test_capitalized_only_because_it_opens_the_sentence(self):
        self.assertEqual(detect_proper_name_spans("SUDDENLY, THE LIGHTS DIED."), [])

    def test_sentence_without_any_name_yields_no_span(self):
        self.assertEqual(detect_proper_name_spans("THEY ARE ALL HUMAN."), [])


class ProperNameDetectionTests(unittest.TestCase):
    """Phase 5/6: a real name is detected in the positions that prove one."""

    def test_title_followed_by_name(self):
        self.assertEqual(detect_proper_name_spans("LADY BRELOFF, I--"), ["BRELOFF"])

    def test_standalone_direct_address(self):
        self.assertEqual(detect_proper_name_spans("ARSKAN..."), ["ARSKAN"])

    def test_chapter_consensus_name_is_kept_mid_sentence(self):
        self.assertEqual(
            detect_proper_name_spans("I SAW ARSKAN THERE", known_names=["ARSKAN"]),
            ["ARSKAN"],
        )

    def test_multi_token_known_name_is_matched_token_by_token(self):
        self.assertEqual(
            detect_proper_name_spans("ORION VALE", known_names=["ORION VALE"]),
            ["ORION", "VALE"],
        )

    def test_known_name_survives_next_to_a_pronoun(self):
        self.assertEqual(
            detect_proper_name_spans("ARSKAN, ARE YOU LISTENING?", known_names=["ARSKAN"]),
            ["ARSKAN"],
        )

    def test_known_name_in_vocative_keeps_only_the_name(self):
        self.assertEqual(
            detect_proper_name_spans("ARSKAN, WAIT!", known_names=["ARSKAN"]),
            ["ARSKAN"],
        )


class FailClosedAmbiguityTests(unittest.TestCase):
    """A bare vocative proves nothing without a lexicon, so nothing is claimed.

    'ARSKAN, WAIT!' and 'WAIT, ARSKAN' are the same shape; only knowing that one
    token is a verb separates them. Rather than guess, an unknown token in that
    position is left to be translated: a word translated in error is recoverable,
    an invented name is not.
    """

    def test_unknown_token_in_leading_vocative_is_not_claimed(self):
        self.assertEqual(detect_proper_name_spans("ARSKAN, WAIT!"), [])

    def test_unknown_token_in_trailing_vocative_is_not_claimed(self):
        self.assertEqual(detect_proper_name_spans("WAIT, ARSKAN"), [])

    def test_possessive_alone_is_not_claimed(self):
        self.assertEqual(detect_proper_name_spans("THE SHIP'S ENGINE DIED."), [])


class FalseNameControlTests(unittest.TestCase):
    """Phase 8: an all-caps token is not a name merely because it is all-caps."""

    def test_common_english_word_in_caps_is_not_a_name(self):
        self.assertEqual(detect_proper_name_spans("FRIEND, COME HERE."), [])

    def test_sfx_is_not_a_name(self):
        self.assertEqual(detect_proper_name_spans("BOOM!"), [])

    def test_short_token_is_not_a_name(self):
        self.assertEqual(detect_proper_name_spans("GO!"), [])

    def test_acronym_shorter_than_the_floor_is_not_a_standalone_name(self):
        self.assertEqual(detect_proper_name_spans("FBI"), [])

    def test_inflected_word_before_a_comma_is_not_a_vocative(self):
        self.assertEqual(detect_proper_name_spans("RUNNING, HE FELL."), [])

    def test_interjection_before_a_comma_is_not_a_vocative(self):
        self.assertEqual(detect_proper_name_spans("OKAY, ALL CLEAR!"), [])

    def test_name_with_ellipsis_is_still_a_name(self):
        self.assertEqual(detect_proper_name_spans("BRELOFF..."), ["BRELOFF"])

    def test_hyphenated_name_is_detected(self):
        self.assertEqual(detect_proper_name_spans("VAL-KIRA..."), ["VAL-KIRA"])

    def test_known_name_that_is_a_common_word_is_refused(self):
        # Fail-closed: the vocabulary wins over the consensus list, so a common
        # word can never be frozen in the source language as a "name".
        self.assertEqual(
            detect_proper_name_spans("SHUT IT, WILL YOU?", known_names=["WILL"]),
            [],
        )


class ProperNameOnlyTerminalStateTests(unittest.TestCase):
    """Phase 7: a name-only balloon is preserved, not reported as untranslated."""

    def test_name_only_group_is_detected(self):
        self.assertTrue(group_is_proper_name_only(_group("ARSKAN...")))

    def test_sentence_group_is_not_name_only(self):
        self.assertFalse(group_is_proper_name_only(_group("ARSKAN, WAIT!")))

    def test_sfx_group_is_never_name_only(self):
        self.assertFalse(group_is_proper_name_only(_group("KRAAA", classification="sfx")))

    def test_name_only_group_is_quality_neutral(self):
        group = _group("ARSKAN...")
        apply_group_translations([group], ["ARSKAN..."])

        translator = _RecordingTranslator("ARSKAN...")
        with patch.object(config, "TRANSLATION_MAX_RETRIES", 2):
            records = validate_and_retry_translations([group], translator)

        self.assertEqual(records, [], "a name-only group must not be retried")
        self.assertEqual(translator.calls, [], "the model must not be called again")
        self.assertEqual(group.translation_final_state, "preserved_original")
        self.assertEqual(group.translation_final_reason, "proper_name_only")
        self.assertEqual(group.translation_quality_impact, "none")
        self.assertTrue(group.translation_valid)
        self.assertFalse(group.manual_review_required)
        self.assertTrue(group.preserved_original)
        self.assertEqual(group.translation, group.text)

    def test_untranslated_sentence_still_reaches_manual_review(self):
        # The neutral state must not become a hiding place for a real failure.
        group = _group("THE SIGNAL IS CLEAR.")
        apply_group_translations([group], [group.text])

        with patch.object(config, "TRANSLATION_MAX_RETRIES", 1):
            validate_and_retry_translations([group], _RecordingTranslator(group.text))

        self.assertEqual(group.translation_final_state, "manual_review")
        self.assertTrue(group.manual_review_required)


class ProperNameAccountingTests(unittest.TestCase):
    """Phase 7/15: a preserved name must not fail the chapter's quality gate."""

    @staticmethod
    def _states(items):
        return [
            {
                "index": 1,
                "status": "processed",
                "output_path": "",
                "image_path": "",
                "timings": {},
                "debug_data": {
                    "items": items,
                    "selective_ocr_fallbacks": [],
                    "classification_counts": {},
                },
            }
        ]

    @staticmethod
    def _name_only_item():
        return {
            "id": "BALAO_1",
            "classification": "speech",
            "translation_final_state": "preserved_original",
            "translation_final_reason": "proper_name_only",
            "translation_valid": True,
            "preserved_original": True,
            "redrawn": False,
            "sent_to_nvidia": True,
            "clean_text": "ARSKAN...",
            "translation": "ARSKAN...",
            "translation_candidate": "ARSKAN...",
            "bounding_box": [10, 10, 90, 30],
        }

    def test_preserved_name_does_not_require_review(self):
        accounting = _translation_quality_accounting(self._states([self._name_only_item()]))

        self.assertEqual(accounting["proper_name_preserved"], 1)
        self.assertEqual(accounting["candidate_equals_source"], 0)
        self.assertEqual(accounting["invalid_candidate"], 0)
        self.assertEqual(accounting["manual_review"], 0)
        self.assertEqual(accounting["source_language_residual"], 0)
        self.assertTrue(accounting["accounting_closed"])
        self.assertFalse(accounting["requires_review"])
        self.assertTrue(accounting["quality_passed"])

    def test_untranslated_sentence_still_requires_review(self):
        item = self._name_only_item()
        item["translation_final_reason"] = "untranslated_source_after_retries"
        item["translation_final_state"] = "manual_review"
        item["translation_valid"] = False
        item["clean_text"] = "THE SIGNAL IS CLEAR."
        item["translation"] = "THE SIGNAL IS CLEAR."
        item["translation_candidate"] = "THE SIGNAL IS CLEAR."

        accounting = _translation_quality_accounting(self._states([item]))

        self.assertEqual(accounting["proper_name_preserved"], 0)
        self.assertEqual(accounting["candidate_equals_source"], 1)
        self.assertTrue(accounting["requires_review"])
        self.assertFalse(accounting["quality_passed"])


class StrictRetryRoutingTests(unittest.TestCase):
    """Phase 4/15: the strict retry is told exactly which spans it may keep."""

    def test_retry_of_an_auxiliary_sentence_forbids_every_name(self):
        group = _group("SHUT IT, Will YoU?")
        apply_group_translations([group], ["Cala a boca, Will!"])
        self.assertFalse(group.translation_valid)

        translator = _RecordingTranslator("Cala a boca, ta bom?")
        with patch.object(config, "TRANSLATION_MAX_RETRIES", 1):
            validate_and_retry_translations([group], translator)

        self.assertTrue(translator.calls)
        self.assertEqual(translator.calls[0]["proper_names"], [])
        self.assertTrue(group.translation_valid, group.translation_validation_reason)
        self.assertEqual(group.translation, "Cala a boca, ta bom?")
        self.assertEqual(group.translation_final_state, "translated")

    def test_retry_of_a_named_sentence_passes_the_detected_span(self):
        group = _group("ARSKAN, ARE YOU LISTENING?", names=["ARSKAN"])
        apply_group_translations([group], ["ARSKAN, ARE YOU LISTENING?"])

        translator = _RecordingTranslator("ARSKAN, ESTA ME OUVINDO?")
        with patch.object(config, "TRANSLATION_MAX_RETRIES", 1):
            validate_and_retry_translations([group], translator)

        self.assertTrue(translator.calls)
        self.assertEqual(translator.calls[0]["proper_names"], ["ARSKAN"])
        self.assertTrue(group.translation_valid, group.translation_validation_reason)

    def test_translated_away_name_is_rejected_by_the_validator(self):
        # The exact production failure: the model adapted the span it was told to
        # copy into a target-language name.
        valid, reason = validate_translation_text(
            "ARSKAN, WAIT!",
            "GUILHERME, ESPERE!",
            "speech",
            [],
            required_name_spans=["ARSKAN"],
        )
        self.assertFalse(valid)
        self.assertTrue(reason.startswith("proper_name_altered"), reason)

    def test_preserved_name_passes_the_validator(self):
        valid, reason = validate_translation_text(
            "ARSKAN, WAIT!",
            "ARSKAN, ESPERE!",
            "speech",
            ["ARSKAN"],
            required_name_spans=["ARSKAN"],
        )
        self.assertTrue(valid, reason)


class StrictPromptTests(unittest.TestCase):
    """The instruction sent to the model must name the spans, or forbid them all."""

    def _translator(self):
        return TranslatorNvidiaBatch(api_key="test-key")

    def test_prompt_without_spans_demands_a_full_translation(self):
        instruction = self._translator()._proper_name_instruction([], True)
        self.assertIn("NENHUMA palavra", instruction)

    def test_prompt_lists_only_the_detected_spans(self):
        instruction = self._translator()._proper_name_instruction(["ARSKAN"], True)
        self.assertIn("ARSKAN", instruction)
        self.assertIn("Nenhum outro token", instruction)

    def test_isolated_retry_forbids_names_even_if_spans_are_offered(self):
        instruction = self._translator()._proper_name_instruction(["ARSKAN"], False)
        self.assertIn("NENHUMA palavra", instruction)
        self.assertNotIn("ARSKAN", instruction)


if __name__ == "__main__":
    unittest.main()
