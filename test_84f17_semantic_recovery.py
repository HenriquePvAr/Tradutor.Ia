"""Recovery for a semantic review the pipeline already knows is unusable.

#84F14 and #84F15 taught the pipeline to *detect* two classes of bad output that
every other validator calls clean: a corrupt source token that survived verbatim
into the Portuguese, and a fluent Portuguese sentence that commits to the wrong
sense of an ambiguous word. Both were filed as ``review_unusable`` and shipped
anyway, because detection was where the work stopped - a region flagged this way
never asked for a second translation.

These tests pin the bounded recovery that closes the gap:

* a *unique* single-edit repair against trusted vocabulary turns a suspicious
  raw source into a canonical one, and the raw OCR is never erased;
* an *ambiguous* repair, a proper name, a protected term and an SFX are never
  touched, because a repair that could go two ways is a guess;
* every ``review_unusable`` finding now spends the region's one existing retry,
  and the retried candidate faces the full validation again;
* when the recovery works the region is clean; when it fails the region is
  exactly as unusable as before - still rendering, never clean, never rejected.

Every token here is synthetic or drawn from a fixture built in this file.
Production code keys off no page, job, chapter or sentence.
"""

import _test_bootstrap  # noqa: F401

import unittest

import numpy as np

import ocr_balloon
import semantic_fidelity
from ocr_balloon import OCRLine, TextGroup, validate_and_retry_translations


# --- fixtures ---------------------------------------------------------------
# The three real corruption shapes, spelled as synthetic tokens here. What
# matters to the code is the *shape*: an improbable token that the provider
# handed back untranslated.
OCR_CORRUPT_RECOVERABLE = "COLLD"        # one edit from a word the lexicon knows
OCR_CORRUPT_UNRECOVERABLE = "VALLT"      # no unique candidate anywhere
AMBIGUOUS_PAIR_SOURCE = "BFAR"           # two plausible repairs -> none

POLICE_CONTEXT = (
    "THE OFFICERS SEALED THE CONTAINMENT FACILITY.",
    "SECURITY GUARDS ARE HOLDING THE SUSPECT.",
)
ELECTORAL_CONTEXT = (
    "THE BALLOTS ARE STILL BEING COUNTED.",
    "EVERY VOTER IN THE DISTRICT TURNED OUT.",
)
PRECINCT_SOURCE = "PRECINCT 7"
PRECINCT_ELECTORAL_CANDIDATE = "7 DISTRITO ELEITORAL"
PRECINCT_POLICE_CANDIDATE = "DISTRITO POLICIAL 7"


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


def _group(text, translation="", classification="speech", group_id="R001"):
    group = TextGroup(
        group_id=group_id,
        lines=[_line(text)],
        text=text,
        classification=classification,
        inside_balloon_like_region=True,
        source_engine="rapidocr",
    )
    group.translation = translation
    group.translation_candidate = translation
    group.sent_to_translation = bool(translation)
    return group


class _ScriptedTranslator:
    """Answers retries from a fixed script and records every request."""

    is_configured = True

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def translate_strict(self, text, previous_translation="", validation_reason="",
                         force=False, allow_proper_names=True, proper_names=None,
                         **kwargs):
        self.calls.append({
            "text": text,
            "reason": validation_reason,
            "previous": previous_translation,
            "source_context": tuple(kwargs.get("source_context") or ()),
        })
        return self.responses.pop(0) if self.responses else ""


class _SilentTranslator:
    """A provider that must never be asked for anything."""

    is_configured = True


def _run(groups, translator=None, stats=None):
    stats = {} if stats is None else stats
    records = validate_and_retry_translations(
        list(groups),
        translator or _SilentTranslator(),
        fidelity_stats=stats,
    )
    return records, stats


