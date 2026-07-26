"""Contracts for the OCR plausibility detector (BLOCO 6A.1).

The point of these tests is the *negative* side: a short, rare or foreign token
is not garbage. Nothing here names a phrase from the chapter under revision —
every case is either synthetic or a generic shape.
"""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import random
import string
import unittest

import linguistic_triage as lt
import region_taxonomy as tax


def _garbage(length=6):
    """A consonant run with no vowel structure — shaped like a bad read."""
    consonants = "BCDFGHJKLMNPQRSTVWXZ"
    while True:
        token = "".join(random.choice(consonants) for _ in range(length))
        if not any(token[i] == token[i + 1] == token[i + 2] for i in range(len(token) - 2)):
            return token


class NeverAutoMarked(unittest.TestCase):
    """FASE 5 — legitimate text must never be auto-marked as invalid OCR."""

    def _assert_safe(self, text, **kw):
        result = lt.assess_ocr_plausibility(source_text=text, **kw)
        self.assertFalse(result["auto_markable"],
                         f"{text!r} was auto-marked: {result['reason_codes']}")
        self.assertNotEqual(result["status"], lt.LIKELY_OCR_INVALID, repr(text))
        return result

    def test_uncommon_proper_names(self):
        for name in ("Kuroshitsuji", "Sunny", "Nephis", "Yggdrasil", "Caeserion"):
            self._assert_safe(name, confidence=0.9)

    def test_a_proven_proper_name_carries_protective_evidence(self):
        result = self._assert_safe(_garbage(7), confidence=0.9,
                                   container_evidence={"proven_proper_name": True})
        self.assertIn("proven_proper_name", result["positive_evidence"])

    def test_acronyms(self):
        for acronym in ("NQSC", "FBI", "S.H.I.E.L.D", "UN", "HQ"):
            result = self._assert_safe(acronym, confidence=0.9)
            self.assertIn("acronym_shape", result["positive_evidence"], acronym)

    def test_short_sfx_classified_as_preservable(self):
        for sfx in ("BAM", "TMP", "SHHH"):
            result = self._assert_safe(sfx, confidence=0.9,
                                       classification=tax.SFX_PRESERVE)
            self.assertEqual(result["status"], lt.PRESERVABLE_NONSEMANTIC)
            self.assertEqual(result["recommended_action"], "confirm_preservation")

    def test_onomatopoeia_without_a_classification(self):
        for sound in ("WHOOSH", "CRASH", "AAAAH", "THUD"):
            self._assert_safe(sound, confidence=0.9)

    def test_legitimate_foreign_words(self):
        for word in ("Doppelgänger", "senpai", "déjà vu", "Weltschmerz"):
            self._assert_safe(word, confidence=0.9)

    def test_work_titles_and_place_names(self):
        for title in ("SHADOW SLAVE", "Nightmare Spell", "Forgotten Shore",
                      "THE ANTIQUARIAN'S HOUSE"):
            result = self._assert_safe(title, confidence=0.9)
            self.assertEqual(result["status"], lt.PLAUSIBLE_SEMANTIC, title)

    def test_fictional_universe_terms(self):
        for term in ("Soul Core", "Aspect Ability", "Awakened", "Sleeper"):
            self._assert_safe(term, confidence=0.9)

    def test_stuttering(self):
        for text in ("I-I-I don't know", "W-what?", "N-no way"):
            self._assert_safe(text, confidence=0.9)

    def test_interjections(self):
        for text in ("Ah!", "Huh?", "Hm...", "Eh?!"):
            self._assert_safe(text, confidence=0.9)

    def test_censored_text(self):
        for text in ("F***", "s--t", "#$%&!"):
            self._assert_safe(text, confidence=0.9)

    def test_valid_urls_and_branding(self):
        for text in ("www.example.com", "reader.example.io", "example.com.br"):
            result = self._assert_safe(text, confidence=0.9)
            self.assertIn("valid_network_suffix", result["positive_evidence"], text)
        self._assert_safe("EXAMPLE SCANS", confidence=0.9,
                          classification=tax.BRANDING_PRESERVE)

    def test_low_confidence_alone_never_marks(self):
        """A weak signal on its own is never enough."""
        result = self._assert_safe("Sunny", confidence=0.2)
        self.assertIn("very_low_ocr_confidence", result["weak_evidence"])

    def test_a_short_or_rare_token_is_not_automatically_garbage(self):
        for text in ("Xi", "Qi", "Ur", "Zed"):
            self._assert_safe(text, confidence=0.9)


