"""Two defects the pipeline could not see, both proven against a real run.

#84F17 closed *recovery*: a review the reader cannot use now spends the one
retry the region already has.  #84F18 then ran that loop against the real
provider and it worked - every region it flagged recovered.  What it also
showed is that the flagging itself has two holes, and a region nobody flags is
never recovered:

* **SEMANTIC-DETECTION-GAP-001.**  A source whose word boundaries the OCR lost
  was translated into confident nonsense and filed ``semantic_clean``.  The
  ``source_segmentation_incomplete`` rule that exists for exactly this could not
  fire, because it was gated on ``group.repair_reason`` - and the segmentation
  repair that ran had recorded its provenance on the *lines*, so the group's own
  field was empty at the moment of the decision.  The gate asked "did the
  repair run" of the one object that could not answer.

  The fix does not chase the provenance.  It reads the residual directly: a
  glued run whose *ends* tile into closed-class English words is a run whose
  boundaries are known to have collapsed, whatever any provenance says.  Two
  such words are required and they must tile from one end, which is what
  separates ``AGATETHROUGHWHICH`` (``…THROUGH|WHICH``, tiling to the end) from
  ``RECOVERFROMASERIES``, where ``OVER`` is just a piece of ``RECOVER`` and the
  provider recovered the line correctly.

* **OCR-UI-PROVENANCE-001.**  The settings panel showed "paddle · Ativo" for a
  job that ran RapidOCR.  ``config.OCR_ENGINE`` is an *output* of the engine
  decision, written into the job process when a run starts; the UI process
  never starts a run, so it was reporting the packaging default as fact.

Every fixture below is the exact persisted text of the real #84F18 run
(job ``01a0ee89``), or synthetic.  No page, chapter or sentence is special-cased
in production code.
"""

import _test_bootstrap  # noqa: F401

import os
import unittest
from unittest import mock

import numpy as np

import semantic_fidelity
from config import BETA_OCR_ENGINE, effective_ocr_engine
from ocr_balloon import OCRLine, TextGroup, validate_and_retry_translations


# --- exact persisted #84F18 regions -----------------------------------------
# P65: the hard sentinel. The OCR lost the spaces, the segmentation repair ran
# at line level ("YOUBECOME" -> "YOU BE COME") and stopped there, and the
# provider found "AGATE" inside the residual and translated the gemstone.
P65_SOURCE = "..YOU BE COME AGATETHROUGHWHICH AMONSTERAPPEARSIN THEREALWORLD."
P65_CANDIDATE = (
    "...VOCÊ SE TORNA A GÁTETA POR MEIO DA QUAL UM MONSTRO "
    "APARECE NO MUNDO REAL."
)

# P26 BALAO_2: the same corpus, the same missing spaces - and a correct
# translation. This is the control that keeps the new rule from becoming the
# glued-token detector, which would bury the real defects.
P26_GLUED_BUT_RECOVERED_SOURCE = (
    "BACKTHEN,THEPLANET WASJUSTSTARTINGTO RECOVERFROMASERIES "
    "OFDEVASTATINGNATLRAL DISASTERSANDSUBSEQLENT RESOLRCEWARS."
)
P26_GLUED_BUT_RECOVERED_CANDIDATE = (
    "NAQUELA ÉPOCA, O PLANETA ESTAVA APENAS COMEÇANDO A SE RECUPERAR "
    "DE UMA SÉRIE DE DESASTRE NATURAIS DEVASTADORES E DAS GUERRAS POR "
    "RECURSOS QUE SE SEGUIRAM."
)
P26_GLUED_BUT_RECOVERED_SOURCE_2 = (
    "BUTWHENITSVICTIMS BEGANFALLINGINTOAN ENDLESS SLLMBER, "
    "THE WORLD TOOK NOTICE."
)
P26_GLUED_BUT_RECOVERED_CANDIDATE_2 = (
    "MAS QUANDO AS VÍTIMAS COMEÇARAM A CAIR EM UM SONO SEM FIM, "
    "O MUNDO PERCEBEU."
)
P27_MALFORMED_SOURCE = (
    "EVENTLALLY, THIS PHENOMENON CAME TO BE KNOWNASTHENIGHTMARE SPELL, "
    "AND IT WAS NO MERE DISEASE."
)
P27_MALFORMED_CANDIDATE = (
    "COM O TEMPO, ESSE FENÔMENO PASSO A SER CONHECIDO COMO “FEITIÇO DO "
    "PESADELO”, E NÃO ERA UMA SIMPLES DOENÇA."
)
P45_AMBIGUOUS_SOURCE = "NORTHERN QHADRANT SIEGE CAPITAL"
P45_AMBIGUOUS_CANDIDATE = "CAPITAL DO QUADRANTE NORTE EM SITIO"


