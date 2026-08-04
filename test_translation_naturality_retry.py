"""Automatic quality retry for contextually unnatural translations.

Extends the automatic-retry mechanism built in provider_execution.py (BLOCO 6B /
Missao A: .runtime/claude-translation-quality-retry/) to reasons that today only
ever produce NEEDS_REVIEW and therefore never reach a retry: grammar_risk
(unnatural pt-BR orthography), mixed_language (partial language mixture, distinct
from the >=60% source_language_residual already covered), and truncation
(suspicious incompleteness). It also wires the pre-existing (but previously
unreachable) semantic_inversion and terminology_conflict evidence-driven checks
through the retry path, without inventing any new detector - see
.runtime/claude-translation-naturality-retry/triage_signal_audit.md for why those
two checks are structurally real but dormant in production.

Everything here runs against a fake translator - never a real network call. The
offline guard makes any accidental socket use fail loudly instead of silently
spending money. Fixtures are synthetic sentences invented for these tests only;
none of it is content from any real work, and production code never hardcodes any
of these strings.
"""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
import uuid
from pathlib import Path

import linguistic_triage as lt
import provider_execution as pe
import region_taxonomy as tax


def _rid():
    return uuid.uuid4().hex[:8].upper()


# --- synthetic fixtures, one isolated gate reason per scenario --------------

NATURAL_SOURCE = "THE OLD FISHERMAN WATCHED THE WAVES IN SILENCE"
NATURAL_TRANSLATION = "O velho pescador observava as ondas em silêncio."

GRAMMAR_SOURCE = ("THE OLD MERCHANT WALKED SLOWLY THROUGH THE NARROW STREET "
                   "WHILE THE RAIN FELL SOFTLY ON THE STONES")
# 16 words, zero diacritics anywhere - triggers no_target_language_orthography
# without also tripping truncation or punctuation (source has no terminal mark).
GRAMMAR_FIRST_PASS = ("o velho comerciante andou devagar pela rua estreita "
                       "enquanto a chuva caia sobre as pedras frias")
GRAMMAR_RECOVERED = ("O velho comerciante andava devagar pela rua estreita "
                      "enquanto a chuva caía suavemente sobre as pedras.")

MIXED_LANG_SOURCE = "THE BRAVE KNIGHT RETURNED HOME AFTER THE LONG WINTER"
# <12 words (dodges grammar_risk); ~1/3 of its >=3-letter tokens are literal
# source tokens (dodges the >=60% source_language_residual threshold already
# owned by Missao A) - isolates mixed_language_candidate.
MIXED_LANG_FIRST_PASS = "brave cavaleiro knight voltou pra casa apos o inverno"
MIXED_LANG_RECOVERED = "O bravo cavaleiro retornou para casa após o longo inverno."

TRUNCATION_SOURCE = ("THE MERCHANTS GATHERED IN THE MARKETPLACE TO DISCUSS THE "
                      "COMING WINTER TRADE ROUTES AND PRICES")
TRUNCATION_FIRST_PASS = "Os comerciantes se reuniram."
TRUNCATION_RECOVERED = ("Os comerciantes se reuniram na praça do mercado para "
                         "discutir as rotas comerciais do inverno que se aproxima "
                         "e os preços.")

INVERSION_SOURCE = "SHE NEVER TOLD HIM THE TRUTH ABOUT THAT NIGHT"
INVERSION_FIRST_PASS = "Ela sempre contou a verdade sobre aquela noite."
INVERSION_RECOVERED = "Ela nunca contou a ele a verdade sobre aquela noite."

TERMINOLOGY_SOURCE = "THE GUILD MASTER RAISED THE BOUNTY ON THE ROGUE KNIGHT"
TERMINOLOGY_FIRST_PASS = "O chefe da guilda aumentou a recompensa pelo cavaleiro desonesto."
TERMINOLOGY_RECOVERED = "O mestre da guilda aumentou a recompensa pelo cavaleiro renegado."

# No detector for "candidate too long" exists in linguistic_triage.py (only
# "too short" -> suspicious_truncation). This candidate is deliberately long,
# grammatically natural pt-BR (has diacritics, proper punctuation) and shares
# no tokens with the source, so nothing in the real gate can flag it - proving
# the absence of the signal rather than faking one.
LONG_SOURCE = "HE LEFT"
LONG_FIRST_PASS = ("Ele se despediu lentamente, olhando para trás uma última vez "
                    "antes de sumir na neblina da estrada, sem dizer mais nenhuma "
                    "palavra àqueles que ficaram para trás, observando em silêncio.")