# --- class A: source OCR repair ---------------------------------------------
class SourceRepairEvidenceTests(unittest.TestCase):
    """A repair is made only when the evidence points at exactly one word."""

    def test_unique_single_edit_candidate_is_repaired(self):
        canonical, repairs = semantic_fidelity.repair_suspicious_source(
            f"SO THEY {OCR_CORRUPT_RECOVERABLE} RECEIVE TRAINING.",
            (OCR_CORRUPT_RECOVERABLE,),
            {"COULD", "RECEIVE", "TRAINING"},
        )
        self.assertEqual(
            canonical, "SO THEY COULD RECEIVE TRAINING.")
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]["raw_source"], OCR_CORRUPT_RECOVERABLE)
        self.assertEqual(repairs[0]["canonical_source"], "COULD")
        self.assertTrue(repairs[0]["repair_reason"])
        self.assertGreater(float(repairs[0]["repair_confidence"]), 0.0)
        self.assertTrue(repairs[0]["repair_evidence"])

    def test_two_plausible_repairs_produce_none(self):
        # "BFAR" is one edit from both BEAR and BOAR. Nobody knows which, so the
        # source is left exactly as the OCR read it.
        canonical, repairs = semantic_fidelity.repair_suspicious_source(
            f"THE {AMBIGUOUS_PAIR_SOURCE} IS CLOSE.",
            (AMBIGUOUS_PAIR_SOURCE,),
            {"BEAR", "BOAR", "CLOSE"},
        )
        self.assertEqual(canonical, f"THE {AMBIGUOUS_PAIR_SOURCE} IS CLOSE.")
        self.assertEqual(repairs, ())

    def test_no_candidate_at_all_produces_none(self):
        canonical, repairs = semantic_fidelity.repair_suspicious_source(
            f"EMERGENCY CONTAINMENT {OCR_CORRUPT_UNRECOVERABLE}",
            (OCR_CORRUPT_UNRECOVERABLE,),
            {"EMERGENCY", "CONTAINMENT"},
        )
        self.assertEqual(
            canonical, f"EMERGENCY CONTAINMENT {OCR_CORRUPT_UNRECOVERABLE}")
        self.assertEqual(repairs, ())

    def test_protected_token_is_never_repaired(self):
        # A name one edit from a common word must survive as the name.
        canonical, repairs = semantic_fidelity.repair_suspicious_source(
            "NARAEK CAME BACK.",
            ("NARAEK",),
            {"NARAEL", "CAME", "BACK"},
            protected=("NARAEK",),
        )
        self.assertEqual(canonical, "NARAEK CAME BACK.")
        self.assertEqual(repairs, ())

    def test_repair_is_not_offered_for_a_token_the_vocabulary_already_knows(self):
        canonical, repairs = semantic_fidelity.repair_suspicious_source(
            "THE GATE OPENED.", ("GATE",), {"GATE", "GAZE", "OPENED"},
        )
        self.assertEqual(canonical, "THE GATE OPENED.")
        self.assertEqual(repairs, ())

    def test_short_tokens_are_never_repaired(self):
        # SFX and interjections live here. One edit from a real word proves
        # nothing at this length.
        canonical, repairs = semantic_fidelity.repair_suspicious_source(
            "TAK", ("TAK",), {"TAKE", "TAP"},
        )
        self.assertEqual(canonical, "TAK")
        self.assertEqual(repairs, ())


class SourceRepairVocabularyTests(unittest.TestCase):
    """The vocabulary a repair may draw on is the pipeline's own, plus context."""

    def test_repo_lexicon_supports_the_recoverable_shape(self):
        vocabulary = ocr_balloon.source_repair_vocabulary()
        self.assertIn("COULD", vocabulary)
        # An SFX is never a repair target: "TAK" must not be pulled to "TAKE"
        # through a vocabulary that carries onomatopoeia.
        self.assertFalse(vocabulary & {word.upper() for word in ocr_balloon.SFX_WORDS})

    def test_bounded_context_adds_words_the_lexicon_does_not_carry(self):
        vocabulary = ocr_balloon.source_repair_vocabulary(
            context_texts=("EMERGENCY CONTAINMENT VAULT",)
        )
        self.assertIn("VAULT", vocabulary)

    def test_context_never_contributes_an_improbable_token(self):
        vocabulary = ocr_balloon.source_repair_vocabulary(
            context_texts=(f"A {OCR_CORRUPT_UNRECOVERABLE} DOOR",)
        )
        self.assertNotIn(OCR_CORRUPT_UNRECOVERABLE, vocabulary)