def evaluate(source, candidate, **kwargs):
    return semantic_fidelity.evaluate_local_fidelity(
        source, candidate, classification="speech", **kwargs
    )


class SegmentationResidualTests(unittest.TestCase):
    """A source whose boundaries collapsed can never be called clean."""

    def test_p65_is_not_clean_even_with_no_repair_provenance(self):
        """The exact state of the real run: ``repair_reason`` empty, output nonsense."""
        finding = evaluate(P65_SOURCE, P65_CANDIDATE, source_repair_reason="")
        self.assertEqual(finding.status, semantic_fidelity.REVIEW)
        self.assertEqual(
            finding.primary_reason,
            semantic_fidelity.SOURCE_SEGMENTATION_INCOMPLETE,
        )

    def test_p65_reaches_the_existing_bounded_recovery_path(self):
        finding = evaluate(P65_SOURCE, P65_CANDIDATE, source_repair_reason="")
        self.assertTrue(semantic_fidelity.is_review_unusable(finding.reason()))
        self.assertTrue(semantic_fidelity.retry_constraint(finding.reason()))

    def test_the_collapsed_run_is_named_in_the_evidence(self):
        finding = evaluate(P65_SOURCE, P65_CANDIDATE, source_repair_reason="")
        self.assertIn("AGATETHROUGHWHICH", finding.critical_fact_mismatches)

    def test_the_provenance_gated_rule_still_fires_when_provenance_exists(self):
        """#84F14's rule is untouched: a run plus segmentation provenance still routes."""
        finding = evaluate(
            "THIS TAKEAFEWHOURSNOW",
            "ISSO LEVA ALGUMAS HORAS AGORA",
            source_repair_reason="segment_compact_english_word",
        )
        self.assertEqual(
            finding.primary_reason,
            semantic_fidelity.SOURCE_SEGMENTATION_INCOMPLETE,
        )


class SegmentationFalsePositiveTests(unittest.TestCase):
    """The real corpus is full of glued runs the provider reads correctly."""

    def test_a_glued_run_the_provider_recovered_is_left_alone(self):
        finding = evaluate(
            P26_GLUED_BUT_RECOVERED_SOURCE,
            P26_GLUED_BUT_RECOVERED_CANDIDATE,
            source_repair_reason="",
        )
        self.assertTrue(finding.faithful, finding.reason())

    def test_a_second_recovered_glued_run_is_left_alone(self):
        finding = evaluate(
            P26_GLUED_BUT_RECOVERED_SOURCE_2,
            P26_GLUED_BUT_RECOVERED_CANDIDATE_2,
            source_repair_reason="",
        )
        self.assertTrue(finding.faithful, finding.reason())

    def test_a_function_word_buried_inside_one_long_word_proves_nothing(self):
        """``OVER`` inside ``RECOVER`` is a substring, not a boundary."""
        self.assertEqual(
            semantic_fidelity.collapsed_source_runs("RECOVERFROMASERIES"), ()
        )

    def test_a_long_legitimate_term_is_never_a_collapsed_run(self):
        for term in (
            "EMERGENCY CONTAINMENT VAULT",
            "RESPONSIBILITIES",
            "COUNTERINTELLIGENCE",
            "UNCHARACTERISTICALLY",
        ):
            self.assertEqual(
                semantic_fidelity.collapsed_source_runs(term), (), term
            )

    def test_names_fantasy_terms_and_sfx_are_never_collapsed_runs(self):
        for term in (
            "SUNNY NEVERMORE",
            "NIGHTMARE SPELL",
            "SLEEPER AWAKENED SOVEREIGN",
            "KRAAAAKKKKBOOOOM",
            "AAAAAAAAAAAAAAAAH",
        ):
            self.assertEqual(
                semantic_fidelity.collapsed_source_runs(term), (), term
            )

    def test_a_single_tiling_word_is_not_enough_evidence(self):
        """``THEREALWORLD`` is ``THE|REAL|WORLD``, but only ``THE`` is proven."""
        self.assertEqual(
            semantic_fidelity.collapsed_source_runs("THEREALWORLDISGONE"), ()
        )


