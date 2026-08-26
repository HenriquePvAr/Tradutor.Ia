"""TDD #6: exhausted translation attempts fail closed instead of trusting source."""

import _test_bootstrap  # noqa: F401

import tempfile
import unittest
from pathlib import Path

import numpy as np

import semantic_fidelity
from benchmark_pipeline import _translation_quality_accounting
from ocr_balloon import (
    OCRLine,
    TextGroup,
    apply_group_translations,
    get_translatable_groups,
    validate_and_retry_translations,
)
from session_context import SessionContextStore


def _line(text):
    polygon = np.array([[10, 10], [210, 10], [210, 45], [10, 45]], dtype=np.int32)
    return OCRLine(
        text=text,
        confidence=0.95,
        polygon=polygon,
        box=(10, 10, 200, 35),
        raw_text=text,
        engine="rapidocr",
        page=1,
    )


def _group(source, *, group_id="G001", names=()):
    group = TextGroup(
        group_id=group_id,
        lines=[_line(source)],
        text=source,
        classification="speech",
        inside_balloon_like_region=True,
    )
    group.detected_proper_names = list(names)
    return group


class _RetryTranslator:
    is_configured = True

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def translate_strict(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return self.responses.pop(0) if self.responses else ""


class _Naturalizer:
    def __init__(self):
        self.calls = []

    def naturalize_ptbr(self, request):
        self.calls.append(dict(request))
        return "Texto naturalizado."


def _validate(group, translator=None, **kwargs):
    validate_and_retry_translations([group], translator or _RetryTranslator(), **kwargs)
    return group


def _debug_item(group):
    return {
        "classification": group.classification,
        "clean_text": group.text,
        "sent_to_nvidia": group.sent_to_translation,
        "translation": group.translation,
        "translation_candidate": group.translation_candidate,
        "translation_valid": group.translation_valid,
        "translation_final_state": group.translation_final_state,
        "translation_final_reason": group.translation_final_reason,
        "manual_review_required": group.manual_review_required,
        "translation_unresolved": group.translation_unresolved,
        "translation_attempts_exhausted": group.translation_attempts_exhausted,
        "trusted_translation_missing": group.trusted_translation_missing,
        "source_fallback_prevented": group.source_fallback_prevented,
        "redrawn": group.redrawn,
        "preserved_original": group.preserved_original,
        "rejected_translation": group.rejected_translation,
    }


class SafeExhaustedRetryPolicyTests(unittest.TestCase):
    def test_provider_exhaustion_preserves_source_but_has_no_trusted_translation(self):
        group = _group("PLEASE WAIT")
        apply_group_translations([group], [""])

        _validate(group, _RetryTranslator())

        self.assertEqual(group.text, "PLEASE WAIT")
        self.assertEqual(group.translation, "")
        self.assertEqual(group.translation_candidate, "")
        self.assertFalse(group.translation_valid)
        self.assertTrue(group.manual_review_required)
        self.assertTrue(group.translation_unresolved)
        self.assertTrue(group.trusted_translation_missing)
        self.assertTrue(group.source_fallback_prevented)
        self.assertEqual(group.translation_final_state, "manual_review")
        self.assertEqual(group.translation_final_reason, "missing_translation_candidate")
        self.assertEqual(get_translatable_groups([group]), [])

    def test_candidate_equals_source_exhaustion_does_not_promote_source_to_target(self):
        group = _group("PLEASE WAIT")
        translator = _RetryTranslator("PLEASE WAIT", "PLEASE WAIT")
        apply_group_translations([group], ["PLEASE WAIT"])

        _validate(group, translator)

        self.assertGreaterEqual(len(translator.calls), 1)
        self.assertEqual(group.text, "PLEASE WAIT")
        self.assertEqual(group.translation, "")
        self.assertEqual(group.translation_candidate, "PLEASE WAIT")
        self.assertEqual(group.rejected_translation, "PLEASE WAIT")
        self.assertFalse(group.translation_valid)
        self.assertTrue(group.manual_review_required)
        self.assertTrue(group.source_fallback_prevented)
        self.assertEqual(group.translation_final_reason, "untranslated_source_after_retries")
        self.assertEqual(get_translatable_groups([group]), [])

    def test_semantic_failure_exhaustion_does_not_resurrect_rejected_candidate(self):
        group = _group("I won't go.")
        translator = _RetryTranslator("Eu vou.", "Eu vou.")
        apply_group_translations([group], ["Eu vou."])

        _validate(group, translator)

        self.assertEqual(group.translation, "")
        self.assertEqual(group.translation_candidate, "Eu vou.")
        self.assertFalse(group.translation_valid)
        self.assertEqual(group.translation_final_reason, "semantic_fidelity_failed_after_retries")
        self.assertTrue(semantic_fidelity.is_fidelity_reason(group.translation_validation_reason))

    def test_terminology_failure_exhaustion_renders_under_review_never_as_clean(self):
        # TDD #84F8: the candidate ships so the English source does not stay on the
        # page, but it is never trusted -- not valid, review-required, conflict kept.
        group = _group("THE ZARQUON IS CLOSED")
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        ledger = SessionContextStore(Path(folder.name) / "session.json", "synthetic")
        ledger.prepare([])
        seed = _group("ZARQUON")
        apply_group_translations([seed], ["ZARQUONITE"])
        ledger.record_translations([seed])
        translator = _RetryTranslator("O PORTALIS ESTA FECHADO", "O PORTALIS ESTA FECHADO")
        apply_group_translations([group], ["O PORTALIS ESTA FECHADO"])

        _validate(group, translator, terminology_ledger=ledger)

        self.assertEqual(group.translation, "O PORTALIS ESTA FECHADO")
        self.assertEqual(group.translation_candidate, "O PORTALIS ESTA FECHADO")
        self.assertFalse(group.translation_valid)
        self.assertTrue(group.manual_review_required)
        self.assertEqual(group.translation_quality_impact, "review_required")
        self.assertEqual(group.translation_final_reason, "terminology_conflict_after_retries")

    def test_last_allowed_retry_success_stays_trusted(self):
        group = _group("PLEASE WAIT")
        translator = _RetryTranslator("Por favor, espere.")
        apply_group_translations([group], ["PLEASE WAIT"])

        _validate(group, translator)

        self.assertEqual(group.translation, "POR FAVOR, ESPERE.")
        self.assertTrue(group.translation_valid)
        self.assertEqual(group.translation_final_state, "translated")
        self.assertFalse(group.manual_review_required)
        self.assertFalse(group.trusted_translation_missing)

    def test_naturalization_failure_after_trusted_translation_keeps_trusted_target(self):
        class FailingNaturalizer:
            def __init__(self):
                self.calls = []

            def naturalize_ptbr(self, request):
                self.calls.append(dict(request))
                raise RuntimeError("boom")

        group = _group("I will explain later.")
        group.quality_evidence = {"ptbr_naturalization_needed": True}
        naturalizer = FailingNaturalizer()
        apply_group_translations([group], ["Eu explicarei depois."])

        _validate(group, _RetryTranslator(), ptbr_naturalizer=naturalizer)

        self.assertEqual(len(naturalizer.calls), 1)
        self.assertEqual(group.translation, "Eu explicarei depois.")
        self.assertTrue(group.translation_valid)
        self.assertFalse(group.trusted_translation_missing)
        self.assertEqual(group.naturalization_status, "failed")

    def test_untrusted_translation_never_reaches_naturalizer(self):
        group = _group("I won't go.")
        group.quality_evidence = {"ptbr_naturalization_needed": True}
        naturalizer = _Naturalizer()
        apply_group_translations([group], ["Eu vou."])

        _validate(group, _RetryTranslator("Eu vou."), ptbr_naturalizer=naturalizer)

        self.assertEqual(naturalizer.calls, [])
        self.assertEqual(group.translation, "")
        self.assertTrue(group.trusted_translation_missing)

    def test_legitimate_source_equal_proper_name_is_preserved_not_failed(self):
        group = _group("NARAEK", names=["NARAEK"])
        apply_group_translations([group], ["NARAEK"])

        _validate(group, _RetryTranslator())

        self.assertEqual(group.translation, "NARAEK")
        self.assertTrue(group.translation_valid)
        self.assertIn(group.translation_final_state, {"translated", "preserved_original"})
        self.assertFalse(group.trusted_translation_missing)
        self.assertFalse(group.manual_review_required)

    def test_already_target_language_translation_remains_valid(self):
        group = _group("Olá, espere aqui.")
        apply_group_translations([group], ["Olá, espere aqui."])

        _validate(group, _RetryTranslator())

        self.assertEqual(group.translation, "Olá, espere aqui.")
        self.assertTrue(group.translation_valid)
        self.assertEqual(group.translation_final_state, "translated")
        self.assertFalse(group.trusted_translation_missing)

    def test_one_failed_region_preserves_good_regions_and_blocks_clean_quality(self):
        good = _group("I will wait.", group_id="G1")
        failed = _group("PLEASE WAIT", group_id="G2")
        apply_group_translations([good, failed], ["Eu vou esperar.", "PLEASE WAIT"])

        validate_and_retry_translations(
            [good, failed],
            _RetryTranslator("PLEASE WAIT", "PLEASE WAIT"),
        )
        good.redrawn = True
        states = [{"debug_data": {"items": [_debug_item(good), _debug_item(failed)]}}]
        accounting = _translation_quality_accounting(states)

        self.assertEqual(good.translation, "Eu vou esperar.")
        self.assertTrue(good.translation_valid)
        self.assertEqual(failed.translation, "")
        self.assertTrue(failed.manual_review_required)
        self.assertFalse(accounting["quality_passed"])
        self.assertTrue(accounting["requires_review"])
        self.assertEqual(accounting["manual_review"], 1)
        self.assertEqual(accounting["translation_unresolved"], 1)
        self.assertEqual(accounting["trusted_translation_missing"], 1)
        self.assertEqual(accounting["source_fallback_prevented"], 1)


if __name__ == "__main__":
    unittest.main()