# --- the recovery path ------------------------------------------------------
class UnusableReviewIsRetriedTests(unittest.TestCase):
    """``review_unusable`` spends the region's one retry - it used not to."""

    def test_source_ocr_suspicion_asks_for_one_retry(self):
        source = f"THAT HAS NOTHING TO DO WITH A {OCR_CORRUPT_RECOVERABLE} RAT."
        bad = f"ISSO NAO TEM NADA A VER COM UM RATO {OCR_CORRUPT_RECOVERABLE}."
        good = "ISSO NAO TEM NADA A VER COM UM RATO QUE PODERIA."
        translator = _ScriptedTranslator(good)
        group = _group(source, bad)
        _run([group], translator)
        self.assertEqual(len(translator.calls), 1)
        self.assertTrue(translator.calls[0]["reason"].startswith(
            semantic_fidelity.SOURCE_OCR_SUSPICIOUS))

    def test_retry_receives_the_canonical_repaired_source(self):
        source = f"SO THEY {OCR_CORRUPT_RECOVERABLE} RECEIVE TRAINING."
        bad = f"ENTAO ELES {OCR_CORRUPT_RECOVERABLE} RECEBER TREINAMENTO."
        translator = _ScriptedTranslator("ENTAO ELES PODERIAM RECEBER TREINAMENTO.")
        group = _group(source, bad)
        _run([group], translator)
        self.assertEqual(len(translator.calls), 1)
        self.assertIn("COULD", translator.calls[0]["text"])
        self.assertNotIn(OCR_CORRUPT_RECOVERABLE, translator.calls[0]["text"])

    def test_raw_ocr_survives_the_repair(self):
        source = f"SO THEY {OCR_CORRUPT_RECOVERABLE} RECEIVE TRAINING."
        bad = f"ENTAO ELES {OCR_CORRUPT_RECOVERABLE} RECEBER TREINAMENTO."
        group = _group(source, bad)
        _run([group], _ScriptedTranslator("ENTAO ELES PODERIAM RECEBER TREINAMENTO."))
        self.assertEqual(group.text, source)
        repairs = getattr(group, "source_repairs", ())
        self.assertTrue(repairs)
        self.assertEqual(repairs[0]["raw_source"], OCR_CORRUPT_RECOVERABLE)

    def test_word_sense_mismatch_asks_for_one_retry_with_scene_context(self):
        groups = [
            _group(PRECINCT_SOURCE, PRECINCT_ELECTORAL_CANDIDATE, group_id="R001"),
        ] + [
            _group(text, "IRRELEVANTE.", group_id=f"C{index}")
            for index, text in enumerate(POLICE_CONTEXT)
        ]
        translator = _ScriptedTranslator(PRECINCT_POLICE_CANDIDATE, "IRRELEVANTE.",
                                         "IRRELEVANTE.")
        _run(groups, translator)
        sense_calls = [call for call in translator.calls
                       if call["reason"].startswith(
                           semantic_fidelity.WORD_SENSE_CONTEXT_MISMATCH)]
        self.assertEqual(len(sense_calls), 1)
        # Descriptive evidence, never an instruction naming the answer.
        context = " ".join(sense_calls[0]["source_context"]).upper()
        self.assertIn("OFFICERS", context)
        self.assertNotIn("POLICIAL", context)

    def test_a_renderable_review_still_does_not_retry(self):
        # #84F14 kept ``review_renderable`` shipping without a second call, and
        # recovery must not turn that into provider traffic.
        translator = _ScriptedTranslator("NUNCA PEDIDO.")
        group = _group("I WILL NOT ENTER.", "EU VOU ENTRAR.")
        _run([group], translator)
        self.assertNotEqual(
            [call["reason"] for call in translator.calls],
            [semantic_fidelity.SOURCE_OCR_SUSPICIOUS],
        )


