"""#84F45R - two false positives that held back correct Portuguese.

Part A. ``source_segmentation_incomplete`` fired on the *source* and vetoed the
region outright, so a collapsed OCR run made every candidate unusable regardless
of what the candidate said.  Two real regions prove the two sides of that: one
whose Portuguese shares nothing with the unreadable run, and one that carried the
unreadable run onto the page as a word that is neither language.  The veto has to
survive for the second and step aside for the first, on evidence, with no page,
region or string special-cased.

Part B. The post-render residual-English gate forgives a flagged token when the
*expected* translation explains it and the source does not.  It tested the raw
provenance, so a token belonging to both sides - Portuguese "SE VOCE FOR" against
English "WAITING FOR YOU" - could never be forgiven, and a correct render was
rejected for printing its own translation.
"""
from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest
from unittest.mock import patch

import numpy as np

import semantic_fidelity
from ocr_balloon import _post_render_source_text_check
from test_ocr_quality_regressions import _line, _scored_group


def _fidelity(source, candidate, **kwargs):
    finding = semantic_fidelity.evaluate_local_fidelity(source, candidate, **kwargs)
    reason = finding.reason()
    return reason, semantic_fidelity.review_usability(reason)


class SourceSegmentationAcceptanceTests(unittest.TestCase):
    """Part A - the veto becomes a decision, and only where evidence allows."""

    def test_a1_clean_candidate_over_collapsed_source_stays_renderable(self):
        reason, usability = _fidelity(
            "INSIDEYOURFIRST NIGHTMARE,YOU'LLRUN INTO MONSTERS,SURE, "
            "BUTYOUWILLALSO MEETPEOPLE.",
            "NO SEU PRIMEIRO PESADELO, VOCÊ VAI SE DEPARAR COM MONSTROS, "
            "É CLARO, MAS TAMBÉM VAI CONHECER PESSOAS.",
        )
        # Still reviewed - the source really is unverifiable - but readable.
        self.assertTrue(
            reason.startswith(semantic_fidelity.SOURCE_SEGMENTATION_RECOVERED), reason
        )
        self.assertEqual(usability, semantic_fidelity.REVIEW_RENDERABLE)

    def test_a2_candidate_carrying_the_unread_run_stays_unusable(self):
        reason, usability = _fidelity(
            "..YOU BE COME AGATETHROUGHWHICH AMONSTERAPPEARSIN THEREALWORLD.",
            "...VOCÊ SE TORNA A GÁTES ATRAVÉS DA QUAL UM MONSTRO "
            "APARECE NO MUNDO REAL.",
        )
        self.assertTrue(
            reason.startswith(semantic_fidelity.SOURCE_SEGMENTATION_INCOMPLETE), reason
        )
        self.assertEqual(usability, semantic_fidelity.REVIEW_UNUSABLE)
        self.assertEqual(
            semantic_fidelity.unresolved_run_debris(("AGATETHROUGHWHICH",),
                                                    "A GÁTES"),
            ("GATES",),
        )

    def test_a3_malformed_portuguese_over_collapsed_source_stays_unusable(self):
        reason, usability = _fidelity(
            "ASK HIM, BUTYOUWILLALSO WANT TO KNOW.",
            "PERGUNTE A ELE, MAS VOCE QUERER SABER TAMBEM.",
        )
        self.assertTrue(
            reason.startswith(semantic_fidelity.SOURCE_SEGMENTATION_INCOMPLETE), reason
        )
        self.assertEqual(usability, semantic_fidelity.REVIEW_UNUSABLE)

    def test_a3b_wrong_word_sense_over_collapsed_source_stays_unusable(self):
        reason, usability = _fidelity(
            "THE CELL WASLOCKEDWITHTHOSE CHAINS.",
            "A CELULA FOI TRANCADA COM AQUELAS CORRENTES.",
        )
        self.assertTrue(
            reason.startswith(semantic_fidelity.SOURCE_SEGMENTATION_INCOMPLETE), reason
        )
        self.assertEqual(usability, semantic_fidelity.REVIEW_UNUSABLE)

    def test_a4_ordinary_trusted_source_is_untouched(self):
        reason, _ = _fidelity(
            "WHAT YOU DO DURING THE TRIAL WILL DETERMINE THE REWARDS.",
            "O QUE VOCÊ FIZER DURANTE A PROVA DETERMINARÁ AS RECOMPENSAS.",
        )
        self.assertEqual(reason, "")

    def test_a5_protected_names_still_block_over_a_collapsed_source(self):
        # The new path is reached only after every blocking rule passed, so a
        # dropped protected name is decided before segmentation is consulted.
        finding = semantic_fidelity.evaluate_local_fidelity(
            "SUNLESS,BUTYOUWILLALSO MEETPEOPLE.",
            "SEM SOL, MAS VOCÊ TAMBÉM VAI CONHECER PESSOAS.",
            protected_entities=("SUNLESS",),
        )
        self.assertEqual(finding.status, semantic_fidelity.BLOCKED)
        self.assertTrue(
            finding.reason().startswith(semantic_fidelity.ENTITY_CHANGED),
            finding.reason(),
        )

    def test_a5b_preserved_name_over_collapsed_source_is_not_debris(self):
        reason, usability = _fidelity(
            "SUNNY,BUTYOUWILLALSO MEETPEOPLE.",
            "SUNNY, MAS VOCÊ TAMBÉM VAI CONHECER PESSOAS.",
            protected_entities=("SUNNY",),
        )
        self.assertTrue(
            reason.startswith(semantic_fidelity.SOURCE_SEGMENTATION_RECOVERED), reason
        )
        self.assertEqual(usability, semantic_fidelity.REVIEW_RENDERABLE)


