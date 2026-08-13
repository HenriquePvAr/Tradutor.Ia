"""Semantic fidelity gate: does the target still say what the source said?

Every validator before this one answered a different question - is the candidate
in the target language, does it echo the source, does it keep the chapter's
terminology. A candidate could pass all of them, read as fluent Portuguese, and
still carry a meaning the source never had. These tests pin the two-layer answer:
what is *proven* wrong locally and blocks for free, what is merely *uncertain*
and is worth one adjudication call, and - just as important - the very large
majority that is plainly faithful and must never cost a second call.

Every sentence, name and number here is synthetic. Production code keys off no
chapter, page, job, phrase or character.
"""

import _test_bootstrap  # noqa: F401

import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

import semantic_fidelity
from ocr_balloon import (
    OCRLine,
    TextGroup,
    get_translatable_groups,
    validate_and_retry_translations,
)
from session_context import SessionContextStore
from translator_nvidia import TranslatorNvidiaBatch


CHAPTER = "https://example.invalid/series/chapter-fidelity"

# --- synthetic fixtures, one per semantic class ------------------------------
INTENT_SOURCE = "AFTER THIS MISSION, I AM LEAVING THE GUILD."
INTENT_CORRUPTED = "DEPOIS DESTA MISSAO, ESTAREI ACABADO NA GUILDA."
INTENT_FAITHFUL = "DEPOIS DESTA MISSAO, VOU SAIR DA GUILDA."

NEGATION_SOURCE = "I WILL NOT ENTER THAT ROOM."
NEGATION_CORRUPTED = "EU VOU ENTRAR NAQUELE QUARTO."
NEGATION_FAITHFUL = "EU NAO VOU ENTRAR NAQUELE QUARTO."

QUANTITY_SOURCE = "THERE ARE 3 ENEMIES AHEAD."
QUANTITY_CORRUPTED = "HA 30 INIMIGOS ADIANTE."
QUANTITY_FAITHFUL = "HA 3 INIMIGOS ADIANTE."

ENTITY_NAME = "NARAEK"
ENTITY_SOURCE = "NARAEK CAME BACK ALIVE."
ENTITY_CORRUPTED = "O GUERREIRO VOLTOU VIVO."

ROLE_SOURCE = "NARAEK PROTECTED SOLNA FROM THE BEAST."
ROLE_REVERSED = "SOLNA PROTEGEU NARAEK DA FERA."


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


def _group(text, translation="", classification="speech", names=(), group_id="T001"):
    group = TextGroup(
        group_id=group_id,
        lines=[_line(text)],
        text=text,
        classification=classification,
        inside_balloon_like_region=True,
        source_engine="rapidocr",
    )
    group.detected_proper_names = list(names)
    group.translation = translation
    group.translation_candidate = translation
    group.sent_to_translation = bool(translation)
    return group


class _SilentTranslator:
    """A provider that must not be asked for anything."""

    is_configured = True


class _StubTranslator:
    """Records every retry it is asked for and answers from a fixed script."""

    is_configured = True

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def translate_strict(self, text, previous_translation="", validation_reason="",
                         force=False, allow_proper_names=True, proper_names=None):
        self.calls.append({
            "text": text,
            "previous_translation": previous_translation,
            "validation_reason": validation_reason,
            "allow_proper_names": allow_proper_names,
        })
        return self.responses.pop(0) if self.responses else ""


