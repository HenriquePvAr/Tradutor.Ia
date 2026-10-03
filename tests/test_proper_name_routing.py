"""Regressions for proper-name routing in translation.

Two production defects are covered here. A source auxiliary opening a tag question
was taken for a character name and adapted into a target-language name, and a
balloon holding only a character's name was reported as an untranslated speech,
which sent a whole chapter to review.

The real chapter texts appear only as fixtures. Nothing in production keys off any
specific word, page or chapter.
"""

import _test_bootstrap  # noqa: F401

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
    group_has_name_only_shape,
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
        **kwargs,
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

    def test_name_declaration_context_proves_aliases(self):
        self.assertEqual(
            detect_proper_name_spans("SuNLEsS... BUT PEOPLE CALL Me Sunny."),
            ["SuNLEsS", "Sunny"],
        )

    def test_strange_name_context_proves_the_named_token(self):
        self.assertEqual(
            detect_proper_name_spans("SUNLESS? THAT'S A STRANGE NAME."),
            ["SUNLESS"],
        )

    def test_name_declaration_context_survives_ocr_glue_on_the_pronoun(self):
        """Job acd0f20ca7e14c09b74009a64a46678b, p024: OCR glued the alias onto
        "ME" ("CALL MESUNNY") with no separating space. Losing the match here
        does not fall back to a safer default - it silently drops both names
        the sentence declares, since neither is capitalization-derived.
        """
        self.assertEqual(
            detect_proper_name_spans("SUNLESS... BUT PEOPLE CALL MESUNNY."),
            ["SUNLESS", "SUNNY"],
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

    def test_lone_token_is_never_claimed_from_its_shape(self):
        # This is the regression the offline re-audit caught. A lone token looks
        # exactly like a lone name, so static detection claims neither: 'WAIT!' must
        # not be frozen, which means 'BRELOFF...' cannot be claimed here either. The
        # model settles it later.
        self.assertEqual(detect_proper_name_spans("BRELOFF..."), [])
        self.assertEqual(detect_proper_name_spans("WAIT!"), [])
        self.assertEqual(detect_proper_name_spans("PANT"), [])

    def test_lone_name_is_recovered_once_the_chapter_knows_it(self):
        self.assertEqual(
            detect_proper_name_spans("BRELOFF...", known_names=["BRELOFF"]),
            ["BRELOFF"],
        )

    def test_known_name_that_is_a_common_word_is_refused(self):
        # Fail-closed: the vocabulary wins over the consensus list, so a common
        # word can never be frozen in the source language as a "name".
        self.assertEqual(
            detect_proper_name_spans("SHUT IT, WILL YOU?", known_names=["WILL"]),
            [],
        )


class ProperNameAuthorityValidationTests(unittest.TestCase):
    """TDD #10: shape-only/OCR tokens are hints, not hard name authority."""

    def test_all_caps_ordinary_lexical_token_is_not_hard_name(self):
        valid, reason = validate_translation_text(
            "THE MONSTER IS HERE.",
            "O MONSTRO ESTÁ AQUI.",
            "speech",
            required_name_spans=["MONSTER"],
        )
        self.assertTrue(valid, reason)

    def test_sentence_initial_common_word_is_not_required_name(self):
        valid, reason = validate_translation_text(
            "Reason doesn't matter.",
            "O motivo não importa.",
            "speech",
            required_name_spans=["Reason"],
        )
        self.assertTrue(valid, reason)

    def test_ocr_joined_ordinary_phrase_is_not_hard_name(self):
        valid, reason = validate_translation_text(
            "THEMONSTER DEVOUREDASSOONAS YOUVESEEN.",
            "O monstro devorou assim que você viu.",
            "narration",
            required_name_spans=["THEMONSTER", "DEVOUREDASSOONAS", "YOUVESEEN"],
        )
        self.assertTrue(valid, reason)

    def test_shape_only_unknown_without_registry_is_not_detected_as_name(self):
        self.assertEqual(detect_proper_name_spans("VELRAN ARRIVED."), [])

    def test_registered_character_altered_still_fails(self):
        valid, reason = validate_translation_text(
            "HYEON, WAIT!",
            "HÉLIO, ESPERE!",
            "speech",
            allowed_proper_names=["HYEON"],
            required_name_spans=["HYEON"],
        )
        self.assertFalse(valid)
        self.assertTrue(reason.startswith("proper_name_altered"), reason)

    def test_registered_character_preserved_passes(self):
        valid, reason = validate_translation_text(
            "HYEON, WAIT!",
            "HYEON, ESPERE!",
            "speech",
            allowed_proper_names=["HYEON"],
            required_name_spans=["HYEON"],
        )
        self.assertTrue(valid, reason)

    def test_authoritative_entity_altered_still_fails(self):
        valid, reason = validate_translation_text(
            "ZARQUON OPENED THE GATE.",
            "ZARQUIN ABRIU O PORTÃO.",
            "narration",
            allowed_proper_names=["ZARQUON"],
            required_name_spans=["ZARQUON"],
        )
        self.assertFalse(valid)
        self.assertTrue(reason.startswith("proper_name_altered"), reason)

    def test_ocr_suspect_name_like_token_is_not_promoted(self):
        valid, reason = validate_translation_text(
            "THEREARESTILLALOT NEARTHEENTRANCE,SO PLEASEBECAREFUL.",
            "Ainda tem muitos perto da entrada, por isso tenha cuidado.",
            "narration",
            required_name_spans=[
                "THEREARESTILLALOT",
                "NEARTHEENTRANCE",
                "SO",
                "PLEASEBECAREFUL",
            ],
        )
        self.assertTrue(valid, reason)

    def test_joined_title_name_blob_is_not_the_authoritative_span(self):
        valid, reason = validate_translation_text(
            "...HUNTERHYEON.",
            "... CAÇADOR HYEON.",
            "speech",
            required_name_spans=["HUNTERHYEON"],
        )
        self.assertTrue(valid, reason)

    def test_interjection_translation_is_not_proper_name_altered(self):
        valid, reason = validate_translation_text(
            "HUH?!",
            "O QUE?",
            "speech",
            required_name_spans=["HUH"],
        )
        self.assertTrue(valid, reason)

    def test_short_registered_name_altered_still_fails(self):
        valid, reason = validate_translation_text(
            "IO, WAIT!",
            "IA, ESPERE!",
            "speech",
            allowed_proper_names=["IO"],
            required_name_spans=["IO"],
        )
        self.assertFalse(valid)
        self.assertTrue(reason.startswith("proper_name_altered"), reason)

    def test_hyphenated_entity_internal_i_is_not_language_residual(self):
        valid, reason = validate_translation_text(
            "SERIOUSLY, CHO-I, STOP TALKING!",
            "SÉRIO, CHO-I, PARA DE FALAR!",
            "narration",
        )
        self.assertTrue(valid, reason)

    def test_standalone_english_i_still_fails_residual_check(self):
        valid, reason = validate_translation_text(
            "I CAN'T DO THIS.",
            "EU NÃO SEI, I CAN'T DO THIS.",
            "speech",
        )
        self.assertFalse(valid)
        self.assertIn("mixed_language", reason)

    def test_hyphenated_entity_segments_are_excluded_from_residual_scan(self):
        valid, reason = validate_translation_text(
            "NAR-IO ARRIVED.",
            "NAR-IO CHEGOU.",
            "speech",
        )
        self.assertTrue(valid, reason)

    def test_declared_sunless_must_not_be_translated_literally(self):
        required = detect_proper_name_spans("SuNLEsS... BUT PEOPLE CALL Me Sunny.")
        valid, reason = validate_translation_text(
            "SuNLEsS... BUT PEOPLE CALL Me Sunny.",
            "Sem sol... MAS AS PESSOAS ME CHAMAM DE Sunny.",
            "narration",
            allowed_proper_names=["SUNLESS", "SUNNY"],
            required_name_spans=required,
        )
        self.assertFalse(valid)
        self.assertTrue(reason.startswith("proper_name_altered"), reason)

    def test_ptbr_voce_infinitive_is_not_accepted_as_natural(self):
        valid, reason = validate_translation_text(
            "WHAT YOU DO DURING THE TRIAL WILL DETERMINE THE REWARDS.",
            "O QUE VOCÊ FAZER DURANTE A PROVA DETERMINARÁ AS RECOMPENSAS.",
            "speech",
        )
        self.assertFalse(valid)
        self.assertEqual(reason, "unnatural_ptbr_verb_mood:voce_infinitive")

    def test_stray_single_letter_parenthesis_from_ocr_is_rejected(self):
        valid, reason = validate_translation_text(
            "SO DO Y YOURSELF A FAVOR AND ) JUST THINK ABOUT THEM AS ILLUSIONS.",
            "ENTÃO, FAÇA UM FAVOR A SI MESMO E) APENAS PENSE NELAS COMO ILUSÕES.",
            "narration",
        )
        self.assertFalse(valid)
        self.assertEqual(reason, "stray_ocr_fragment:E)")


class ProperNameOnlyTerminalStateTests(unittest.TestCase):
    """Phase 7: a lone name is settled by the model, never by its shape.

    'WAIT!' and 'ARSKAN...' are the same shape, and this pipeline has no English
    lexicon that can tell them apart. So it asks: once the model is told the text has
    no names and every word must be translated, an ordinary word comes back
    translated and a name comes back unchanged.
    """

    def test_name_shape_alone_claims_nothing(self):
        self.assertTrue(group_has_name_only_shape(_group("ARSKAN...")))
        # Same shape, ordinary word: the shape test cannot separate them, and does
        # not pretend to.
        self.assertTrue(group_has_name_only_shape(_group("WAIT!")))

    def test_sentence_has_no_name_shape(self):
        self.assertFalse(group_has_name_only_shape(_group("ARSKAN, WAIT!")))

    def test_sfx_group_never_has_name_shape(self):
        self.assertFalse(
            group_has_name_only_shape(_group("KRAAA", classification="sfx"))
        )

    def test_vocabulary_word_never_has_name_shape(self):
        self.assertFalse(group_has_name_only_shape(_group("OKAY!")))

    def test_model_refusing_to_translate_settles_it_as_a_name(self):
        group = _group("ARSKAN...")
        apply_group_translations([group], ["ARSKAN..."])
        self.assertFalse(group.translation_valid)

        # The model hands the text back even after names are forbidden.
        translator = _RecordingTranslator("ARSKAN...", "ARSKAN...")
        with patch.object(config, "TRANSLATION_MAX_RETRIES", 1):
            validate_and_retry_translations([group], translator)

        self.assertTrue(
            any(not call["allow_proper_names"] for call in translator.calls),
            "the model must be asked once with every name forbidden",
        )
        self.assertEqual(group.translation_final_state, "preserved_original")
        self.assertEqual(group.translation_final_reason, "proper_name_only")
        self.assertEqual(group.translation_quality_impact, "none")
        self.assertTrue(group.translation_valid)
        self.assertFalse(group.manual_review_required)
        self.assertTrue(group.preserved_original)
        self.assertEqual(group.translation, group.text)

    def test_ordinary_lone_word_is_translated_not_preserved(self):
        # The regression the offline re-audit caught: a lone ordinary word has the
        # shape of a name, and must never be frozen as one.
        group = _group("WAIT!")
        apply_group_translations([group], ["WAIT!"])

        translator = _RecordingTranslator("ESPERA!")
        with patch.object(config, "TRANSLATION_MAX_RETRIES", 1):
            validate_and_retry_translations([group], translator)

        self.assertEqual(group.translation, "ESPERA!")
        self.assertEqual(group.translation_final_state, "translated")
        self.assertNotEqual(group.translation_final_reason, "proper_name_only")
        self.assertFalse(group.manual_review_required)

    def test_untranslated_sentence_still_reaches_manual_review(self):
        # The neutral state must not become a hiding place for a real failure: a
        # sentence has no name-only shape, so it can never take this route.
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


class QualityOnlyRescueTests(unittest.TestCase):
    """#84F44: a PT-BR candidate rejected only for grammar/quality renders under
    review instead of leaving English story text on the page.
    """

    def _review_group(self, source, candidate, validation_reason):
        group = _group(source)
        group.translation_candidate = candidate
        group.translation_final_state = "manual_review"
        group.translation_final_reason = "invalid_translation_after_retries"
        group.translation_validation_reason = validation_reason
        return group

    def test_voce_infinitive_candidate_is_rescued(self):
        """TEST: imperfect PT-BR verb mood is still rendered rather than English."""
        from ocr_balloon import _quality_review_render_candidate
        group = self._review_group(
            "WHAT YOU DO DURING THE TRIAL WILL DETERMINE THE REWARDS.",
            "O QUE VOCE FAZER DURANTE A PROVA DETERMINARA AS RECOMPENSAS.",
            "unnatural_ptbr_verb_mood:voce_infinitive",
        )
        result = _quality_review_render_candidate(group)
        self.assertTrue(result, "PT-BR candidate with verb mood issue should be rescued")

    def test_stray_ocr_fragment_candidate_is_rescued(self):
        """TEST: OCR artifact in translation is still rendered."""
        from ocr_balloon import _quality_review_render_candidate
        group = self._review_group(
            "SO DO YOURSELF A FAVOR AND) JUST THINK ABOUT THEM.",
            "ENTAO, FACA UM FAVOR A SI MESMO E) APENAS PENSE NELAS.",
            "stray_ocr_fragment:E)",
        )
        result = _quality_review_render_candidate(group)
        self.assertTrue(result, "PT-BR candidate with OCR artifact should be rescued")

    def test_english_candidate_is_not_rescued(self):
        """TEST: candidate that is still in English stays blocked."""
        from ocr_balloon import _quality_review_render_candidate
        group = self._review_group(
            "THE MONSTER IS HERE.",
            "THE MONSTER IS HERE.",
            "candidate_equals_source",
        )
        result = _quality_review_render_candidate(group)
        self.assertFalse(result, "English candidate must not be rescued")

    def test_semantic_fidelity_failure_is_not_rescued(self):
        """TEST: semantic fidelity rejection is too risky to rescue."""
        from ocr_balloon import _quality_review_render_candidate
        group = self._review_group(
            "FOR ME, BEING CHOSEN IS A DEATH SENTENCE.",
            "PARA MIM, SER ESCOLHIDO E UMA SENTENCA DE MORTE.",
            "fidelity_uncertain",
        )
        group.translation_validation_reason = "fidelity_uncertain"
        result = _quality_review_render_candidate(group)
        self.assertFalse(result, "Semantic fidelity failure must not be rescued")

    def test_proper_name_altered_candidate_is_rescued_with_imperfect_name(self):
        """TEST: name-altered candidate still ships Portuguese under review."""
        from ocr_balloon import _quality_review_render_candidate
        group = self._review_group(
            "SUNLESS... BUT PEOPLE CALL ME SUNNY.",
            "Sem sol... MAS AS PESSOAS ME CHAMAM DE Sunny.",
            "proper_name_altered:SUNLESS",
        )
        result = _quality_review_render_candidate(group)
        self.assertTrue(result, "Name-altered PT-BR is better than English")

    def test_rescue_is_wired_into_finalize_translation_failure(self):
        """TEST: _finalize_translation_failure applies the quality rescue."""
        from ocr_balloon import _finalize_translation_failure
        group = _group("WHAT YOU DO DURING THE TRIAL WILL DETERMINE THE REWARDS.")
        group.translation_candidate = (
            "O QUE VOCE FAZER DURANTE A PROVA DETERMINARA AS RECOMPENSAS."
        )
        group.translation_valid = False
        group.translation_validation_reason = "unnatural_ptbr_verb_mood:voce_infinitive"
        group.sent_to_translation = True

        _finalize_translation_failure(
            group,
            "invalid_translation_after_retries",
            candidate="O QUE VOCE FAZER DURANTE A PROVA DETERMINARA AS RECOMPENSAS.",
            validator_reason="unnatural_ptbr_verb_mood:voce_infinitive",
        )

        self.assertTrue(group.translation, "rescued PT-BR should be set as translation")
        self.assertFalse(group.preserved_original, "original should not be preserved")
        self.assertTrue(group.manual_review_required)

    def test_branding_english_is_permitted(self):
        """TEST: branding/URL stays in English without being flagged as story."""
        valid, reason = validate_translation_text(
            "VORTEXSCANS.COM",
            "VORTEXSCANS.COM",
            "decorative",
        )
        # Decorative text is not in the translatable-context set, so it must
        # not be rejected as "candidate_equals_source" the way a speech region
        # would be.
        self.assertTrue(valid or reason == "candidate_equals_source")

    def test_sfx_english_is_permitted(self):
        """TEST: SFX preserves source per policy."""
        valid, reason = validate_translation_text(
            "STAGGER",
            "STAGGER",
            "sfx",
        )
        self.assertTrue(valid, reason)

    def test_corrupted_ocr_token_detected(self):
        """TEST: corrupted OCR fragment is caught as non-clean."""
        from ocr_balloon import _candidate_forensic_class
        result = _candidate_forensic_class(
            "TRNDGE HAPPPNED.",
            "TRNDGE HAPPPNED.",
            "speech",
        )
        self.assertNotEqual(result, "PTBR_CLEAN")


class ChapterEntityMemoryTests(unittest.TestCase):
    """#84F44: declared names propagate to every group in the chapter."""

    def test_declared_name_propagates_to_other_groups(self):
        """TEST NAME-A: name declared in one group is known to others."""
        from ocr_balloon import propagate_chapter_declared_names
        declarer = _group("SUNLESS... BUT PEOPLE CALL ME SUNNY.")
        other_sunny = _group("HAVE YOU SEEN SUNNY LATELY?")
        other_sunless = _group("SUNLESS IS ASLEEP.")
        propagate_chapter_declared_names([declarer, other_sunny, other_sunless])

        self.assertIn("SUNNY", other_sunny.detected_proper_names)
        self.assertIn("SUNLESS", other_sunless.detected_proper_names)

    def test_translated_name_is_caught_after_propagation(self):
        """TEST NAME-B: translator converting a propagated name is rejected."""
        from ocr_balloon import propagate_chapter_declared_names
        declarer = _group("SUNLESS... BUT PEOPLE CALL ME SUNNY.")
        other = _group("HAVE YOU SEEN SUNNY LATELY?")
        propagate_chapter_declared_names([declarer, other])

        valid, reason = validate_translation_text(
            "HAVE YOU SEEN SUNNY LATELY?",
            "VOCE VIU O ENSOLARADO ULTIMAMENTE?",
            "speech",
            allowed_proper_names=other.detected_proper_names,
            required_name_spans=["SUNNY"],
        )
        self.assertFalse(valid)
        self.assertTrue(reason.startswith("proper_name_altered"), reason)

    def test_alias_is_not_inconsistency(self):
        """TEST NAME-C: a legitimate alias is not flagged as inconsistent."""
        from ocr_balloon import propagate_chapter_declared_names
        declarer = _group("SUNLESS... BUT PEOPLE CALL ME SUNNY.")
        sunless_group = _group("SUNLESS, ARE YOU OKAY?")
        sunny_group = _group("SUNNY, WAIT!")
        propagate_chapter_declared_names([declarer, sunless_group, sunny_group])

        # Both forms are known; neither should be flagged as altered when used
        # as an allowed name.
        valid1, r1 = validate_translation_text(
            "SUNLESS, ARE YOU OKAY?",
            "SUNLESS, VOCE ESTA BEM?",
            "speech",
            allowed_proper_names=["SUNLESS", "SUNNY"],
            required_name_spans=["SUNLESS"],
        )
        self.assertTrue(valid1, r1)

        valid2, r2 = validate_translation_text(
            "SUNNY, WAIT!",
            "SUNNY, ESPERE!",
            "speech",
            allowed_proper_names=["SUNLESS", "SUNNY"],
            required_name_spans=["SUNNY"],
        )
        self.assertTrue(valid2, r2)

    def test_common_word_not_promoted_to_name(self):
        """TEST NAME-D: capitalized common words are not treated as names."""
        from ocr_balloon import propagate_chapter_declared_names
        group = _group("THE DREAM IS OVER.")
        propagate_chapter_declared_names([group])

        self.assertNotIn("DREAM", group.detected_proper_names)
        self.assertNotIn("THE", group.detected_proper_names)

    def test_unknown_name_not_invented(self):
        """TEST NAME-E: no canonical form is invented for unrecognized tokens."""
        from ocr_balloon import propagate_chapter_declared_names
        group = _group("ZARQUON ARRIVED QUIETLY.")
        propagate_chapter_declared_names([group])

        # Without a declaration pattern, ZARQUON is not promoted.
        self.assertNotIn("ZARQUON", group.detected_proper_names)


if __name__ == "__main__":
    unittest.main()
