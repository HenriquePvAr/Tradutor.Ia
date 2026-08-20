"""TDD #62 - story review to clean render closure contracts.

The fixtures are synthetic and offline: they encode the #60 failure classes without using
the real PDF, source pages, provider, network, Drive, Community or Supabase.
"""

import _test_bootstrap  # noqa: F401
from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest

import numpy as np

import config
import source_completeness
from ocr_balloon import (
    TextGroup,
    enforce_rapidocr_quality_gate,
    score_group_ocr_quality,
    _should_translate_group,
    _source_scoped_speech_reason,
    apply_group_translations,
    validate_translation_text,
)
from ocr_engine import OCRLine


def _line(text="STORY TEXT", box=(10, 10, 180, 40), line_id="L0"):
    x, y, w, h = box
    line = OCRLine(
        text=text,
        confidence=0.97,
        polygon=np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.int32),
        box=box,
        raw_text=text,
        engine="rapidocr",
        page=1,
    )
    line.metadata = {"ocr_line_id": line_id}
    return line


def _story_group(text, classification="speech"):
    group = TextGroup(
        group_id="BALAO_1",
        lines=[_line(text)],
        text=text,
        classification=classification,
    )
    group.translation = "TRADUÇÃO PT-BR"
    group.translation_candidate = "TRADUÇÃO PT-BR"
    group.source_completeness = {"status": source_completeness.STATUS_PASS}
    return group


class StoryReviewToRenderClosureTests(unittest.TestCase):
    def test_narration_can_use_source_scoped_cleanup_when_story_owned(self):
        group = _story_group(
            "THE WORLD TOOK NOTICE.",
            classification="narration",
        )

        self.assertEqual(_source_scoped_speech_reason(group), "")

    def test_system_story_text_from_unknown_class_is_renderable_when_semantic(self):
        group = _story_group(
            "Aspirant! Welcome to the Nightmare Spell.",
            classification="unknown",
        )

        self.assertTrue(_should_translate_group(group))
        self.assertEqual(_source_scoped_speech_reason(group), "")

    def test_stylized_story_text_is_eligible_but_sfx_and_promo_remain_excluded(self):
        stylized = _story_group(
            "THAT'S WHAT THEY USED TO SAY.",
            classification="unknown",
        )
        sfx = _story_group("THUNK", classification="sfx")
        promo = _story_group("READ FIRST AT VORTEXSCANS.COM", classification="unknown")
        logo = _story_group("VORTEX SCANS", classification="logo")

        self.assertTrue(_should_translate_group(stylized))
        self.assertFalse(_should_translate_group(sfx))
        self.assertFalse(_should_translate_group(promo))
        self.assertFalse(_should_translate_group(logo))

    def test_declared_proper_name_repair_preserves_token_without_rejecting_region(self):
        group = _story_group(
            "Sunless... but people call me Sunny.",
            classification="narration",
        )

        apply_group_translations(
            [group],
            [{"translation": "Sem sol... mas as pessoas me chamam de Sunny."}],
        )

        self.assertTrue(group.translation_valid, group.translation_validation_reason)
        self.assertIn("Sunless", group.translation)
        self.assertIn("Sunny", group.translation)
        self.assertNotIn("Sem sol", group.translation)

    def test_declared_proper_name_repair_normalizes_ocr_mixed_case_surface(self):
        group = _story_group(
            "SuNLEsS... BUT PEOPLE CALL Me Sunny.",
            classification="narration",
        )

        apply_group_translations(
            [group],
            [{"translation": "Sem sol... mas as pessoas me chamam de Sunny."}],
        )

        self.assertTrue(group.translation_valid, group.translation_validation_reason)
        self.assertIn("Sunless", group.translation)
        self.assertIn("Sunny", group.translation)
        self.assertNotIn("SuNLEsS", group.translation)
        self.assertNotIn("Sem sol", group.translation)

    def test_bad_voce_infinitive_candidate_is_repaired_narrowly(self):
        group = _story_group(
            "WHAT YOU DO DURING THE TRIAL WILL DETERMINE THE REWARDS.",
            classification="speech",
        )

        apply_group_translations(
            [group],
            [{"translation": "O que você fazer durante a prova determinará as recompensas."}],
        )

        self.assertTrue(group.translation_valid, group.translation_validation_reason)
        self.assertIn("você fizer", group.translation.lower())

    def test_stray_ocr_debris_fragment_is_removed_before_validation(self):
        group = _story_group(
            "SO DO YOURSELF A FAVOR AND JUST THINK ABOUT THEM AS ILLUSIONS.",
            classification="narration",
        )

        apply_group_translations(
            [group],
            [{"translation": "Então, faça um favor a si mesmo e) apenas pense nelas como ilusões."}],
        )

        self.assertTrue(group.translation_valid, group.translation_validation_reason)
        self.assertIn("e apenas", group.translation.lower())
        self.assertNotIn("e)", group.translation.lower())

    def test_portuguese_do_is_not_mixed_language_residual(self):
        valid, reason = validate_translation_text(
            "HOW MUCH DO YOU REALLY KNOW ABOUT THE NIGHTMARE SPELL?",
            "O que você realmente sabe sobre o Feitiço do Pesadelo?",
            "speech",
        )

        self.assertTrue(valid, reason)

    def test_long_damaged_story_sentences_are_routed_to_translation_not_retained(self):
        samples = [
            (
                "BUTWHENITSVICTIMS BEGANFALLINGINTOAN ENDLESS SLLMBER, THE WORLD TOOK NOTICE.",
                "speech",
            ),
            (
                "FLRTHERMORE, CHILDREN BORN INTO POWERFLL AWAKENED FAMILIES..",
                "narration",
            ),
            (
                "SOWEWOULD REALLYAPPRECIATEIT IFYOUDIDN'TMAKEUS FIGHTTHATTHING OURSELVES...",
                "narration",
            ),
        ]
        original_engine = config.OCR_ENGINE
        try:
            config.OCR_ENGINE = "rapidocr"
            for text, classification in samples:
                with self.subTest(text=text):
                    group = _story_group(text, classification=classification)
                    group.source_engine = "rapidocr"
                    for line in group.lines:
                        line.confidence = 0.55
                    group.quality_score, group.quality_reasons = score_group_ocr_quality(group)

                    blocked = enforce_rapidocr_quality_gate([group])

                    self.assertEqual(blocked, [])
                    self.assertFalse(group.ocr_quality_blocked)
                    self.assertTrue(_should_translate_group(group))
                    self.assertTrue(group.quality_evidence.get("ocr_source_suspicious"))
        finally:
            config.OCR_ENGINE = original_engine

    def test_short_unintelligible_token_still_fails_closed(self):
        group = _story_group("TRNDGE", classification="speech")
        group.source_engine = "rapidocr"
        group.quality_score, group.quality_reasons = score_group_ocr_quality(group)

        enforce_rapidocr_quality_gate([group])

        self.assertFalse(_should_translate_group(group))


if __name__ == "__main__":
    unittest.main()