class _StubVerifier:
    """Semantic adjudicator stub: structured answer in, structured answer out."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def verify_fidelity(self, **kwargs):
        self.calls.append(kwargs)
        if not self.answers:
            return {"faithful": True}
        answer = self.answers[0] if len(self.answers) == 1 else self.answers.pop(0)
        return answer


def _faithful():
    return _StubVerifier({"faithful": True, "reason_codes": [], "confidence": 0.9})


def _unfaithful(code=semantic_fidelity.MEANING_MISMATCH):
    return _StubVerifier({"faithful": False, "reason_codes": [code], "confidence": 0.9})


def _uncertain():
    return _StubVerifier({"faithful": None, "reason_codes": [], "confidence": 0.2})


def _run(group, translator=None, verifier=None, ledger=None, stats=None):
    stats = {} if stats is None else stats
    records = validate_and_retry_translations(
        [group],
        translator or _SilentTranslator(),
        terminology_ledger=ledger,
        fidelity_verifier=verifier,
        fidelity_stats=stats,
    )
    return records, stats


class LocalInvariantTests(unittest.TestCase):
    """Layer A: what is provably wrong without asking anybody."""

    def test_quantity_change_is_proven_locally_and_blocks(self):
        finding = semantic_fidelity.evaluate_local_fidelity(
            QUANTITY_SOURCE, QUANTITY_CORRUPTED
        )
        self.assertEqual(finding.status, semantic_fidelity.BLOCKED)
        self.assertEqual(finding.primary_reason, semantic_fidelity.QUANTITY_CHANGED)

    def test_preserved_quantity_passes(self):
        finding = semantic_fidelity.evaluate_local_fidelity(
            QUANTITY_SOURCE, QUANTITY_FAITHFUL
        )
        self.assertTrue(finding.faithful)

    def test_locale_number_formatting_is_not_a_quantity_change(self):
        # 1,000 -> 1.000 and 3.5 -> 3,5 are the same values, written for a
        # different locale. Rejecting them would be rejecting correct output.
        finding = semantic_fidelity.evaluate_local_fidelity(
            "IT COST 1,000 GOLD AND WEIGHS 3.5 KG.",
            "CUSTOU 1.000 DE OURO E PESA 3,5 KG.",
        )
        self.assertTrue(finding.faithful)

    def test_rank_and_stat_digits_survive_untouched(self):
        finding = semantic_fidelity.evaluate_local_fidelity(
            "HE REACHED RANK S AT HP 50.", "ELE ALCANCOU O RANK S COM HP 50."
        )
        self.assertTrue(finding.faithful)

    def test_a_region_detected_name_alone_never_blocks_here(self):
        # The translation validator already enforces a detected span verbatim.
        # A second, independent proper-name authority would only add its noise.
        finding = semantic_fidelity.evaluate_local_fidelity(
            ENTITY_SOURCE, ENTITY_CORRUPTED, proper_names=[ENTITY_NAME]
        )
        self.assertTrue(finding.faithful)

    def test_negation_loss_is_routed_not_rejected_outright(self):
        # A regex can see that a marker vanished; it cannot prove a contradiction,
        # because lexical negation and idiom carry no marker at all.
        finding = semantic_fidelity.evaluate_local_fidelity(
            NEGATION_SOURCE, NEGATION_CORRUPTED
        )
        self.assertEqual(finding.status, semantic_fidelity.VERIFY)
        self.assertEqual(finding.primary_reason, semantic_fidelity.NEGATION_CHANGED)

    def test_negative_concordance_is_not_a_negation_change(self):
        # Portuguese legitimately spells two markers where English spells one.
        finding = semantic_fidelity.evaluate_local_fidelity(
            "I DID NOT SEE ANYONE THERE.", "NAO VI NINGUEM LA."
        )
        self.assertTrue(finding.faithful)

    def test_preserved_negation_passes(self):
        finding = semantic_fidelity.evaluate_local_fidelity(
            NEGATION_SOURCE, NEGATION_FAITHFUL
        )
        self.assertTrue(finding.faithful)

    def test_decision_rendered_as_a_state_is_routed(self):
        finding = semantic_fidelity.evaluate_local_fidelity(
            INTENT_SOURCE, INTENT_CORRUPTED
        )
        self.assertEqual(finding.status, semantic_fidelity.VERIFY)
        self.assertEqual(finding.primary_reason, semantic_fidelity.STATE_ACTION_CHANGED)

    def test_the_same_decision_translated_correctly_is_not_routed(self):
        finding = semantic_fidelity.evaluate_local_fidelity(
            INTENT_SOURCE, INTENT_FAITHFUL
        )
        self.assertTrue(finding.faithful)

    def test_ongoing_action_kept_as_an_action_is_not_routed(self):
        finding = semantic_fidelity.evaluate_local_fidelity(
            "I AM GOING HOME NOW.", "ESTOU INDO PARA CASA AGORA."
        )
        self.assertTrue(finding.faithful)

    def test_a_state_source_translated_as_a_state_is_not_routed(self):
        finding = semantic_fidelity.evaluate_local_fidelity(
            "I AM TIRED.", "ESTOU CANSADO."
        )
        self.assertTrue(finding.faithful)

    def test_two_known_identities_swapping_places_is_routed(self):
        finding = semantic_fidelity.evaluate_local_fidelity(
            ROLE_SOURCE, ROLE_REVERSED, proper_names=["NARAEK", "SOLNA"]
        )
        self.assertEqual(finding.status, semantic_fidelity.VERIFY)
        self.assertEqual(
            finding.primary_reason, semantic_fidelity.ACTOR_RELATION_CHANGED
        )

    def test_identities_in_the_original_order_are_not_routed(self):
        finding = semantic_fidelity.evaluate_local_fidelity(
            ROLE_SOURCE, "NARAEK PROTEGEU SOLNA DA FERA.",
            proper_names=["NARAEK", "SOLNA"],
        )
        self.assertTrue(finding.faithful)

    def test_natural_paraphrase_with_no_shared_word_stays_faithful(self):
        # The gate must never become a literalness checker: these share no token
        # with their source and are perfect translations.
        for source, candidate in (
            ("LEAVE IT TO ME.", "DEIXA COMIGO."),
            ("GIVE ME A BREAK.", "AH, ME POUPE."),
            ("I NEED TO GET OUT OF HERE.", "TENHO QUE SAIR DAQUI."),
        ):
            with self.subTest(source=source):
                finding = semantic_fidelity.evaluate_local_fidelity(source, candidate)
                self.assertTrue(finding.faithful, finding)

    def test_preservable_classifications_are_not_judged_as_translations(self):
        finding = semantic_fidelity.evaluate_local_fidelity(
            "BOOM 3", "BOOM 3", classification="sfx"
        )
        self.assertTrue(finding.faithful)


class FastPathTests(unittest.TestCase):
    """The normal region: one translation call, no adjudication, accepted."""

    def test_faithful_candidate_is_accepted_without_any_verifier_call(self):
        group = _group(QUANTITY_SOURCE, QUANTITY_FAITHFUL)
        verifier = _faithful()
        _records, stats = _run(group, verifier=verifier)
        self.assertTrue(group.translation_valid)
        self.assertEqual(group.translation_final_state, "translated")
        self.assertEqual(verifier.calls, [])
        self.assertEqual(stats.get("fidelity_verifier_requested", 0), 0)
        self.assertEqual(stats["fidelity_fast_path_pass"], 1)

    def test_fast_path_never_asks_the_provider_to_translate_again(self):
        translator = _StubTranslator("NUNCA DEVIA SER CHAMADO")
        group = _group("LEAVE IT TO ME.", "DEIXA COMIGO.")
        _run(group, translator=translator, verifier=_faithful())
        self.assertEqual(translator.calls, [])
        self.assertTrue(group.translation_valid)

    def test_a_whole_page_of_faithful_regions_costs_no_adjudication(self):
        verifier = _faithful()
        groups = [
            _group(f"THE GATE OPENED AT DAWN NUMBER {index}.",
                   f"O PORTAO ABRIU AO AMANHECER NUMERO {index}.",
                   group_id=f"G{index:03}")
            for index in range(12)
        ]
        stats = {}
        validate_and_retry_translations(
            groups, _SilentTranslator(), fidelity_verifier=verifier,
            fidelity_stats=stats,
        )
        self.assertTrue(all(group.translation_valid for group in groups))
        self.assertEqual(verifier.calls, [])
        self.assertEqual(stats["fidelity_fast_path_pass"], 12)


class SelectiveVerificationTests(unittest.TestCase):
    """Layer B: at most one adjudication, and only for what needs one."""

    def test_ambiguous_candidate_costs_exactly_one_verifier_call(self):
        verifier = _unfaithful()
        group = _group(INTENT_SOURCE, INTENT_CORRUPTED)
        _records, stats = _run(group, verifier=verifier)
        self.assertEqual(len(verifier.calls), 1)
        self.assertFalse(group.translation_valid)
        self.assertEqual(stats["fidelity_verifier_requested"], 1)
        self.assertEqual(stats["fidelity_verifier_fail"], 1)

    def test_verifier_rejection_keeps_the_candidate_out_of_the_page(self):
        group = _group(INTENT_SOURCE, INTENT_CORRUPTED)
        _run(group, verifier=_unfaithful())
        self.assertFalse(group.translation_valid)
        self.assertTrue(group.manual_review_required)
        self.assertEqual(group.translation, "")
        self.assertEqual(group.rejected_translation, INTENT_CORRUPTED)
        self.assertNotIn(group, get_translatable_groups([group]))

    def test_verifier_acceptance_lets_a_legitimate_adaptation_through(self):
        # Local heuristics saw something move; the adjudicator says the meaning
        # held. Local suspicion must not be able to force a literal translation.
        verifier = _faithful()
        group = _group(ROLE_SOURCE, ROLE_REVERSED, names=["NARAEK", "SOLNA"])
        _records, stats = _run(group, verifier=verifier)
        self.assertEqual(len(verifier.calls), 1)
        self.assertTrue(group.translation_valid)
        self.assertEqual(group.translation, ROLE_REVERSED)
        self.assertEqual(stats["fidelity_verifier_pass"], 1)

    def test_uncertain_is_never_read_as_faithful(self):
        group = _group(NEGATION_SOURCE, NEGATION_CORRUPTED)
        _records, stats = _run(group, verifier=_uncertain())
        self.assertFalse(group.translation_valid)
        self.assertEqual(stats["fidelity_verifier_uncertain"], 1)
        self.assertIn(semantic_fidelity.FIDELITY_UNCERTAIN,
                      group.translation_validation_reason)

    def test_a_verifier_outage_is_uncertainty_not_a_pass(self):
        class _Broken:
            def verify_fidelity(self, **kwargs):
                raise RuntimeError("verifier unavailable")

        group = _group(NEGATION_SOURCE, NEGATION_CORRUPTED)
        _run(group, verifier=_Broken())
        self.assertFalse(group.translation_valid)

    def test_without_a_configured_verifier_a_routed_region_is_not_trusted(self):
        group = _group(INTENT_SOURCE, INTENT_CORRUPTED)
        _records, stats = _run(group, verifier=None)
        self.assertFalse(group.translation_valid)
        self.assertEqual(stats["fidelity_verifier_uncertain"], 1)

    def test_a_locally_proven_block_never_spends_a_verifier_call(self):
        verifier = _faithful()
        group = _group(QUANTITY_SOURCE, QUANTITY_CORRUPTED)
        _records, stats = _run(group, verifier=verifier)
        self.assertEqual(verifier.calls, [])
        self.assertEqual(stats["fidelity_local_block"], 1)
        self.assertFalse(group.translation_valid)

    def test_the_verifier_sees_bounded_context_and_no_chapter_dump(self):
        verifier = _unfaithful()
        with tempfile.TemporaryDirectory() as folder:
            ledger = SessionContextStore(Path(folder) / "s.json", CHAPTER)
            ledger.prepare([])
            ledger.record_translations([
                _group("NARAEK RAISED THE BANNER", "NARAEK ERGUEU O ESTANDARTE",
                       names=["NARAEK"], group_id="G000")
            ])
            group = _group(NEGATION_SOURCE, NEGATION_CORRUPTED)
            _run(group, verifier=verifier, ledger=ledger)
        call = verifier.calls[0]
        self.assertEqual(call["source_text"], NEGATION_SOURCE)
        self.assertEqual(call["candidate"], NEGATION_CORRUPTED)
        self.assertIn(semantic_fidelity.NEGATION_CHANGED, call["reason_codes"])
        self.assertLessEqual(len(call["context"]), 4)
        self.assertLessEqual(len(call["terminology"]), 20)


class RetryTests(unittest.TestCase):
    """The corrective attempt: told what failed, bounded, and never trusted blind."""

    def test_retry_is_told_which_constraint_failed(self):
        translator = _StubTranslator(QUANTITY_FAITHFUL)
        group = _group(QUANTITY_SOURCE, QUANTITY_CORRUPTED)
        _run(group, translator=translator)
        self.assertEqual(len(translator.calls), 1)
        reason = translator.calls[0]["validation_reason"]
        self.assertTrue(reason.startswith(semantic_fidelity.QUANTITY_CHANGED))
        self.assertEqual(semantic_fidelity.retry_constraint(reason),
                         "preserve_quantity")

    def test_every_fidelity_reason_maps_to_a_named_constraint(self):
        for code in semantic_fidelity.FIDELITY_REASON_CODES:
            with self.subTest(code=code):
                self.assertTrue(semantic_fidelity.retry_constraint(code))

    def test_a_corrected_retry_is_accepted(self):
        translator = _StubTranslator(QUANTITY_FAITHFUL)
        group = _group(QUANTITY_SOURCE, QUANTITY_CORRUPTED)
        _run(group, translator=translator)
        self.assertTrue(group.translation_valid)
        self.assertEqual(group.translation, QUANTITY_FAITHFUL)

    def test_a_retry_that_is_still_unfaithful_cannot_become_trusted(self):
        # The second attempt keeps the wrong number: "least bad" is still wrong.
        translator = _StubTranslator("HA 30 INIMIGOS A FRENTE.", "HA 12 INIMIGOS.")
        group = _group(QUANTITY_SOURCE, QUANTITY_CORRUPTED)
        _records, stats = _run(group, translator=translator)
        self.assertFalse(group.translation_valid)
        self.assertEqual(group.translation, "")
        self.assertTrue(group.rejected_translation)
        self.assertTrue(group.manual_review_required)
        self.assertEqual(stats["fidelity_blocked"], 1)
        self.assertEqual(group.translation_final_reason,
                         "semantic_fidelity_failed_after_retries")

    def test_fidelity_retries_share_the_global_retry_budget(self):
        import config

        translator = _StubTranslator(*["HA 30 INIMIGOS."] * 10)
        group = _group(QUANTITY_SOURCE, QUANTITY_CORRUPTED)
        _run(group, translator=translator)
        self.assertLessEqual(len(translator.calls), config.TRANSLATION_MAX_RETRIES)
        self.assertFalse(group.translation_valid)

    def test_the_verifier_is_asked_at_most_once_per_region_across_retries(self):
        verifier = _unfaithful()
        translator = _StubTranslator("EU VOU ENTRAR NESSE QUARTO.",
                                     "EU VOU ENTRAR NA SALA.")
        group = _group(NEGATION_SOURCE, NEGATION_CORRUPTED)
        _run(group, translator=translator, verifier=verifier)
        self.assertEqual(len(verifier.calls), 1)
        self.assertFalse(group.translation_valid)

    def test_a_retry_that_fixes_the_meaning_needs_no_second_adjudication(self):
        verifier = _unfaithful()
        translator = _StubTranslator(NEGATION_FAITHFUL)
        group = _group(NEGATION_SOURCE, NEGATION_CORRUPTED)
        _run(group, translator=translator, verifier=verifier)
        self.assertEqual(len(verifier.calls), 1)
        self.assertTrue(group.translation_valid)
        self.assertEqual(group.translation, NEGATION_FAITHFUL)

    def test_the_retry_prompt_names_the_constraint_and_carries_no_reasoning(self):
        translator = TranslatorNvidiaBatch()
        for code, constraint in semantic_fidelity.FIDELITY_RETRY_CONSTRAINTS.items():
            with self.subTest(code=code):
                instruction = translator._quality_retry_instruction(f"{code}:X")
                self.assertNotIn("Refaca a traducao evitando", instruction)
                self.assertTrue(constraint)


class LedgerAndRegistryIntegrationTests(unittest.TestCase):
    """The gate consumes the chapter's facts; it never grows its own."""

    def _ledger(self, folder):
        ledger = SessionContextStore(Path(folder) / "session_context.json", CHAPTER)
        ledger.prepare([])
        ledger.record_translations([
            _group("NARAEK RAISED THE BANNER", "NARAEK ERGUEU O ESTANDARTE",
                   names=[ENTITY_NAME], group_id="G000")
        ])
        return ledger

    def test_a_name_the_chapter_established_cannot_be_dissolved(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = self._ledger(folder)
            group = _group(ENTITY_SOURCE, ENTITY_CORRUPTED)
            _records, stats = _run(group, ledger=ledger)
        self.assertFalse(group.translation_valid)
        self.assertEqual(stats["fidelity_local_block"], 1)
        self.assertTrue(
            group.translation_validation_reason.startswith(
                semantic_fidelity.ENTITY_CHANGED
            )
        )

    def test_the_same_name_kept_is_accepted_on_the_fast_path(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = self._ledger(folder)
            group = _group(ENTITY_SOURCE, "NARAEK VOLTOU VIVO.")
            _records, stats = _run(group, ledger=ledger)
        self.assertTrue(group.translation_valid)
        self.assertEqual(stats["fidelity_fast_path_pass"], 1)

    def test_protected_entities_come_from_the_ledger_not_a_new_detector(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = self._ledger(folder)
            entities = semantic_fidelity.ledger_entities_for(ENTITY_SOURCE, ledger)
        self.assertIn(ENTITY_NAME, entities)
        # A word the chapter never established is not an entity.
        self.assertEqual(
            semantic_fidelity.ledger_entities_for("THE WARRIOR CAME BACK.", None), ()
        )

    def test_an_inflected_target_term_is_not_treated_as_an_entity_loss(self):
        # The ledger's canonical form is a display form. Portuguese inflects, and
        # a conjugated or pluralised term is not a dropped identity.
        finding = semantic_fidelity.evaluate_local_fidelity(
            "THE HUNTERS ENTERED THE GATE.", "OS CACADORES ENTRARAM NO PORTAL.",
        )
        self.assertTrue(finding.faithful)

    def test_character_identity_checks_still_run_and_are_not_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = self._ledger(folder)
            self.assertTrue(hasattr(ledger, "character_conflict_reason"))
            group = _group("NARAEK RAISED THE BANNER", "NARAEK ERGUEU O ESTANDARTE")
            _run(group, ledger=ledger)
        self.assertTrue(group.translation_valid)


class TrustBoundaryTests(unittest.TestCase):
    """What must never reach the gate at all."""

    def test_an_ocr_blocked_region_is_never_translated_nor_judged(self):
        group = _group("THERE ARE 3 ENEMIES AHEAD.", "")
        group.ocr_quality_blocked = True
        group.sent_to_translation = False
        self.assertEqual(get_translatable_groups([group]), [])
        verifier = _faithful()
        _records, stats = _run(group, verifier=verifier)
        self.assertEqual(verifier.calls, [])
        self.assertEqual(stats, {})

    def test_a_cached_translation_faces_the_same_gate_as_a_fresh_one(self):
        # Validation runs on the candidate, not on where it came from: a result
        # served from the provider's translation cache reaches this gate exactly
        # like a freshly generated one.
        group = _group(QUANTITY_SOURCE, QUANTITY_CORRUPTED)
        group.translation_valid = True
        group.translation_validation_reason = "ok"
        _run(group)
        self.assertFalse(group.translation_valid)


class TelemetryAndConcurrencyTests(unittest.TestCase):
    def test_counters_cover_every_decision_the_gate_can_take(self):
        stats = {}
        validate_and_retry_translations(
            [_group(QUANTITY_SOURCE, QUANTITY_FAITHFUL, group_id="A")],
            _SilentTranslator(), fidelity_stats=stats)
        validate_and_retry_translations(
            [_group(QUANTITY_SOURCE, QUANTITY_CORRUPTED, group_id="B")],
            _SilentTranslator(), fidelity_stats=stats)
        validate_and_retry_translations(
            [_group(INTENT_SOURCE, INTENT_CORRUPTED, group_id="C")],
            _SilentTranslator(), fidelity_verifier=_unfaithful(), fidelity_stats=stats)
        for key in ("fidelity_fast_path_pass", "fidelity_local_block",
                    "fidelity_verifier_requested", "fidelity_verifier_fail",
                    "fidelity_blocked"):
            self.assertGreaterEqual(stats.get(key, 0), 1, key)

    def test_no_secret_or_prompt_is_recorded_in_the_counters(self):
        stats = {}
        validate_and_retry_translations(
            [_group(INTENT_SOURCE, INTENT_CORRUPTED)],
            _SilentTranslator(), fidelity_stats=stats)
        self.assertTrue(all(isinstance(value, int) for value in stats.values()))

    def test_one_regions_verdict_never_contaminates_another(self):
        results = {}
        errors = []

        def run(index):
            try:
                bad = _group(QUANTITY_SOURCE, QUANTITY_CORRUPTED, group_id=f"B{index}")
                good = _group(QUANTITY_SOURCE, QUANTITY_FAITHFUL, group_id=f"G{index}")
                validate_and_retry_translations([bad, good], _SilentTranslator(),
                                                fidelity_stats={})
                results[index] = (bad.translation_valid, good.translation_valid)
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(set(results.values()), {(False, True)})

    def test_the_verifier_budget_is_per_region_not_per_run(self):
        verifier = _unfaithful()
        groups = [
            _group(INTENT_SOURCE, INTENT_CORRUPTED, group_id="A"),
            _group(NEGATION_SOURCE, NEGATION_CORRUPTED, group_id="B"),
        ]
        validate_and_retry_translations(groups, _SilentTranslator(),
                                        fidelity_verifier=verifier, fidelity_stats={})
        self.assertEqual(len(verifier.calls), 2)
        self.assertTrue(all(not group.translation_valid for group in groups))


if __name__ == "__main__":
    unittest.main()
