"""TDD #82 - semantic meaning preservation and natural PT-BR quality.

The #81 gate proved ordinary story text is translated and no ordinary English
survives on the page.  It did not prove the Portuguese still *says what the
source said*, nor that it is Portuguese a reader would accept.

Three questions this suite pins, in the order the gate must ask them:

1. **Can the source be trusted at all?**  OCR that glues a sentence into one
   token, or that turns ``SLUM`` into ``SLLM``, is not a translation defect; a
   fluent sentence invented on top of it is not clean output either.  Those
   regions are marked for review - never silently accepted, never blocked into
   a page that then shows English again.
2. **Was the meaning preserved?**  Quantities spelled as words, and ordering
   relations (``before``/``after``/``until``) the candidate inverts or invents,
   are proven mismatches and reject the candidate so the retry can do better.
3. **Is it natural Portuguese?**  Narrow, high-confidence malformation only.

Real persisted sentinels are replayed from their exact persisted strings, which
belong here and nowhere else: production code keys off no page, region, chapter
or sentence.  Nothing in this file calls a provider, a runner, the network,
Supabase, Community or Drive.
"""

import _test_bootstrap  # noqa: F401

import unittest

import numpy as np

import semantic_fidelity
import translator_deepl
import translator_nvidia
from ocr_balloon import (
    OCRLine,
    TextGroup,
    _token_is_source_vocabulary,
    detect_proper_name_spans,
    validate_and_retry_translations,
)


# --- real persisted sentinels ------------------------------------------------
# output/3a396e01_77d9_44b4_95a0_4759e2e7eb17/quality_report.json
# page 68, REGION_002, narration, accepted clean by the #81 gate.
P068_SOURCE = "TAKEAFEWHOURS FORTHENEAREST AWAKENEDTO GET HERE."
P068_ACCEPTED = "LEVE ALGUMAS HORAS PARA CHEGAR AQUI, DEPOIS QUE ACORDAR."
# The same region, page 43 REGION_001: OCR read SLUM as SLLM and the garbage
# token survived verbatim into the Portuguese.
P043_SOURCE = "THAT HAS NOTHING TO DO WITH A SLLM RAT LIKE ME."
P043_ACCEPTED = "ISSO NAO TEM NADA A VER COM UM RATO DO SLLM COMO EU."


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


def _group(text, translation="", classification="speech", group_id="T001"):
    group = TextGroup(
        group_id=group_id,
        lines=[_line(text)],
        text=text,
        classification=classification,
        inside_balloon_like_region=True,
        source_engine="rapidocr",
    )
    group.detected_proper_names = list(detect_proper_name_spans(text))
    group.translation = translation
    group.translation_candidate = translation
    group.sent_to_translation = bool(translation)
    return group


def _evaluate(source, candidate, **kwargs):
    kwargs.setdefault("is_source_word", _token_is_source_vocabulary)
    kwargs.setdefault("proper_names", tuple(detect_proper_name_spans(source)))
    return semantic_fidelity.evaluate_local_fidelity(source, candidate, **kwargs)


class _ScriptedTranslator:
    is_configured = True

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def translate_strict(self, text, previous_translation="", validation_reason="",
                         force=False, allow_proper_names=True, proper_names=None,
                         **kwargs):
        self.calls.append({"validation_reason": validation_reason})
        return self.responses.pop(0) if self.responses else ""


class SourceTrustTests(unittest.TestCase):
    """Is the source itself readable?  Asked before anything is blamed on the provider."""

    def test_clean_source_and_faithful_candidate_stay_silent(self):
        finding = _evaluate("I WILL WAIT HERE FOR YOU.", "VOU ESPERAR VOCE AQUI.")
        self.assertTrue(finding.faithful)

    def test_garbled_source_token_surviving_into_target_is_reviewed(self):
        finding = _evaluate(P043_SOURCE, P043_ACCEPTED)
        self.assertEqual(finding.status, semantic_fidelity.REVIEW)
        self.assertEqual(finding.primary_reason, semantic_fidelity.SOURCE_OCR_SUSPICIOUS)
        self.assertIn("SLLM", finding.critical_fact_mismatches)

    def test_run_together_ocr_that_translated_correctly_is_not_flagged(self):
        """Agglutination is endemic and mostly recovered; it is not a defect signal.

        Flagging it would bury the real defects under a review queue four times
        their size - 126 of 542 persisted real regions carry a run-together
        token, and almost all of them translated correctly.
        """
        finding = _evaluate(
            "ITFIRSTAPPEARED DECADESAGO.", "ISSO SURGIU PELA PRIMEIRA VEZ HA DECADAS."
        )
        self.assertTrue(finding.faithful)

    def test_proper_name_kept_verbatim_is_not_a_suspicious_source_token(self):
        finding = _evaluate(
            "DOCTOR ALTRON, WE'LL SPEAK AGAIN IN A MONTH OR TWO.",
            "DOUTOR ALTRON, VAMOS CONVERSAR DAQUI A UM OU DOIS MESES.",
        )
        self.assertTrue(finding.faithful)

    def test_stylised_interjection_is_not_a_suspicious_source_token(self):
        finding = _evaluate("HMMM... INTERESTING.", "HMMM... INTERESSANTE.")
        self.assertTrue(finding.faithful)

    def test_source_trust_review_never_blocks_the_candidate(self):
        self.assertNotIn(
            semantic_fidelity.SOURCE_OCR_SUSPICIOUS,
            semantic_fidelity.BLOCKING_FIDELITY_REASON_CODES,
        )