class MalformedPortugueseTests(unittest.TestCase):
    """Malformed PT-BR needs corroborated grammar, not broad spellchecking."""

    def test_p27_malformed_verb_form_is_not_clean(self):
        finding = evaluate(P27_MALFORMED_SOURCE, P27_MALFORMED_CANDIDATE)
        self.assertEqual(finding.status, semantic_fidelity.REVIEW)
        self.assertEqual(
            finding.primary_reason,
            semantic_fidelity.GRAMMAR_MALFORMED,
        )

    def test_p45_is_ambiguous_not_a_local_grammar_positive(self):
        finding = evaluate(P45_AMBIGUOUS_SOURCE, P45_AMBIGUOUS_CANDIDATE)
        self.assertTrue(finding.faithful, finding.reason())

    def test_uncommon_valid_ptbr_control_is_not_flagged(self):
        finding = evaluate(
            "THE ANCIENT CREATURE STAYED QUIET.",
            "A CRIATURA ANCESTRAL PERMANECEU QUIETA.",
        )
        self.assertTrue(finding.faithful, finding.reason())


class WholeCorpusNoiseTests(unittest.TestCase):
    """The rule must stay quiet on ordinary story text."""

    ORDINARY = (
        "I WILL NOT LET YOU DOWN.",
        "THE NEAREST AWAKENED IS THREE HOURS AWAY.",
        "SHE HAS BEEN THROUGH WORSE THAN THIS, AND SO HAVE WE.",
        "EVERYTHING THAT COULD HAVE GONE WRONG ALREADY HAS.",
        "PRECINCT 7 IS UNDER CONTAINMENT.",
    )

    def test_no_ordinary_sentence_is_flagged(self):
        for line in self.ORDINARY:
            self.assertEqual(semantic_fidelity.collapsed_source_runs(line), (), line)


class _ScriptedTranslator:
    """Answers retries from a fixed script and records every request."""

    is_configured = True

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def translate_strict(self, text, previous_translation="", validation_reason="",
                         force=False, allow_proper_names=True, proper_names=None,
                         **kwargs):
        self.calls.append({"text": text, "reason": validation_reason})
        return self.responses.pop(0) if self.responses else ""


def _group(text, translation):
    polygon = np.array([[10, 10], [210, 10], [210, 45], [10, 45]], dtype=np.int32)
    group = TextGroup(
        group_id="R001",
        lines=[OCRLine(text=text, confidence=0.92, polygon=polygon,
                       box=(10, 10, 200, 35), raw_text=text, engine="rapidocr",
                       page=1)],
        text=text,
        classification="speech",
        inside_balloon_like_region=True,
        source_engine="rapidocr",
    )
    group.translation = translation
    group.translation_candidate = translation
    group.sent_to_translation = True
    return group