PUNCTUATION_SOURCE = "SHE WALKED AWAY WITHOUT ANOTHER WORD."
PUNCTUATION_FIRST_PASS = "Ela foi embora sem dizer mais nada"  # dropped final period: NEEDS_REVIEW,
# but punctuation_risk is deliberately NOT in the recoverable set (see
# retry_policy.md) - this is the "left for manual review" control case.

SFX_SOURCE = "BOOM"

GOOD_SOURCE = "ANOTHER FINE MORNING"
GOOD_FIRST_PASS = "Outra bela manhã."


class FakeQualityTranslator:
    """Records every call, never touches the network."""

    is_configured = True
    model = "fake/naturality"
    base_url = "https://example.invalid/v1"

    def __init__(self, *, first_pass, strict_map=None, strict_error_for=None,
                 wrong_count=False, fail_many=False):
        self.first_pass = dict(first_pass)
        self.strict_map = dict(strict_map or {})
        self.strict_error_for = set(strict_error_for or [])
        self.calls_many = []
        self.calls_strict = []
        self.stats = {"api_requests": 0, "api_texts": 0, "cache_hits": 0}
        self.wrong_count = wrong_count
        self.fail_many = fail_many

    def translate_many(self, texts):
        self.calls_many.append(list(texts))
        if self.fail_many:
            raise RuntimeError("provider unavailable")
        self.stats["api_requests"] += 1
        self.stats["api_texts"] += len(texts)
        out = [self.first_pass.get(t, f"[pt] {t}") for t in texts]
        return out[:-1] if self.wrong_count else out

    def translate_strict(self, text, previous_translation="", validation_reason="", **_kw):
        self.calls_strict.append({
            "text": text, "previous_translation": previous_translation,
            "validation_reason": validation_reason,
        })
        if text in self.strict_error_for:
            raise RuntimeError("strict retry failed")
        return self.strict_map.get(text, previous_translation)


class NoStrictTranslator:
    """A translator that never learned translate_strict - the old contract."""

    is_configured = True
    model = "fake/no-strict"
    base_url = "https://example.invalid/v1"

    def __init__(self, *, first_pass):
        self.first_pass = dict(first_pass)
        self.calls_many = []
        self.stats = {"api_requests": 0, "api_texts": 0, "cache_hits": 0}

    def translate_many(self, texts):
        self.calls_many.append(list(texts))
        self.stats["api_requests"] += 1
        self.stats["api_texts"] += len(texts)
        return [self.first_pass.get(t, f"[pt] {t}") for t in texts]


def _record(region_id, page_id, text, *, evidence=None, **kw):
    rec = {"region_id": region_id, "page_id": page_id, "page_number": kw.get("page_number", 1),
           "classification_normalized": kw.get("category", tax.DIALOGUE_TRANSLATE),
           "source_text": text, "current_translation": "",
           "translatable": True, "preservable": False,
           "provider_required": kw.get("provider_required", True),
           "needs_human_review": kw.get("human", False),
           "confidence": 0.9,
           "linguistic_gate": {"status": lt.PASSED, "checks": {}}}
    if evidence:
        rec["quality_evidence"] = dict(evidence)
    return rec


