"""Automatic quality retry: one extra provider call per region, only when the
linguistic gate rejects the first translation for a recoverable reason
(BLOCO 6B follow-up).

Everything here runs against a fake translator - never a real network call.
The offline guard makes that fail loudly instead of silently spending money.
"""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

import linguistic_triage as lt
import provider_execution as pe
import region_taxonomy as tax


def _rid():
    return uuid.uuid4().hex[:8].upper()


# Deliberately chosen so the linguistic gate isolates exactly one failure
# reason per scenario (see .runtime/claude-translation-quality-retry/
# retry_reason_mapping.md for why these particular strings were picked).
RESIDUAL_SOURCE = "HARBOUR EMPTY ARRIVED LATE TONIGHT SILENT"
RESIDUAL_FIRST_PASS = "harbour empty arrived late"   # 100% overlap, not equal
RESIDUAL_RECOVERED = "O píer estava deserto e quieto."

LITERAL_SOURCE = "PROPER NOUN TEST SENTENCE HERE NOW"
LITERAL_FIRST_PASS = "PROPER NOUN TEST SENTENCE HERE NOW"  # exact echo
LITERAL_RECOVERED = "Substantivo próprio, frase de teste aqui agora."

EMPTY_SOURCE = "THIS BALLOON HAS REAL TEXT IN IT"
EMPTY_FIRST_PASS = ""
EMPTY_RECOVERED = "Este balão tem texto de verdade nele."

ENCODING_SOURCE = "SOME PLAIN LINE"
ENCODING_FIRST_PASS = "texto quebradoÂ¿ com artefato"  # encoding artefact -> terminal

GOOD_SOURCE = "ANOTHER FINE MORNING"
GOOD_FIRST_PASS = "Outra bela manhã."


class FakeQualityTranslator:
    """Records every call, never touches the network."""

    is_configured = True
    model = "fake/quality"
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


def _no_strict_translator(first_pass):
    return NoStrictTranslator(first_pass=first_pass)


def _record(region_id, page_id, text, **kw):
    return {"region_id": region_id, "page_id": page_id, "page_number": kw.get("page_number", 1),
            "classification_normalized": kw.get("category", tax.DIALOGUE_TRANSLATE),
            "source_text": text, "current_translation": "",
            "translatable": True, "preservable": False,
            "provider_required": kw.get("provider_required", True),
            "needs_human_review": kw.get("human", False),
            "confidence": 0.9,
            "linguistic_gate": {"status": lt.PASSED, "checks": {}}}