class TemporalRelationTests(unittest.TestCase):
    """Ordering words carry meaning; a fluent sentence may still reverse them."""

    def test_before_rendered_as_after_is_rejected(self):
        finding = _evaluate("COME BACK BEFORE HE ARRIVES.", "VOLTE DEPOIS QUE ELE CHEGAR.")
        self.assertFalse(finding.faithful)
        self.assertEqual(
            finding.primary_reason, semantic_fidelity.TEMPORAL_RELATION_CHANGED
        )

    def test_before_rendered_as_before_passes(self):
        finding = _evaluate("COME BACK BEFORE HE ARRIVES.", "VOLTE ANTES QUE ELE CHEGUE.")
        self.assertTrue(finding.faithful)

    def test_ordering_relation_the_source_never_stated_is_rejected(self):
        """The real P068 defect, stated generically: an invented ``depois que``."""
        finding = _evaluate(
            "IT'LL TAKE A FEW HOURS FOR THE NEAREST AWAKENED TO GET HERE.",
            P068_ACCEPTED,
        )
        self.assertFalse(finding.faithful)
        self.assertEqual(
            finding.primary_reason, semantic_fidelity.TEMPORAL_RELATION_CHANGED
        )

    def test_real_p068_candidate_is_no_longer_accepted_clean(self):
        finding = _evaluate(P068_SOURCE, P068_ACCEPTED)
        self.assertFalse(finding.faithful)
        self.assertIn(
            finding.primary_reason,
            {
                semantic_fidelity.TEMPORAL_RELATION_CHANGED,
                semantic_fidelity.SOURCE_OCR_SUSPICIOUS,
            },
        )

    def test_after_kept_as_after_passes(self):
        finding = _evaluate(
            "AFTER THIS MISSION, I AM LEAVING THE GUILD.",
            "DEPOIS DESTA MISSAO, VOU SAIR DA GUILDA.",
        )
        self.assertTrue(finding.faithful)


class QuantityWordTests(unittest.TestCase):
    """A quantity spelled as a word is still a quantity."""

    def test_three_days_rendered_as_two_days_blocks(self):
        finding = _evaluate("IT TOOK THREE DAYS.", "LEVOU DOIS DIAS.")
        self.assertEqual(finding.status, semantic_fidelity.BLOCKED)
        self.assertEqual(finding.primary_reason, semantic_fidelity.QUANTITY_CHANGED)

    def test_three_days_rendered_faithfully_passes(self):
        finding = _evaluate("IT TOOK THREE DAYS.", "LEVOU TRES DIAS.")
        self.assertTrue(finding.faithful)

    def test_quantity_word_dropped_without_another_number_is_not_claimed(self):
        """Portuguese may carry the count in the noun; only a *conflict* blocks."""
        finding = _evaluate("GIVE ME ONE MOMENT.", "ME DA UM SEGUNDO.")
        self.assertTrue(finding.faithful)