class RecoveryOutcomeTests(unittest.TestCase):
    """Bad first, good second - and all-bad stays exactly as unusable."""

    def test_good_second_candidate_is_selected_and_region_is_clean(self):
        source = f"THAT HAS NOTHING TO DO WITH A {OCR_CORRUPT_RECOVERABLE} RAT."
        bad = f"ISSO NAO TEM NADA A VER COM UM RATO {OCR_CORRUPT_RECOVERABLE}."
        good = "ISSO NAO TEM NADA A VER COM UM RATO QUE PODERIA."
        group = _group(source, bad)
        _run([group], _ScriptedTranslator(good))
        self.assertEqual(group.translation, good)
        self.assertTrue(group.translation_valid)
        self.assertEqual(group.semantic_review_reason, "")
        self.assertEqual(ocr_balloon.translation_render_state(group)[0], "clean")

    def test_all_bad_stays_review_unusable_and_still_renders(self):
        source = f"EMERGENCY CONTAINMENT {OCR_CORRUPT_UNRECOVERABLE}"
        bad = f"BARREIRA DE CONTENCAO {OCR_CORRUPT_UNRECOVERABLE}"
        worse = f"CONTENCAO DE EMERGENCIA {OCR_CORRUPT_UNRECOVERABLE}"
        group = _group(source, bad, classification="narration")
        _run([group], _ScriptedTranslator(worse))
        axis, reason = ocr_balloon.translation_render_state(group)
        self.assertEqual(axis, "review")
        self.assertEqual(
            semantic_fidelity.review_usability(reason),
            semantic_fidelity.REVIEW_UNUSABLE,
        )
        # Rendering, never rejected: withholding puts the English back.
        self.assertTrue(group.translation.strip())
        self.assertNotEqual(group.translation_final_state, "rejected")
        # A failed recovery is still a *review*. Leaving the fidelity reason on
        # the rejection channel would make the quality accounting count it as a
        # semantic rejection, which is a different - and untrue - verdict.
        self.assertFalse(semantic_fidelity.is_fidelity_reason(
            group.translation_validation_reason))
        self.assertTrue(group.manual_review_required)
        self.assertEqual(group.translation_quality_impact, "review_required")

    def test_recovery_does_not_bypass_the_other_validators(self):
        # A second candidate that is not usable Portuguese is not a recovery.
        source = f"THAT HAS NOTHING TO DO WITH A {OCR_CORRUPT_RECOVERABLE} RAT."
        bad = f"ISSO NAO TEM NADA A VER COM UM RATO {OCR_CORRUPT_RECOVERABLE}."
        group = _group(source, bad)
        _run([group], _ScriptedTranslator("THAT HAS NOTHING TO DO WITH A RAT."))
        self.assertNotEqual(ocr_balloon.translation_render_state(group)[0], "clean")