class QualityRetryHarness(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.output = self.dir.name
        self.revision = _rid()

    def tearDown(self):
        self.dir.cleanup()

    def _build(self, texts):
        records = [_record(f"p00{i}:R{_rid()}", f"p00{i}", text)
                   for i, text in enumerate(texts, start=1)]
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


class AllApproved(QualityRetryHarness):
    def test_all_approved_means_a_single_provider_call(self):
        records, request = self._build([GOOD_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(first_pass={GOOD_SOURCE: GOOD_FIRST_PASS})
        result = self._execute(request, plan, translator)
        self.assertEqual(len(translator.calls_many), 1)
        self.assertEqual(len(translator.calls_strict), 0)
        self.assertFalse(result["results"][0]["review_required"])
        self.assertEqual(result["results"][0]["translation"], GOOD_FIRST_PASS)


class OneRejectedTriggersOneRetry(QualityRetryHarness):
    def test_second_call_contains_only_the_rejected_region(self):
        records, request = self._build([GOOD_SOURCE, RESIDUAL_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={GOOD_SOURCE: GOOD_FIRST_PASS, RESIDUAL_SOURCE: RESIDUAL_FIRST_PASS},
            strict_map={RESIDUAL_SOURCE: RESIDUAL_RECOVERED})
        self._execute(request, plan, translator)
        self.assertEqual(len(translator.calls_strict), 1)
        self.assertEqual(translator.calls_strict[0]["text"], RESIDUAL_SOURCE)

    def test_residual_english_is_recovered(self):
        records, request = self._build([RESIDUAL_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={RESIDUAL_SOURCE: RESIDUAL_FIRST_PASS},
            strict_map={RESIDUAL_SOURCE: RESIDUAL_RECOVERED})
        result = self._execute(request, plan, translator)
        entry = result["results"][0]
        self.assertEqual(entry["translation"], RESIDUAL_RECOVERED)
        self.assertFalse(entry["review_required"])
        self.assertEqual(translator.calls_strict[0]["validation_reason"],
                          "source_language_residual")

    def test_empty_result_is_recovered(self):
        records, request = self._build([EMPTY_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={EMPTY_SOURCE: EMPTY_FIRST_PASS},
            strict_map={EMPTY_SOURCE: EMPTY_RECOVERED})
        result = self._execute(request, plan, translator)
        entry = result["results"][0]
        self.assertEqual(entry["translation"], EMPTY_RECOVERED)
        self.assertFalse(entry["review_required"])
        self.assertEqual(translator.calls_strict[0]["validation_reason"], "empty_translation")

    def test_literal_echo_is_recovered_when_the_signal_exists(self):
        records, request = self._build([LITERAL_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={LITERAL_SOURCE: LITERAL_FIRST_PASS},
            strict_map={LITERAL_SOURCE: LITERAL_RECOVERED})
        result = self._execute(request, plan, translator)
        entry = result["results"][0]
        self.assertEqual(entry["translation"], LITERAL_RECOVERED)
        self.assertFalse(entry["review_required"])
        self.assertEqual(translator.calls_strict[0]["validation_reason"],
                          "candidate_equals_source")


class SeveralRejectedOnlySubsetRetried(QualityRetryHarness):
    def test_only_the_rejected_subset_is_retried(self):
        records, request = self._build(
            [GOOD_SOURCE, RESIDUAL_SOURCE, EMPTY_SOURCE, LITERAL_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={GOOD_SOURCE: GOOD_FIRST_PASS, RESIDUAL_SOURCE: RESIDUAL_FIRST_PASS,
                        EMPTY_SOURCE: EMPTY_FIRST_PASS, LITERAL_SOURCE: LITERAL_FIRST_PASS},
            strict_map={RESIDUAL_SOURCE: RESIDUAL_RECOVERED, EMPTY_SOURCE: EMPTY_RECOVERED,
                        LITERAL_SOURCE: LITERAL_RECOVERED})
        self._execute(request, plan, translator)
        retried_texts = {c["text"] for c in translator.calls_strict}
        self.assertEqual(retried_texts, {RESIDUAL_SOURCE, EMPTY_SOURCE, LITERAL_SOURCE})
        self.assertNotIn(GOOD_SOURCE, retried_texts)

    def test_final_order_is_preserved(self):
        texts = [GOOD_SOURCE, RESIDUAL_SOURCE, EMPTY_SOURCE, LITERAL_SOURCE]
        records, request = self._build(texts)
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={GOOD_SOURCE: GOOD_FIRST_PASS, RESIDUAL_SOURCE: RESIDUAL_FIRST_PASS,
                        EMPTY_SOURCE: EMPTY_FIRST_PASS, LITERAL_SOURCE: LITERAL_FIRST_PASS},
            strict_map={RESIDUAL_SOURCE: RESIDUAL_RECOVERED, EMPTY_SOURCE: EMPTY_RECOVERED,
                        LITERAL_SOURCE: LITERAL_RECOVERED})
        result = self._execute(request, plan, translator)
        self.assertEqual([r["text"] for r in result["results"]], texts)

    def test_region_ids_stay_correctly_mapped(self):
        texts = [GOOD_SOURCE, RESIDUAL_SOURCE, EMPTY_SOURCE]
        records, request = self._build(texts)
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={GOOD_SOURCE: GOOD_FIRST_PASS, RESIDUAL_SOURCE: RESIDUAL_FIRST_PASS,
                        EMPTY_SOURCE: EMPTY_FIRST_PASS},
            strict_map={RESIDUAL_SOURCE: RESIDUAL_RECOVERED, EMPTY_SOURCE: EMPTY_RECOVERED})
        result = self._execute(request, plan, translator)
        expected_ids = [r["region_id"] for r in records]
        self.assertEqual([r["region_id"] for r in result["results"]], expected_ids)
        by_id = {r["region_id"]: r for r in result["results"]}
        self.assertEqual(by_id[records[1]["region_id"]]["translation"], RESIDUAL_RECOVERED)
        self.assertEqual(by_id[records[2]["region_id"]]["translation"], EMPTY_RECOVERED)

    def test_approved_regions_are_never_resent(self):
        records, request = self._build([GOOD_SOURCE, RESIDUAL_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={GOOD_SOURCE: GOOD_FIRST_PASS, RESIDUAL_SOURCE: RESIDUAL_FIRST_PASS},
            strict_map={RESIDUAL_SOURCE: RESIDUAL_RECOVERED})
        self._execute(request, plan, translator)
        self.assertEqual(len(translator.calls_many), 1)  # approved never re-sent via translate_many
        strict_texts = [c["text"] for c in translator.calls_strict]
        self.assertNotIn(GOOD_SOURCE, strict_texts)


class SecondRejectionStopsAtOne(QualityRetryHarness):
    def test_still_rejected_after_retry_goes_to_review_required_no_third_call(self):
        records, request = self._build([RESIDUAL_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={RESIDUAL_SOURCE: RESIDUAL_FIRST_PASS},
            strict_map={RESIDUAL_SOURCE: RESIDUAL_FIRST_PASS})  # still bad
        result = self._execute(request, plan, translator)
        entry = result["results"][0]
        self.assertTrue(entry["review_required"])
        self.assertEqual(len(translator.calls_strict), 1)

    def test_never_a_third_quality_call_for_the_same_region(self):
        records, request = self._build([EMPTY_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={EMPTY_SOURCE: ""}, strict_map={EMPTY_SOURCE: ""})
        self._execute(request, plan, translator)
        self.assertEqual(len(translator.calls_strict), 1)


class TerminalReasonsAreNotRetried(QualityRetryHarness):
    def test_encoding_artefact_is_terminal_not_retried(self):
        records, request = self._build([ENCODING_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={ENCODING_SOURCE: ENCODING_FIRST_PASS})
        result = self._execute(request, plan, translator)
        self.assertEqual(len(translator.calls_strict), 0)
        self.assertTrue(result["results"][0]["review_required"])

    def test_translator_without_translate_strict_is_never_called_twice(self):
        records, request = self._build([RESIDUAL_SOURCE])
        plan = self._plan(request, records)
        translator = _no_strict_translator({RESIDUAL_SOURCE: RESIDUAL_FIRST_PASS})
        self.assertFalse(hasattr(translator, "translate_strict"))
        result = self._execute(request, plan, translator)
        self.assertTrue(result["results"][0]["review_required"])


class CancellationStopsTheRetry(QualityRetryHarness):
    def test_cancellation_requested_before_retry_prevents_the_call(self):
        records, request = self._build([RESIDUAL_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={RESIDUAL_SOURCE: RESIDUAL_FIRST_PASS},
            strict_map={RESIDUAL_SOURCE: RESIDUAL_RECOVERED})
        result = self._execute(request, plan, translator, should_cancel=lambda: True)
        self.assertEqual(len(translator.calls_strict), 0)
        self.assertTrue(result["results"][0]["review_required"])
        # The first pass still happened - cancellation only stops the retry.
        self.assertEqual(len(translator.calls_many), 1)


class TransportAndQualityCountersAreIndependent(QualityRetryHarness):
    def test_internal_transport_retries_do_not_consume_the_quality_attempt(self):
        records, request = self._build([RESIDUAL_SOURCE])
        plan = self._plan(request, records)

        class TransportRetryingTranslator(FakeQualityTranslator):
            def translate_many(self, texts):
                # Simulate two internal transport attempts collapsed into one
                # logical call, invisible to provider_execution.
                self.stats["transport_attempts"] = self.stats.get("transport_attempts", 0) + 2
                return super().translate_many(texts)

        translator = TransportRetryingTranslator(
            first_pass={RESIDUAL_SOURCE: RESIDUAL_FIRST_PASS},
            strict_map={RESIDUAL_SOURCE: RESIDUAL_RECOVERED})
        self._execute(request, plan, translator)
        self.assertEqual(len(translator.calls_many), 1)
        self.assertEqual(len(translator.calls_strict), 1)
        self.assertEqual(translator.stats["transport_attempts"], 2)


class IdempotencyAcrossReruns(QualityRetryHarness):
    def test_an_already_executed_request_never_retries_again(self):
        records, request = self._build([RESIDUAL_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={RESIDUAL_SOURCE: RESIDUAL_FIRST_PASS},
            strict_map={RESIDUAL_SOURCE: RESIDUAL_RECOVERED})
        self._execute(request, plan, translator)
        self.assertEqual(len(translator.calls_strict), 1)
        spent = pe.find_request(self.output, request["authorization_request_id"])
        with self.assertRaisesRegex(ValueError, "already_executed"):
            pe.plan_execution(spent, records, source_audit_hash="hash-a")
        # No further calls happened trying to reconcile after "restart".
        self.assertEqual(len(translator.calls_strict), 1)


class OneRegionFailingDoesNotCorruptOthers(QualityRetryHarness):
    def test_a_retry_error_on_one_region_leaves_the_others_intact(self):
        texts = [RESIDUAL_SOURCE, EMPTY_SOURCE, LITERAL_SOURCE]
        records, request = self._build(texts)
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={RESIDUAL_SOURCE: RESIDUAL_FIRST_PASS, EMPTY_SOURCE: EMPTY_FIRST_PASS,
                        LITERAL_SOURCE: LITERAL_FIRST_PASS},
            strict_map={EMPTY_SOURCE: EMPTY_RECOVERED, LITERAL_SOURCE: LITERAL_RECOVERED},
            strict_error_for={RESIDUAL_SOURCE})
        result = self._execute(request, plan, translator)
        by_text = {r["text"]: r for r in result["results"]}
        self.assertTrue(by_text[RESIDUAL_SOURCE]["review_required"])
        self.assertFalse(by_text[EMPTY_SOURCE]["review_required"])
        self.assertFalse(by_text[LITERAL_SOURCE]["review_required"])
        self.assertEqual(by_text[EMPTY_SOURCE]["translation"], EMPTY_RECOVERED)
        self.assertEqual(by_text[LITERAL_SOURCE]["translation"], LITERAL_RECOVERED)
        self.assertEqual(len(result["results"]), 3)


class DuplicateRegionIdFailsClosed(QualityRetryHarness):
    def test_a_duplicate_region_id_in_the_authorization_fails_closed(self):
        records, request = self._build([RESIDUAL_SOURCE, EMPTY_SOURCE])
        request["region_ids"] = [records[0]["region_id"], records[0]["region_id"]]
        with self.assertRaises(ValueError):
            pe.plan_execution(request, records, source_audit_hash="hash-a")


class SanitizedArtifact(QualityRetryHarness):
    def test_the_execution_record_never_leaks_secrets_or_raw_tracebacks(self):
        records, request = self._build([RESIDUAL_SOURCE])
        plan = self._plan(request, records)
        translator = FakeQualityTranslator(
            first_pass={RESIDUAL_SOURCE: RESIDUAL_FIRST_PASS},
            strict_error_for={RESIDUAL_SOURCE})
        result = self._execute(request, plan, translator)
        blob = json.dumps(result).lower()
        for forbidden in ("api_key", "authorization:", "bearer", "cookie", "senha", "token",
                          "secret", "traceback", str(Path(self.output).resolve()).lower()):
            self.assertNotIn(forbidden, blob)


class DownloaderIsNeverTouched(QualityRetryHarness):
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


if __name__ == "__main__":
    unittest.main()