class CollapsedSourceReachesRecoveryTests(unittest.TestCase):
    """The new reason feeds the bounded path #84F17 already built - no new one."""

    GOOD = "...VOCÊ SE TORNA UM PORTAL PELO QUAL UM MONSTRO SURGE NO MUNDO REAL."

    def test_ambiguous_source_stays_unusable_even_when_retry_reads_well(self):
        group = _group(P65_SOURCE, P65_CANDIDATE)
        translator = _ScriptedTranslator(self.GOOD)
        validate_and_retry_translations([group], translator, fidelity_stats={})
        self.assertEqual(len(translator.calls), 1)
        self.assertTrue(translator.calls[0]["reason"].startswith(
            semantic_fidelity.SOURCE_SEGMENTATION_INCOMPLETE))
        self.assertEqual(group.translation, P65_CANDIDATE)
        self.assertTrue(semantic_fidelity.is_review_unusable(
            group.semantic_review_reason))

    def test_bad_first_good_second_selects_the_second_candidate_for_clean_source(self):
        group = _group(
            P27_MALFORMED_SOURCE,
            P27_MALFORMED_CANDIDATE,
        )
        good = (
            "COM O TEMPO, ESSE FENÔMENO PASSOU A SER CONHECIDO COMO "
            "“FEITIÇO DO PESADELO”, E NÃO ERA UMA SIMPLES DOENÇA."
        )
        translator = _ScriptedTranslator(good)
        validate_and_retry_translations([group], translator, fidelity_stats={})
        self.assertEqual(len(translator.calls), 1)
        self.assertTrue(translator.calls[0]["reason"].startswith(
            semantic_fidelity.GRAMMAR_MALFORMED))
        self.assertEqual(group.translation, good)
        self.assertTrue(group.translation_valid)
        self.assertFalse(group.semantic_review_reason)

    def test_all_bad_stays_unusable_and_is_never_clean(self):
        group = _group(P65_SOURCE, P65_CANDIDATE)
        translator = _ScriptedTranslator(
            "...VOCÊ SE TORNA A GÁTETA PELA QUAL UM MONSTRO APARECE NO MUNDO REAL.")
        validate_and_retry_translations([group], translator, fidelity_stats={})
        self.assertTrue(semantic_fidelity.is_review_unusable(
            group.semantic_review_reason))
        self.assertEqual(group.translation, P65_CANDIDATE)

    def test_the_region_never_costs_more_than_one_retry(self):
        group = _group(P65_SOURCE, P65_CANDIDATE)
        translator = _ScriptedTranslator("AINDA A GÁTETA POR MEIO DA QUAL ALGO VEM.")
        validate_and_retry_translations([group], translator, fidelity_stats={})
        self.assertEqual(len(translator.calls), 1)
        self.assertEqual(group.initial_translation_calls + group.selective_retry_calls, 2)

    def test_the_raw_source_is_never_rewritten_by_the_new_reason(self):
        group = _group(P65_SOURCE, P65_CANDIDATE)
        validate_and_retry_translations(
            [group], _ScriptedTranslator(self.GOOD), fidelity_stats={})
        self.assertEqual(group.text, P65_SOURCE)


class OcrEngineProvenanceTests(unittest.TestCase):
    """The UI must name the engine the run will really use."""

    def test_a_stale_paddle_process_config_cannot_lie(self):
        with mock.patch.dict(os.environ, {"OCR_ENGINE": "paddle"}, clear=False):
            os.environ.pop("TRADUTOR_OCR_ENGINE_OVERRIDE", None)
            self.assertEqual(effective_ocr_engine(), "rapidocr")

    def test_the_beta_default_engine_is_rapidocr(self):
        self.assertEqual(BETA_OCR_ENGINE, "rapidocr")

    def test_an_explicit_supported_override_is_honoured(self):
        with mock.patch.dict(
            os.environ, {"TRADUTOR_OCR_ENGINE_OVERRIDE": "paddle"}, clear=False
        ):
            self.assertEqual(effective_ocr_engine(), "paddle")

    def test_an_unsupported_override_falls_back_to_the_beta_engine(self):
        with mock.patch.dict(
            os.environ, {"TRADUTOR_OCR_ENGINE_OVERRIDE": "tesseract"}, clear=False
        ):
            self.assertEqual(effective_ocr_engine(), "rapidocr")


if __name__ == "__main__":
    unittest.main()
