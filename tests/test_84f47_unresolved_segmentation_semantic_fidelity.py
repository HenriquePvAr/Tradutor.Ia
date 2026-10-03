"""#84F47 - a collapsed source run resolved into the wrong real word.

#84F45R made ``source_segmentation_incomplete`` a decision instead of a veto: a
collapsed OCR run only holds the region when the unreadable residual actually
reached the candidate.  That discriminator was measured *literally* - the
candidate word had to contain four of the residual's letters in sequence - so it
caught "AGATE" -> "GÁTES" and missed "AGATE" -> "GATA".

"GATA" is a real Portuguese word, spelled correctly, in a grammatical sentence,
and it is still the unread fragment: the provider gave the letters nobody could
read a Portuguese ending and shipped them as a female cat where the source had a
gate.  Nothing downstream can tell that apart from a translation, which is
exactly the class this file pins: source segmentation unresolved, target fluent,
target semantically wrong, no *literal* debris.

No page, region, provider or string is special-cased; the residual and the
candidate are the only inputs.
"""
from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest

import semantic_fidelity


def _fidelity(source, candidate, **kwargs):
    finding = semantic_fidelity.evaluate_local_fidelity(source, candidate, **kwargs)
    reason = finding.reason()
    return reason, semantic_fidelity.review_usability(reason)


class UnresolvedSegmentationSemanticFidelityTests(unittest.TestCase):

    def test_fragment_reshaped_into_a_real_target_word_is_still_debris(self):
        """The #84F47 blocker: fluent, valid, wrong, and zero literal debris."""
        reason, usability = _fidelity(
            "..YOU BE COME AGATETHROUGHWHICH AMONSTERAPPEARSIN THEREALWORLD.",
            "...VOCÊ SE TORNA A GATA ATRAVÉS DA QUAL UM MONSTRO "
            "APARECE NO MUNDO REAL.",
        )
        self.assertTrue(
            reason.startswith(semantic_fidelity.SOURCE_SEGMENTATION_INCOMPLETE), reason
        )
        self.assertEqual(usability, semantic_fidelity.REVIEW_UNUSABLE)

    def test_the_debris_measure_itself_tolerates_one_edit(self):
        self.assertEqual(
            semantic_fidelity.unresolved_run_debris(("AGATETHROUGHWHICH",), "A GATA"),
            ("GATA",),
        )
        # The literal case #84F45R already covered must not regress.
        self.assertEqual(
            semantic_fidelity.unresolved_run_debris(("AGATETHROUGHWHICH",), "A GÁTES"),
            ("GATES",),
        )

    def test_a_faithful_translation_of_the_same_run_stays_renderable(self):
        """Same source, same residual - the correct Portuguese must still ship."""
        reason, usability = _fidelity(
            "..YOU BE COME AGATETHROUGHWHICH AMONSTERAPPEARSIN THEREALWORLD.",
            "...VOCÊ SE TORNA UM PORTAL ATRAVÉS DO QUAL UM MONSTRO "
            "APARECE NO MUNDO REAL.",
        )
        self.assertTrue(
            reason.startswith(semantic_fidelity.SOURCE_SEGMENTATION_RECOVERED), reason
        )
        self.assertEqual(usability, semantic_fidelity.REVIEW_RENDERABLE)

    def test_p28_clean_candidate_over_a_collapsed_run_is_unaffected(self):
        reason, usability = _fidelity(
            "INSIDEYOURFIRST NIGHTMARE,YOU'LLRUN INTO MONSTERS,SURE, "
            "BUTYOUWILLALSO MEETPEOPLE.",
            "NO SEU PRIMEIRO PESADELO, VOCÊ VAI SE DEPARAR COM MONSTROS, "
            "É CLARO, MAS TAMBÉM VAI CONHECER PESSOAS.",
        )
        self.assertTrue(
            reason.startswith(semantic_fidelity.SOURCE_SEGMENTATION_RECOVERED), reason
        )
        self.assertEqual(usability, semantic_fidelity.REVIEW_RENDERABLE)

    def test_a_preserved_proper_name_is_never_read_as_debris(self):
        reason, usability = _fidelity(
            "SUNNY,BUTYOUWILLALSO MEETPEOPLE.",
            "SUNNY, MAS VOCÊ TAMBÉM VAI CONHECER PESSOAS.",
            protected_entities=("SUNNY",),
        )
        self.assertTrue(
            reason.startswith(semantic_fidelity.SOURCE_SEGMENTATION_RECOVERED), reason
        )
        self.assertEqual(usability, semantic_fidelity.REVIEW_RENDERABLE)

    def test_a_trusted_source_never_reaches_the_segmentation_gate(self):
        reason, _ = _fidelity(
            "WHAT YOU DO DURING THE TRIAL WILL DETERMINE THE REWARDS.",
            "O QUE VOCÊ FIZER DURANTE A PROVA DETERMINARÁ AS RECOMPENSAS.",
        )
        self.assertEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