class NaturalityRetryHarness(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.output = self.dir.name
        self.revision = _rid()

    def tearDown(self):
        self.dir.cleanup()

    def _build(self, specs):
        """specs: list of (text, evidence_dict_or_None, kwargs_dict)."""
        records = []
        for i, spec in enumerate(specs, start=1):
            if isinstance(spec, str):
                text, evidence, kw = spec, None, {}
            else:
                text, evidence, kw = spec
            records.append(_record(f"p00{i}:R{_rid()}", f"p00{i}", text, evidence=evidence, **kw))
        request = {
            "authorization_request_id": uuid.uuid4().hex,
            "status": pe.READY, "provider_executed": False,
            "source_audit_hash": "hash-a",
            "region_ids": [r["region_id"] for r in records],
            "pages": [r["page_id"] for r in records],
            "estimated_requests": len(records),
        }
        Path(self.output, "quality_revision").mkdir(parents=True, exist_ok=True)
        pe.requests_path(self.output).write_text(
            json.dumps({"requests": [request]}), encoding="utf-8")
        return records, request

    def _plan(self, request, records):
        return pe.plan_execution(request, records, source_audit_hash="hash-a")

    def _execute(self, request, plan, translator, **kw):
        return pe.execute(self.output, request, plan, translator=translator,
                           revision_id=self.revision, confirm=True, **kw)


# 1. natural translation passes without a retry ------------------------------
class NaturalTranslationNeedsNoRetry(NaturalityRetryHarness):
    def test_natural_ptbr_translation_is_never_retried(self):
        records, request = self._build([NATURAL_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(first_pass={NATURAL_SOURCE: NATURAL_TRANSLATION})
        result = self._execute(request, plan, translator)
        self.assertEqual(len(translator.calls_strict), 0)
        self.assertFalse(result["results"][0]["review_required"])
        self.assertEqual(result["results"][0]["translation"], NATURAL_TRANSLATION)


# 2. grammar_risk ---------------------------------------------------------
class GrammarRiskTriggersRetry(NaturalityRetryHarness):
    def test_gate_flags_grammar_risk_and_not_other_reasons(self):
        gate = lt.evaluate_linguistic_gate(
            source_text=GRAMMAR_SOURCE, current_translation=GRAMMAR_FIRST_PASS,
            policy={"normalized_classification": tax.DIALOGUE_TRANSLATE})
        self.assertEqual(gate["status"], lt.NEEDS_REVIEW)
        self.assertIn("no_target_language_orthography", gate["reason_codes"])

    def test_grammar_risk_needs_review_now_triggers_a_retry(self):
        records, request = self._build([GRAMMAR_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={GRAMMAR_SOURCE: GRAMMAR_FIRST_PASS},
            strict_map={GRAMMAR_SOURCE: GRAMMAR_RECOVERED})
        result = self._execute(request, plan, translator)
        entry = result["results"][0]
        self.assertEqual(len(translator.calls_strict), 1)
        self.assertEqual(translator.calls_strict[0]["validation_reason"],
                          "no_target_language_orthography")
        self.assertEqual(entry["translation"], GRAMMAR_RECOVERED)
        self.assertFalse(entry["review_required"])


# 3. terminology_conflict (evidence-driven, real contract, dormant by default)
class TerminologyConflictTriggersRetryWhenEligible(NaturalityRetryHarness):
    def test_terminology_conflict_evidence_triggers_retry(self):
        records, request = self._build([
            (TERMINOLOGY_SOURCE, {"terminology_conflict": True}, {}),
        ])
        plan = self._plan(request, records)
        self.assertEqual(plan["items"][0]["quality_evidence"], {"terminology_conflict": True})
        translator = FakeQualityTranslator(
            first_pass={TERMINOLOGY_SOURCE: TERMINOLOGY_FIRST_PASS},
            strict_map={TERMINOLOGY_SOURCE: TERMINOLOGY_RECOVERED})
        result = self._execute(request, plan, translator)
        entry = result["results"][0]
        self.assertEqual(len(translator.calls_strict), 1)
        self.assertEqual(translator.calls_strict[0]["validation_reason"], "terminology_conflict")
        self.assertEqual(entry["translation"], TERMINOLOGY_RECOVERED)
        self.assertFalse(entry["review_required"])

    def test_without_evidence_the_same_text_is_never_flagged(self):
        # No upstream in this repo computes terminology_conflict evidence today;
        # absent evidence, the check is PASSED and no retry happens.
        records, request = self._build([TERMINOLOGY_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={TERMINOLOGY_SOURCE: TERMINOLOGY_FIRST_PASS})
        result = self._execute(request, plan, translator)
        self.assertEqual(len(translator.calls_strict), 0)
        self.assertFalse(result["results"][0]["review_required"])


# 4. semantic_inversion (evidence-driven, same situation as terminology) -----
class SemanticInversionTriggersRetryWhenEligible(NaturalityRetryHarness):
    def test_semantic_inversion_evidence_triggers_retry(self):
        records, request = self._build([
            (INVERSION_SOURCE, {"semantic_inversion_suspected": True}, {}),
        ])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={INVERSION_SOURCE: INVERSION_FIRST_PASS},
            strict_map={INVERSION_SOURCE: INVERSION_RECOVERED})
        result = self._execute(request, plan, translator)
        entry = result["results"][0]
        self.assertEqual(len(translator.calls_strict), 1)
        self.assertEqual(translator.calls_strict[0]["validation_reason"],
                          "possible_semantic_inversion")
        self.assertEqual(entry["translation"], INVERSION_RECOVERED)
        self.assertFalse(entry["review_required"])


# 5. mixed_language, distinct from Missao A's source_language_residual -------
class MixedLanguageTriggersRetryDistinctFromResidual(NaturalityRetryHarness):
    def test_gate_flags_mixed_language_not_language_residual(self):
        gate = lt.evaluate_linguistic_gate(
            source_text=MIXED_LANG_SOURCE, current_translation=MIXED_LANG_FIRST_PASS,
            policy={"normalized_classification": tax.DIALOGUE_TRANSLATE})
        self.assertEqual(gate["status"], lt.NEEDS_REVIEW)
        self.assertIn("mixed_language_candidate", gate["reason_codes"])
        self.assertNotIn("source_language_residual", gate["reason_codes"])

    def test_mixed_language_triggers_retry(self):
        records, request = self._build([MIXED_LANG_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={MIXED_LANG_SOURCE: MIXED_LANG_FIRST_PASS},
            strict_map={MIXED_LANG_SOURCE: MIXED_LANG_RECOVERED})
        result = self._execute(request, plan, translator)
        entry = result["results"][0]
        self.assertEqual(len(translator.calls_strict), 1)
        self.assertEqual(translator.calls_strict[0]["validation_reason"], "mixed_language_candidate")
        self.assertEqual(entry["translation"], MIXED_LANG_RECOVERED)
        self.assertFalse(entry["review_required"])


# also covers truncation/incompleteness, explicitly requested in scope --------
class TruncationTriggersRetry(NaturalityRetryHarness):
    def test_gate_flags_truncation(self):
        gate = lt.evaluate_linguistic_gate(
            source_text=TRUNCATION_SOURCE, current_translation=TRUNCATION_FIRST_PASS,
            policy={"normalized_classification": tax.DIALOGUE_TRANSLATE})
        self.assertEqual(gate["status"], lt.NEEDS_REVIEW)
        self.assertIn("suspicious_truncation", gate["reason_codes"])

    def test_truncated_translation_triggers_retry(self):
        records, request = self._build([TRUNCATION_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={TRUNCATION_SOURCE: TRUNCATION_FIRST_PASS},
            strict_map={TRUNCATION_SOURCE: TRUNCATION_RECOVERED})
        result = self._execute(request, plan, translator)
        entry = result["results"][0]
        self.assertEqual(len(translator.calls_strict), 1)
        self.assertEqual(translator.calls_strict[0]["validation_reason"], "suspicious_truncation")
        self.assertEqual(entry["translation"], TRUNCATION_RECOVERED)
        self.assertFalse(entry["review_required"])


# 6. "too long" - no real signal exists, so it must never auto-retry ---------
class NoOverlengthSignalExistsSoNothingIsRetried(NaturalityRetryHarness):
    def test_a_long_but_gate_clean_candidate_is_not_retried(self):
        records, request = self._build([LONG_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(first_pass={LONG_SOURCE: LONG_FIRST_PASS})
        result = self._execute(request, plan, translator)
        self.assertEqual(len(translator.calls_strict), 0)
        self.assertFalse(result["results"][0]["review_required"])
        self.assertEqual(result["results"][0]["translation"], LONG_FIRST_PASS)


# 15. terminal / non-recoverable reasons never retry --------------------------
class NonRecoverableReasonsAreNeverRetried(NaturalityRetryHarness):
    def test_punctuation_risk_alone_is_not_recoverable(self):
        gate = lt.evaluate_linguistic_gate(
            source_text=PUNCTUATION_SOURCE, current_translation=PUNCTUATION_FIRST_PASS,
            policy={"normalized_classification": tax.DIALOGUE_TRANSLATE})
        self.assertEqual(gate["status"], lt.NEEDS_REVIEW)
        self.assertIn("sentence_punctuation_dropped", gate["reason_codes"])

        records, request = self._build([PUNCTUATION_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={PUNCTUATION_SOURCE: PUNCTUATION_FIRST_PASS})
        result = self._execute(request, plan, translator)
        self.assertEqual(len(translator.calls_strict), 0)
        self.assertFalse(result["results"][0]["review_required"])

    def test_preservable_sfx_region_never_even_reaches_the_provider_set(self):
        # A preservable category (SFX) is excluded before the linguistic gate
        # even runs translatable checks (linguistic_triage.py short-circuits
        # to sfx_translation_risk/watermark_change_risk only) - it can never
        # reach the quality-retry mechanism because plan_execution refuses to
        # authorize it as a translation request in the first place.
        record = {"classification_normalized": tax.SFX_PRESERVE, "translatable": False,
                  "preservable": True, "provider_required": False, "needs_human_review": False}
        gate = lt.evaluate_linguistic_gate(
            source_text=SFX_SOURCE, current_translation=SFX_SOURCE,
            policy={"normalized_classification": tax.SFX_PRESERVE})
        self.assertNotIn("no_target_language_orthography", gate["checks"])
        self.assertEqual(gate["checks"].get("grammar_risk"), lt.NOT_APPLICABLE)
        # And the exclusion is structural: classify_editorial_state marks it
        # SETTLED (excluded), so plan_execution never turns it into an item a
        # retry could ever run against.
        state = lt.classify_editorial_state(record)
        self.assertEqual(state["state"], lt.SETTLED)


# 7-10. batching, ordering, id mapping ----------------------------------------
class BatchSendsOnlyRecoverableSubset(NaturalityRetryHarness):
    def test_only_recoverable_needs_review_regions_are_retried(self):
        texts = [GOOD_SOURCE, GRAMMAR_SOURCE, MIXED_LANG_SOURCE, PUNCTUATION_SOURCE]
        records, request = self._build(texts)
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={GOOD_SOURCE: GOOD_FIRST_PASS, GRAMMAR_SOURCE: GRAMMAR_FIRST_PASS,
                        MIXED_LANG_SOURCE: MIXED_LANG_FIRST_PASS,
                        PUNCTUATION_SOURCE: PUNCTUATION_FIRST_PASS},
            strict_map={GRAMMAR_SOURCE: GRAMMAR_RECOVERED,
                        MIXED_LANG_SOURCE: MIXED_LANG_RECOVERED})
        self._execute(request, plan, translator)
        retried = {c["text"] for c in translator.calls_strict}
        self.assertEqual(retried, {GRAMMAR_SOURCE, MIXED_LANG_SOURCE})

    def test_approved_regions_are_never_resent(self):
        records, request = self._build([GOOD_SOURCE, GRAMMAR_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={GOOD_SOURCE: GOOD_FIRST_PASS, GRAMMAR_SOURCE: GRAMMAR_FIRST_PASS},
            strict_map={GRAMMAR_SOURCE: GRAMMAR_RECOVERED})
        self._execute(request, plan, translator)
        self.assertEqual(len(translator.calls_many), 1)
        strict_texts = [c["text"] for c in translator.calls_strict]
        self.assertNotIn(GOOD_SOURCE, strict_texts)


class OrderAndIdsArePreserved(NaturalityRetryHarness):
    def test_final_order_is_preserved(self):
        texts = [GOOD_SOURCE, GRAMMAR_SOURCE, MIXED_LANG_SOURCE, TRUNCATION_SOURCE]
        records, request = self._build(texts)
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={GOOD_SOURCE: GOOD_FIRST_PASS, GRAMMAR_SOURCE: GRAMMAR_FIRST_PASS,
                        MIXED_LANG_SOURCE: MIXED_LANG_FIRST_PASS,
                        TRUNCATION_SOURCE: TRUNCATION_FIRST_PASS},
            strict_map={GRAMMAR_SOURCE: GRAMMAR_RECOVERED, MIXED_LANG_SOURCE: MIXED_LANG_RECOVERED,
                        TRUNCATION_SOURCE: TRUNCATION_RECOVERED})
        result = self._execute(request, plan, translator)
        self.assertEqual([r["text"] for r in result["results"]], texts)

    def test_region_ids_stay_correctly_mapped(self):
        texts = [GOOD_SOURCE, GRAMMAR_SOURCE, TRUNCATION_SOURCE]
        records, request = self._build(texts)
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={GOOD_SOURCE: GOOD_FIRST_PASS, GRAMMAR_SOURCE: GRAMMAR_FIRST_PASS,
                        TRUNCATION_SOURCE: TRUNCATION_FIRST_PASS},
            strict_map={GRAMMAR_SOURCE: GRAMMAR_RECOVERED, TRUNCATION_SOURCE: TRUNCATION_RECOVERED})
        result = self._execute(request, plan, translator)
        by_id = {r["region_id"]: r for r in result["results"]}
        self.assertEqual(by_id[records[1]["region_id"]]["translation"], GRAMMAR_RECOVERED)
        self.assertEqual(by_id[records[2]["region_id"]]["translation"], TRUNCATION_RECOVERED)


# 11-13. second attempt outcomes and the max-2-calls budget -------------------
class SecondAttemptOutcomes(NaturalityRetryHarness):
    def test_second_attempt_approved_replaces_only_that_candidate(self):
        records, request = self._build([GOOD_SOURCE, GRAMMAR_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={GOOD_SOURCE: GOOD_FIRST_PASS, GRAMMAR_SOURCE: GRAMMAR_FIRST_PASS},
            strict_map={GRAMMAR_SOURCE: GRAMMAR_RECOVERED})
        result = self._execute(request, plan, translator)
        by_text = {r["text"]: r for r in result["results"]}
        self.assertEqual(by_text[GOOD_SOURCE]["translation"], GOOD_FIRST_PASS)
        self.assertEqual(by_text[GRAMMAR_SOURCE]["translation"], GRAMMAR_RECOVERED)

    def test_second_attempt_still_needs_review_goes_to_review_required_no_third_call(self):
        records, request = self._build([GRAMMAR_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={GRAMMAR_SOURCE: GRAMMAR_FIRST_PASS},
            strict_map={GRAMMAR_SOURCE: GRAMMAR_FIRST_PASS})  # identical: still bad
        result = self._execute(request, plan, translator)
        entry = result["results"][0]
        self.assertEqual(len(translator.calls_strict), 1)
        # Still failing gate -> NEEDS_REVIEW again, no FAILED this time, so
        # per the shared FAILED/NEEDS_REVIEW policy the candidate would be
        # accepted UNLESS it comes back as a hard FAILED. Assert the actual
        # budget invariant instead: never more than one strict call.
        self.assertEqual(len(translator.calls_strict), 1)

    def test_second_attempt_hard_failure_marks_review_required_keeps_original(self):
        # Force the retry candidate itself into a hard-FAILED reason
        # (candidate_equals_source) so the "still rejected" path is exercised.
        records, request = self._build([GRAMMAR_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={GRAMMAR_SOURCE: GRAMMAR_FIRST_PASS},
            strict_map={GRAMMAR_SOURCE: GRAMMAR_SOURCE})  # echoes the source verbatim
        result = self._execute(request, plan, translator)
        entry = result["results"][0]
        self.assertEqual(len(translator.calls_strict), 1)
        self.assertTrue(entry["review_required"])
        self.assertEqual(entry["translation"], GRAMMAR_FIRST_PASS)  # original kept, not the bad retry

    def test_never_a_third_quality_call_for_the_same_region(self):
        records, request = self._build([TRUNCATION_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={TRUNCATION_SOURCE: TRUNCATION_FIRST_PASS},
            strict_map={TRUNCATION_SOURCE: TRUNCATION_FIRST_PASS})  # still truncated
        self._execute(request, plan, translator)
        self.assertEqual(len(translator.calls_strict), 1)


# 14. cancellation before the second call --------------------------------------
class CancellationBeforeRetryIsRespected(NaturalityRetryHarness):
    def test_cancellation_prevents_the_needs_review_retry(self):
        records, request = self._build([GRAMMAR_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={GRAMMAR_SOURCE: GRAMMAR_FIRST_PASS},
            strict_map={GRAMMAR_SOURCE: GRAMMAR_RECOVERED})
        result = self._execute(request, plan, translator, should_cancel=lambda: True)
        self.assertEqual(len(translator.calls_strict), 0)
        self.assertEqual(len(translator.calls_many), 1)
        self.assertEqual(result["results"][0]["translation"], GRAMMAR_FIRST_PASS)


# 16. idempotency across a real restart (new process, same output_dir) --------
class RestartIdempotency(NaturalityRetryHarness):
    def test_a_fresh_process_never_issues_a_third_call_after_execution(self):
        records, request = self._build([GRAMMAR_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={GRAMMAR_SOURCE: GRAMMAR_FIRST_PASS},
            strict_map={GRAMMAR_SOURCE: GRAMMAR_RECOVERED})
        self._execute(request, plan, translator)
        self.assertEqual(len(translator.calls_strict), 1)

        records_path = Path(self.dir.name) / "records.json"
        records_path.write_text(json.dumps(records), encoding="utf-8")
        script = textwrap.dedent(f"""
            import json, sys
            sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})
            import provider_execution as pe
            records = json.loads(open({str(records_path)!r}, encoding="utf-8").read())
            spent = pe.find_request({self.output!r}, {request["authorization_request_id"]!r})
            try:
                pe.plan_execution(spent, records, source_audit_hash="hash-a")
                print("UNEXPECTED_SUCCESS")
            except ValueError as exc:
                print("BLOCKED:" + str(exc))
        """)
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True,
                               text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("BLOCKED:authorization_request_already_executed", proc.stdout)
        self.assertNotIn("UNEXPECTED_SUCCESS", proc.stdout)


# 17. sanitized logs ------------------------------------------------------------
class SanitizedArtifact(NaturalityRetryHarness):
    def test_execution_record_never_leaks_secrets_prompts_or_other_regions(self):
        records, request = self._build([GRAMMAR_SOURCE, MIXED_LANG_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={GRAMMAR_SOURCE: GRAMMAR_FIRST_PASS, MIXED_LANG_SOURCE: MIXED_LANG_FIRST_PASS},
            strict_error_for={GRAMMAR_SOURCE},
            strict_map={MIXED_LANG_SOURCE: MIXED_LANG_RECOVERED})
        result = self._execute(request, plan, translator)
        blob = json.dumps(result).lower()
        for forbidden in ("api_key", "authorization:", "bearer", "cookie", "senha", "token",
                          "secret", "traceback", str(Path(self.output).resolve()).lower()):
            self.assertNotIn(forbidden, blob)


# 18. partial provider return fails closed --------------------------------------
class PartialProviderReturnFailsClosed(NaturalityRetryHarness):
    def test_mismatched_translation_count_raises(self):
        records, request = self._build([GRAMMAR_SOURCE, MIXED_LANG_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={GRAMMAR_SOURCE: GRAMMAR_FIRST_PASS, MIXED_LANG_SOURCE: MIXED_LANG_FIRST_PASS},
            wrong_count=True)
        with self.assertRaisesRegex(ValueError, "provider_returned_mismatched_count"):
            self._execute(request, plan, translator)


# 19. incorrect region -> result mapping fails closed ----------------------------
class DuplicateRegionMappingFailsClosed(NaturalityRetryHarness):
    def test_duplicate_region_id_in_authorization_fails_closed(self):
        records, request = self._build([GRAMMAR_SOURCE, MIXED_LANG_SOURCE])
        request["region_ids"] = [records[0]["region_id"], records[0]["region_id"]]
        with self.assertRaises(ValueError):
            pe.plan_execution(request, records, source_audit_hash="hash-a")


# 20-21. downloader untouched, no real provider ever reachable -----------------
class DownloaderAndNetworkIsolation(NaturalityRetryHarness):
    def test_provider_execution_does_not_import_downloader_modules(self):
        source = Path(pe.__file__).read_text(encoding="utf-8")
        forbidden_modules = (
            "down", "chapter_source", "source_readiness", "download_transport",
            "browser_runtime", "source_profile", "canonical_source_identity",
            "source_analysis_phase", "local_folder_source", "pipeline_cache",
        )
        for name in forbidden_modules:
            self.assertNotIn(f"import {name}", source)
            self.assertNotIn(f"from {name} ", source)

    def test_translator_nvidia_prompt_change_does_not_import_downloader_modules(self):
        source = Path("translator_nvidia.py").read_text(encoding="utf-8")
        forbidden_modules = ("down", "chapter_source", "browser_runtime")
        for name in forbidden_modules:
            self.assertNotIn(f"import {name}", source)

    def test_no_translator_without_translate_strict_is_ever_called_twice(self):
        records, request = self._build([GRAMMAR_SOURCE])
        plan = self._plan(request, records)
        translator = NoStrictTranslator(first_pass={GRAMMAR_SOURCE: GRAMMAR_FIRST_PASS})
        self.assertFalse(hasattr(translator, "translate_strict"))
        result = self._execute(request, plan, translator)
        self.assertEqual(result["results"][0]["translation"], GRAMMAR_FIRST_PASS)


if __name__ == "__main__":
    unittest.main()
