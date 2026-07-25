"""Hermetic tests for the semantic region taxonomy (BLOCO 2). Offline, no network."""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest

import region_taxonomy as tax


class SemanticTranslatableCases(unittest.TestCase):
    def _cat(self, legacy, text, **kw):
        return tax.normalize(legacy, text=text, **kw)[0]

    def test_real_coffee_is_semantic_not_sfx(self):
        # The obligatory regression: styled out-of-balloon text with meaning,
        # whatever coarse visual/uncertain bucket the legacy classifier used.
        for legacy in ("decorative", "sfx", "unknown", ""):
            cat = self._cat(legacy, "REAL COFFEE.")
            self.assertEqual(cat, tax.DECORATIVE_SEMANTIC_TRANSLATE, legacy)
            self.assertTrue(tax.is_translatable(cat))
            self.assertFalse(tax.is_preservable(cat))

    def test_page4_and_page5_narration_is_translatable(self):
        self.assertTrue(tax.is_translatable(self._cat("narration", "SINCE I'VE SPENT ALL THE MONEY I HAD LEFT ON IT...")))
        # even mislabeled as decorative, a full sentence is semantic
        self.assertTrue(tax.is_translatable(self._cat("decorative", "...IT BETTER BE WORTH IT.")))

    def test_styling_alone_is_not_proof_of_sfx(self):
        # Uppercase, punctuation, short phrase, two related regions: still semantic.
        for text in ("THE NIGHTMARE SPELL", "WELCOME TO THE SPELL.", "CODE BLACK IN PROGRESS!"):
            self.assertTrue(tax.is_translatable(self._cat("decorative", text)), text)

    def test_dialogue_and_title(self):
        self.assertEqual(self._cat("speech", "Hello there."), tax.DIALOGUE_TRANSLATE)
        self.assertEqual(self._cat("title", "The Awakening"), tax.TITLE_SEMANTIC_TRANSLATE)


class PreservedCases(unittest.TestCase):
    def _cat(self, legacy, text, **kw):
        return tax.normalize(legacy, text=text, **kw)[0]

    def test_real_sfx_stays_preserved(self):
        for word in ("BAM", "WHOOSH", "THUD", "CLANG", "GRR", "TSK", "AAAH"):
            cat = self._cat("sfx", word)
            self.assertEqual(cat, tax.SFX_PRESERVE, word)
            self.assertTrue(tax.is_preservable(cat))

    def test_url_credit_watermark_preserved_regardless_of_legacy(self):
        self.assertEqual(self._cat("decorative", "www.readmanga.com"), tax.URL_PRESERVE)
        self.assertEqual(self._cat("speech", "https://scans.example.io/x"), tax.URL_PRESERVE)
        self.assertEqual(self._cat("decorative", "Translated by NightScans"), tax.CREDIT_PRESERVE)
        self.assertEqual(self._cat("narration", "Typeset & Redraw: team"), tax.CREDIT_PRESERVE)

    def test_proven_proper_name_preserved(self):
        self.assertEqual(self._cat("sfx", "Sunny", preserve_as_name=True), tax.PROPER_NAME_PRESERVE)
        self.assertEqual(self._cat("proper_name", "Nightmare"), tax.PROPER_NAME_PRESERVE)


class AmbiguousCases(unittest.TestCase):
    def test_short_ambiguous_tokens_fail_closed(self):
        # Acronym-like or too short to judge: never silently preserved/translated.
        for legacy, text in (("sfx", "NQSC"), ("sfx", "XZ"), ("decorative", "O"), ("decorative", "7B")):
            cat = tax.normalize(legacy, text=text)[0]
            self.assertEqual(cat, tax.UNKNOWN_REVIEW_REQUIRED, (legacy, text))
            self.assertTrue(tax.needs_human_review(cat))

    def test_unrecognised_legacy_label_fails_closed(self):
        cat, reason = tax.normalize("brand_new_bucket", text="whatever it is")
        self.assertEqual(cat, tax.UNKNOWN_REVIEW_REQUIRED)
        self.assertEqual(reason, "fail_closed_unknown_label")

    def test_legacy_unknown_without_real_words_stays_uncertain(self):
        # "unknown" is re-evaluated by text: garbage/short tokens stay uncertain.
        for text in ("NQSC", "XZ", "7B", "@@"):
            self.assertEqual(tax.normalize("unknown", text=text)[0], tax.UNKNOWN_REVIEW_REQUIRED, text)


class LegacyCompatibility(unittest.TestCase):
    def test_every_legacy_label_maps_into_the_taxonomy(self):
        for legacy in ("speech", "thought", "dialogue", "narration", "sfx", "decorative",
                       "credit", "watermark", "url", "proper_name", "title", "unknown", ""):
            cat, reason = tax.normalize(legacy, text="Some sentence with meaning.")
            self.assertIn(cat, tax.ALL_CATEGORIES, legacy)
            self.assertTrue(reason)

    def test_suggested_action_partitions_cleanly(self):
        self.assertEqual(tax.suggested_action(tax.NARRATION_TRANSLATE), "translate")
        self.assertEqual(tax.suggested_action(tax.SFX_PRESERVE), "preserve")
        self.assertEqual(tax.suggested_action(tax.UNKNOWN_REVIEW_REQUIRED), "human_review")

    def test_categories_are_disjoint(self):
        self.assertEqual(tax.PRESERVE & tax.TRANSLATE, frozenset())
        self.assertEqual(tax.PRESERVE & tax.UNCERTAIN, frozenset())
        self.assertEqual(tax.TRANSLATE & tax.UNCERTAIN, frozenset())


if __name__ == "__main__":
    unittest.main()