class NaturalPortugueseTests(unittest.TestCase):
    """Narrow, high-confidence malformation only."""

    def test_bare_infinitive_after_a_pronoun_is_reviewed(self):
        finding = _evaluate("YOU DO IT NOW.", "VOCE FAZER ISSO AGORA.")
        self.assertEqual(finding.status, semantic_fidelity.REVIEW)
        self.assertEqual(finding.primary_reason, semantic_fidelity.GRAMMAR_MALFORMED)

    def test_duplicated_function_word_is_reviewed(self):
        finding = _evaluate("THE DOOR OF THE HOUSE.", "A PORTA DA DA CASA.")
        self.assertEqual(finding.status, semantic_fidelity.REVIEW)
        self.assertEqual(finding.primary_reason, semantic_fidelity.GRAMMAR_MALFORMED)

    def test_short_dialogue_is_never_rejected_for_brevity(self):
        for source, candidate in (
            ("JUST...", "SO..."),
            ("WHAT?!", "O QUE?!"),
            ("HEY!", "EI!"),
            ("...", "..."),
        ):
            with self.subTest(source=source):
                self.assertTrue(_evaluate(source, candidate).faithful)

    def test_legitimate_repetition_is_not_malformed(self):
        finding = _evaluate("NO, NO, NO!", "NAO, NAO, NAO!")
        self.assertTrue(finding.faithful)

    def test_proper_nouns_are_preserved_without_complaint(self):
        for source, candidate in (
            ("SuNLEsS... BUT PEOPLE CALL Me Sunny.",
             "SuNLEsS... MAS AS PESSOAS ME CHAMAM DE Sunny."),
            ("ALRIGHT, SUNNY.", "TA BOM, SUNNY."),
        ):
            with self.subTest(source=source):
                self.assertTrue(_evaluate(source, candidate).faithful)

    def test_sfx_is_outside_the_semantic_workload(self):
        for text in ("TAK", "TUR", "TRUDGE", "STAGGER"):
            with self.subTest(text=text):
                finding = _evaluate(text, text, classification="sfx")
                self.assertTrue(finding.faithful)


class RetrySelectionTests(unittest.TestCase):
    """A fluent wrong candidate must lose to a faithful one, never the reverse."""

    def test_bad_first_candidate_is_replaced_by_the_faithful_retry(self):
        group = _group("COME BACK BEFORE HE ARRIVES.", "VOLTE DEPOIS QUE ELE CHEGAR.")
        translator = _ScriptedTranslator("VOLTE ANTES QUE ELE CHEGUE.")
        stats = {}
        validate_and_retry_translations(
            [group], translator, fidelity_stats=stats,
            fidelity_verifier=None,
        )
        self.assertTrue(group.translation_valid)
        self.assertEqual(group.translation, "VOLTE ANTES QUE ELE CHEGUE.")
        self.assertEqual(
            translator.calls[0]["validation_reason"].split(":", 1)[0],
            semantic_fidelity.TEMPORAL_RELATION_CHANGED,
        )

    def test_fluent_but_unfaithful_retry_does_not_replace_a_held_region(self):
        group = _group("COME BACK BEFORE HE ARRIVES.", "VOLTE DEPOIS QUE ELE CHEGAR.")
        translator = _ScriptedTranslator("VOLTE LOGO DEPOIS DE ELE CHEGAR.")
        validate_and_retry_translations([group], translator, fidelity_stats={})
        self.assertFalse(group.translation_valid)
        self.assertEqual(
            group.translation_final_reason, "semantic_fidelity_failed_after_retries"
        )

    def test_review_only_finding_renders_and_is_counted_separately(self):
        group = _group(P043_SOURCE, P043_ACCEPTED, classification="narration")
        stats = {}
        validate_and_retry_translations(
            [group], _ScriptedTranslator(), fidelity_stats=stats
        )
        self.assertTrue(group.translation_valid)
        self.assertEqual(group.translation, P043_ACCEPTED)
        self.assertTrue(group.semantic_review_reason.startswith(
            semantic_fidelity.SOURCE_OCR_SUSPICIOUS))
        self.assertEqual(stats.get("semantic_review"), 1)

    def test_retry_constraint_exists_for_every_reason_code(self):
        for code in semantic_fidelity.FIDELITY_REASON_CODES:
            with self.subTest(code=code):
                self.assertTrue(semantic_fidelity.retry_constraint(code))


class TranslationContextContractTests(unittest.TestCase):
    """Bounded context, provider-agnostic, and never mistaken for the target."""

    def test_every_provider_accepts_the_same_context_contract(self):
        for adapter in (translator_deepl.DeepLTranslator,
                        translator_nvidia.TranslatorNvidiaBatch):
            with self.subTest(adapter=adapter.__name__):
                self.assertTrue(hasattr(adapter, "set_session_context"))

    def test_deepl_declares_no_prompt_context_rather_than_assuming_support(self):
        adapter = translator_deepl.DeepLTranslator.__new__(translator_deepl.DeepLTranslator)
        adapter.stats = {}
        adapter.set_session_context(object())
        self.assertFalse(adapter.stats["context_enabled"])


if __name__ == "__main__":
    unittest.main()