class PostRenderResidualEnglishTests(unittest.TestCase):
    """Part B - a token both languages spell is not proof the source survived."""

    @staticmethod
    def _check(source, translation, observed, page_index=91):
        group = _scored_group(source)
        group.translation = translation
        group.safe_area = (0, 0, 220, 80)
        image = np.full((100, 240, 3), 255, dtype=np.uint8)
        with patch(
            "ocr_balloon.OCREngine._detect_with_rapidocr",
            return_value=[_line(observed)],
        ):
            return _post_render_source_text_check(image, group, page_index=page_index)

    def test_b1_token_the_translation_itself_spells_is_forgiven(self):
        # "FOR" is the future subjunctive of *ser* in the rendered Portuguese and
        # the English preposition in the source. The render is the translation.
        # The observed read is glued the way RapidOCR really returns a balloon,
        # which is what makes the isolated "FOR" look English at all.
        result = self._check(
            "REWARDS WILL BE WAITING FOR YOU IF YOU SUCCEED",
            "RECOMPENSAS ESTARAO ESPERANDO SE VOCE FOR BEM-SUCEDIDO",
            "RECOMPENSASESTARAO ESPERANDOSEVOCE FOR BEM-SUCEDIDO",
        )
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["forgiven_ocr_noise_tokens"], ["FOR"])
        self.assertEqual(result["residual_source_tokens"], [])

    def test_b2_english_the_translation_cannot_explain_still_blocks(self):
        result = self._check(
            "WAITING FOR YOU",
            "ESPERANDO POR VOCE",
            "ESPERANDO FOR VOCE",
        )
        self.assertFalse(result["passed"], result)
        self.assertEqual(result["reason"], "source_language_detected_after_render")

    def test_b3_a_surviving_english_phrase_still_blocks(self):
        result = self._check(
            "WHAT YOU DO DURING THE TRIAL WILL DETERMINE THE REWARDS",
            "O QUE VOCE FIZER DURANTE A PROVA DETERMINARA AS RECOMPENSAS",
            "WHAT YOU DO DURING THE TRIAL WILL DETERMINE THE REWARDS",
        )
        self.assertFalse(result["passed"], result)
        self.assertEqual(result["reason"], "source_language_detected_after_render")

    def test_b4_forgiveness_needs_the_whole_line_to_read_as_the_translation(self):
        # Same colliding token, but the observed text is not the translation:
        # forgiveness is a three-way judgement, never a per-token allowlist.
        result = self._check(
            "REWARDS WILL BE WAITING FOR YOU IF YOU SUCCEED",
            "RECOMPENSAS ESTARAO ESPERANDO SE VOCE FOR BEM-SUCEDIDO",
            "WAITING FOR YOU IF YOU SUCCEED",
        )
        self.assertFalse(result["passed"], result)

    def test_b5_token_absent_from_the_translation_is_never_forgiven(self):
        result = self._check(
            "YOU WILL BECOME A GATE",
            "VOCE SE TORNARA UM PORTAL",
            "VOCE SE TORNARA UM GATE",
        )
        self.assertFalse(result["passed"], result)

    def test_b6_a_clean_render_is_unaffected(self):
        result = self._check(
            "YOU WILL MEET PEOPLE",
            "VOCE VAI CONHECER PESSOAS",
            "VOCE VAI CONHECER PESSOAS",
        )
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["residual_source_tokens"], [])


if __name__ == "__main__":
    unittest.main()