class DetectsCorruptedReads(unittest.TestCase):
    """The positive side: unambiguous corruption is caught."""

    def _assert_invalid(self, text, **kw):
        result = lt.assess_ocr_plausibility(source_text=text, **kw)
        self.assertEqual(result["status"], lt.LIKELY_OCR_INVALID,
                         f"{text!r}: {result['status']} {result['reason_codes']}")
        self.assertTrue(result["auto_markable"])
        self.assertEqual(result["recommended_action"], "confirm_ocr_invalid")
        self.assertTrue(result["reason_codes"], "a verdict must carry evidence")
        return result

    def test_mojibake(self):
        for text in ("OlÃ¡ amigo", "coraÃ§Ã£o", "n�o sei"):
            self._assert_invalid(text, confidence=0.9)

    def test_control_characters(self):
        self._assert_invalid("hel\x01lo the\x02re", confidence=0.9)

    def test_digits_glued_inside_words(self):
        for text in ("0ON", "S0METHING", "l1ght"):
            self._assert_invalid(text, confidence=0.9)

    def test_corrupted_network_suffix(self):
        result = self._assert_invalid("EXAMPLESCANS.OPG", confidence=0.9)
        self.assertIn("corrupted_url_or_watermark_suffix", result["strong_evidence"])

    def test_two_independent_weak_signals_with_no_protective_evidence(self):
        result = self._assert_invalid(_garbage(9), confidence=0.2)
        self.assertGreaterEqual(len(result["weak_evidence"]), 2)
        self.assertEqual(result["positive_evidence"], [])

    def test_a_single_weak_signal_is_not_enough(self):
        result = lt.assess_ocr_plausibility(source_text=_garbage(9), confidence=0.9)
        self.assertEqual(result["status"], lt.AMBIGUOUS_OCR)
        self.assertFalse(result["auto_markable"])


class AmbiguityStaysAmbiguous(unittest.TestCase):
    def test_ambiguous_asks_for_a_human(self):
        result = lt.assess_ocr_plausibility(source_text="DYoll", confidence=0.6)
        self.assertEqual(result["status"], lt.AMBIGUOUS_OCR)
        self.assertEqual(result["recommended_action"], "human_review")
        self.assertFalse(result["auto_markable"])

    def test_empty_text_is_not_a_verdict(self):
        result = lt.assess_ocr_plausibility(source_text="   ", confidence=0.9)
        self.assertFalse(result["auto_markable"])

    def test_every_verdict_is_explainable(self):
        for text in ("Sunny", "0ON", "SHADOW SLAVE", "FVIP", _garbage(8)):
            result = lt.assess_ocr_plausibility(source_text=text, confidence=0.5)
            self.assertTrue(result["reason_codes"] or result["positive_evidence"], text)
            for key in ("alphabetic_ratio", "vowel_ratio", "symbol_ratio",
                        "digit_ratio", "repetition_score", "word_count",
                        "lexical_plausibility", "confidence"):
                self.assertIn(key, result)

    def test_deterministic(self):
        text = _garbage(7)
        first = lt.assess_ocr_plausibility(source_text=text, confidence=0.4)
        second = lt.assess_ocr_plausibility(source_text=text, confidence=0.4)
        self.assertEqual(first, second)

    def test_no_chapter_specific_vocabulary_in_the_detector(self):
        """Zero-hardcode: the detector must not name words from any chapter."""
        import inspect
        source = inspect.getsource(lt.assess_ocr_plausibility)
        for forbidden in ("sunny", "nephis", "vortex", "shadow", "slave", "cassie"):
            self.assertNotIn(forbidden, source.lower())


if __name__ == "__main__":
    unittest.main()