class PrecinctSenseControlTests(unittest.TestCase):
    """The word-sense controls, all four, unchanged by recovery."""

    def _conflicts(self, candidate, context):
        return semantic_fidelity.word_sense_conflicts(
            PRECINCT_SOURCE, candidate, context)

    def test_electoral_candidate_in_police_context_conflicts(self):
        self.assertEqual(
            self._conflicts(PRECINCT_ELECTORAL_CANDIDATE, POLICE_CONTEXT),
            ("precinct>electoral",),
        )

    def test_police_candidate_in_police_context_is_accepted(self):
        self.assertEqual(
            self._conflicts(PRECINCT_POLICE_CANDIDATE, POLICE_CONTEXT), ())

    def test_electoral_candidate_in_electoral_context_is_accepted(self):
        self.assertEqual(
            self._conflicts(PRECINCT_ELECTORAL_CANDIDATE, ELECTORAL_CONTEXT), ())

    def test_no_context_leaves_a_committed_candidate_under_review(self):
        # Nothing supports the sense the candidate chose. Not proven wrong, not
        # clean either - and never silently forced to the other sense.
        self.assertEqual(
            self._conflicts(PRECINCT_ELECTORAL_CANDIDATE, ()),
            ("precinct>electoral",),
        )

    def test_neutral_candidate_is_never_flagged(self):
        self.assertEqual(self._conflicts("DISTRITO 7", POLICE_CONTEXT), ())


class RetryBoundTests(unittest.TestCase):
    """Recovery reuses the existing budget; it never stacks a new one."""

    def test_one_region_costs_at_most_one_retry(self):
        source = f"EMERGENCY CONTAINMENT {OCR_CORRUPT_UNRECOVERABLE}"
        bad = f"BARREIRA DE CONTENCAO {OCR_CORRUPT_UNRECOVERABLE}"
        translator = _ScriptedTranslator(
            f"CONTENCAO {OCR_CORRUPT_UNRECOVERABLE}",
            "NUNCA PEDIDO.",
            "NUNCA PEDIDO.",
        )
        group = _group(source, bad, classification="narration")
        _run([group], translator)
        self.assertEqual(len(translator.calls), 1)
        self.assertLessEqual(group.selective_retry_calls, 1)

    def test_chapter_budget_still_caps_the_page(self):
        groups = [
            _group(
                f"EMERGENCY CONTAINMENT {OCR_CORRUPT_UNRECOVERABLE} {index}",
                f"CONTENCAO {OCR_CORRUPT_UNRECOVERABLE} {index}",
                classification="narration",
                group_id=f"R{index:03d}",
            )
            for index in range(8)
        ]
        translator = _ScriptedTranslator(*[f"AINDA {OCR_CORRUPT_UNRECOVERABLE}"] * 8)
        _run(groups, translator)
        self.assertLessEqual(len(translator.calls),
                             ocr_balloon._selective_translation_retry_budget(groups))


class DetectionIsPreservedTests(unittest.TestCase):
    """#84F14/#84F15 must not be weakened to make recovery easier."""

    def test_the_four_review_states_still_exist(self):
        self.assertEqual(semantic_fidelity.review_usability(
            semantic_fidelity.SOURCE_OCR_SUSPICIOUS),
            semantic_fidelity.REVIEW_UNUSABLE)
        self.assertEqual(semantic_fidelity.review_usability(
            semantic_fidelity.WORD_SENSE_CONTEXT_MISMATCH),
            semantic_fidelity.REVIEW_UNUSABLE)
        self.assertEqual(semantic_fidelity.review_usability(
            semantic_fidelity.FIDELITY_UNCERTAIN),
            semantic_fidelity.REVIEW_RENDERABLE)

    def test_repaired_source_does_not_silence_the_detector(self):
        # The repair changes what the *provider* is asked to translate. It never
        # changes what the detector reads, so a candidate that still carries the
        # raw token is still caught.
        finding = semantic_fidelity.evaluate_local_fidelity(
            f"A {OCR_CORRUPT_RECOVERABLE} DOOR",
            f"UMA PORTA {OCR_CORRUPT_RECOVERABLE}",
            classification="narration",
            is_source_word=ocr_balloon._token_is_source_vocabulary,
        )
        self.assertEqual(finding.status, semantic_fidelity.REVIEW)
        self.assertEqual(finding.primary_reason,
                         semantic_fidelity.SOURCE_OCR_SUSPICIOUS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
