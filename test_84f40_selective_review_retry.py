"""TDD #84F40 - selective, region-scoped retry for manual_review_required regions.

Mission #84F39 fixed a real silent-drop bug: a region flagged
``manual_review_required`` no longer vanishes from the final gate. #84F40
builds on that fix by giving flagged regions one narrow, class-specific retry
instead of leaving every one of them stuck, without loosening any existing
OCR/translation/render gate and without ever inventing a translation or a
source character.

These tests are offline, read-only against the pure functions in
``selective_review_retry.py`` (plus the real ``ocr_engine``/``ocr_balloon``
functions it reuses) and against one real completed job's ``quality_report.json``
for the structural invariant test. No provider, network, Drive, or Vortex
fixture is touched. No page number, chapter title, or specific story phrase is
hardcoded as *the* thing being fixed - the fixture sentences below are
deliberately invented and generic, matching the existing #84F39 test's own
convention.
"""
from __future__ import annotations

import _test_bootstrap  # noqa: F401
from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import json
import os
import unittest

import ocr_engine
import selective_review_retry as srr
from ocr_balloon import validate_translation_text


class ClassifyReviewCauseTests(unittest.TestCase):
    """TEST 7 (partial) + general classification correctness."""

    def test_ocr_quality_cause(self):
        cause = srr.classify_review_cause(
            quality_reasons=["alphanumeric_ocr_artifact", "long_consonant_run"],
            translation_final_reason="translation_not_selected",
            translation_final_state="skipped_with_reason",
        )
        self.assertEqual(cause, "ocr")

    def test_fidelity_cause_terminology(self):
        cause = srr.classify_review_cause(
            translation_final_reason="terminology_conflict_after_retries",
            translation_validation_reason="terminology_conflict:SOME_TERM",
        )
        self.assertEqual(cause, "fidelity")

    def test_fidelity_cause_invalid_translation(self):
        cause = srr.classify_review_cause(
            translation_final_reason="invalid_translation_after_retries",
            translation_validation_reason="unnatural_ptbr_verb_mood:voce_infinitive",
        )
        self.assertEqual(cause, "fidelity")

    def test_render_cause(self):
        cause = srr.classify_review_cause(
            translation_final_reason="translation_not_rendered_after_validation",
        )
        self.assertEqual(cause, "render")

    def test_branding_reasons_are_never_ocr_or_fidelity(self):
        for reason in ("url", "credit"):
            with self.subTest(reason=reason):
                cause = srr.classify_review_cause(translation_final_reason=reason)
                self.assertEqual(cause, "branding")


class OcrSelectiveRetryTests(unittest.TestCase):
    """TEST 1 + TEST 2: OCR retry only accepts a dictionary-proven repair."""

    def test_isolated_digit_noise_is_repaired_without_inventing_words(self):
        # Generic fixture: a short common word with one spurious OCR digit
        # glyph stuck to it, inside an otherwise ordinary punctuated sentence -
        # the same *shape* of defect the real run's OCR-quality gate reported,
        # not any specific captured phrase.
        source = "SHE SAID THAT WAS ALL SHE HAD IN1 THE END, TRULY."
        result = srr.retry_ocr_region(source)
        self.assertTrue(result.changed)
        self.assertTrue(result.accepted)
        self.assertNotIn("1", result.repaired_text)
        # Every letter of the sentence survives untouched; only the noise
        # digit was discarded.
        source_letters = "".join(ch for ch in source if ch.isalpha())
        repaired_letters = "".join(ch for ch in result.repaired_text if ch.isalpha())
        self.assertEqual(source_letters, repaired_letters)

    def test_unsafe_compact_fusion_is_refused_not_invented(self):
        # Generic fixture: two words fused with no space, of a shape the
        # pipeline's own dictionary segmenter cannot safely resolve (either it
        # finds nothing, or - as verified against the real repair engine - it
        # finds a "correction" that changes letters). Either outcome must
        # leave the source untouched rather than ship a guess.
        source = "CHECK YOURATTRIBUTES ANDASPECT."
        result = srr.retry_ocr_region(source)
        self.assertFalse(result.changed)
        self.assertEqual(result.repaired_text, source)

    def test_retry_ocr_region_never_changes_letters_it_keeps(self):
        # Property check across several generic fused-word shapes: whatever
        # retry_ocr_region accepts, the kept letters must be an exact subset/
        # reordering-with-spaces of the original - never a substituted letter.
        samples = [
            "SHE SAID THAT WAS ALL SHE HAD IN1 THE END, TRULY.",
            "CHECK YOURATTRIBUTES ANDASPECT.",
            "JUST GO IN TO THEDREAMREALM, KILL AFEWMONSTERS.",
            "WHAT YOU DODURINGTHETRIALWILL DETERMINETHEREWARDS.",
        ]
        for source in samples:
            with self.subTest(source=source):
                result = srr.retry_ocr_region(source)
                if not result.changed:
                    continue
                source_letters = sorted(ch for ch in source.upper() if ch.isalpha())
                repaired_letters = sorted(
                    ch for ch in result.repaired_text.upper() if ch.isalpha()
                )
                self.assertEqual(source_letters, repaired_letters)


class FidelitySelectiveRetryTests(unittest.TestCase):
    """TEST 3: fidelity retry only retries that one line; global gate intact."""

    def test_valid_retry_candidate_is_accepted_through_the_real_validator(self):
        calls = []

        def fake_translate(text, **kwargs):
            calls.append((text, kwargs))
            return "TRADUÇÃO VÁLIDA EM PORTUGUÊS AQUI PARA TESTE."

        result = srr.retry_fidelity_region(
            "SOME ORIGINAL ENGLISH SENTENCE FOR TESTING PURPOSES HERE.",
            "previous rejected candidate",
            classification="speech",
            allowed_names=[],
            validation_reason="terminology_conflict:TESTWORD",
            translate_fn=fake_translate,
            validate_fn=validate_translation_text,
        )
        self.assertEqual(len(calls), 1, "must call the translator exactly once")
        self.assertTrue(result.attempted)
        self.assertTrue(result.valid)

    def test_retry_still_failing_the_real_validator_stays_invalid(self):
        def fake_translate(text, **kwargs):
            # Echoes the English source back untranslated - the real
            # validator (the same one production uses, unmodified) must
            # still reject it. This proves the retry cannot bypass the
            # global semantic/language gate.
            return text

        result = srr.retry_fidelity_region(
            "SOME ORIGINAL ENGLISH SENTENCE FOR TESTING PURPOSES HERE.",
            "previous rejected candidate",
            classification="speech",
            allowed_names=[],
            validation_reason="terminology_conflict:TESTWORD",
            translate_fn=fake_translate,
            validate_fn=validate_translation_text,
        )
        self.assertTrue(result.attempted)
        self.assertFalse(result.valid)

    def test_missing_dependencies_never_invents_a_pass(self):
        result = srr.retry_fidelity_region(
            "SOME ORIGINAL ENGLISH SENTENCE.",
            "previous",
            classification="speech",
            allowed_names=[],
            validation_reason="terminology_conflict:TESTWORD",
            translate_fn=None,
            validate_fn=None,
        )
        self.assertFalse(result.attempted)
        self.assertFalse(result.valid)


class RenderSelectiveRetryTests(unittest.TestCase):
    """TEST 4: render retry only re-scores layout evidence, never re-translates."""

    def test_render_forgiveness_takes_no_translation_dependency(self):
        import inspect

        params = inspect.signature(srr.render_residual_forgivable).parameters
        for name in params:
            self.assertNotIn("translate", name)
            self.assertNotIn("translator", name)

    def test_forgives_a_flagged_token_the_pipeline_already_cleared_by_provenance(self):
        # "SO" was already proven, by the pipeline's own per-token check, to
        # be explainable by the expected translation (accent-folded "SO") and
        # not by the source - this only trusts that existing verdict when the
        # whole-string shape match happened to fail on unrelated noise.
        forgiven = srr.render_residual_forgivable(
            flagged_tokens=["SO"],
            forgiven_ocr_noise_tokens=["SO"],
            source_text_coverage=1.0,
        )
        self.assertTrue(forgiven)

    def test_refuses_when_source_removal_is_incomplete(self):
        forgiven = srr.render_residual_forgivable(
            flagged_tokens=["SO"],
            forgiven_ocr_noise_tokens=["SO"],
            source_text_coverage=0.6,
        )
        self.assertFalse(forgiven)

    def test_never_forgives_a_token_the_source_genuinely_owned(self):
        # Pins the exact real regression this module must not reintroduce:
        # a token the source text actually contained is never noise, however
        # short it is - the per-token provenance check would never have put
        # it in ``forgiven_ocr_noise_tokens`` in the first place, and this
        # function must not second-guess that by shortness alone.
        forgiven = srr.render_residual_forgivable(
            flagged_tokens=["SO"],
            forgiven_ocr_noise_tokens=[],
            source_text_coverage=1.0,
        )
        self.assertFalse(forgiven)

    def test_refuses_a_flagged_token_not_individually_cleared(self):
        forgiven = srr.render_residual_forgivable(
            flagged_tokens=["MONSTERS"],
            forgiven_ocr_noise_tokens=["SO"],
            source_text_coverage=1.0,
        )
        self.assertFalse(forgiven)


class NeverSilentlyDisappearsTests(unittest.TestCase):
    """TEST 5 + TEST 6: manual_review_required only clears after every gate passes."""

    def test_branding_region_is_never_retried_and_keeps_its_flag(self):
        record = {
            "id": "BALAO_1",
            "manual_review_required": True,
            "translation_final_reason": "url",
            "text": "SOME-SCAN-SITE.EXAMPLE",
        }
        outcome = srr.run_selective_retry(record, page_index=3)
        self.assertEqual(outcome.cause, "branding")
        self.assertFalse(outcome.attempted)
        self.assertFalse(outcome.resolved)
        self.assertTrue(outcome.manual_review_required)

    def test_ocr_repair_alone_without_translation_pass_does_not_resolve(self):
        record = {
            "id": "BALAO_1",
            "manual_review_required": True,
            "translation_final_state": "skipped_with_reason",
            "translation_final_reason": "translation_not_selected",
            "quality_reasons": ["alphanumeric_ocr_artifact", "long_consonant_run"],
            "text": "SHE SAID THAT WAS ALL SHE HAD IN1 THE END, TRULY.",
        }
        outcome = srr.run_selective_retry(record, page_index=4)
        self.assertEqual(outcome.cause, "ocr")
        self.assertTrue(outcome.attempted)
        self.assertFalse(outcome.resolved)
        self.assertTrue(outcome.manual_review_required)
        self.assertTrue(outcome.detail["changed"])

    def test_ocr_repair_plus_passing_translation_pass_resolves(self):
        record = {
            "id": "BALAO_1",
            "manual_review_required": True,
            "translation_final_state": "skipped_with_reason",
            "translation_final_reason": "translation_not_selected",
            "quality_reasons": ["alphanumeric_ocr_artifact", "long_consonant_run"],
            "text": "SHE SAID THAT WAS ALL SHE HAD IN1 THE END, TRULY.",
        }

        def fake_translate(text, **kwargs):
            return "ELA DISSE QUE ERA TUDO O QUE TINHA NO FINAL, DE VERDADE."

        outcome = srr.run_selective_retry(
            record,
            page_index=4,
            translate_fn=fake_translate,
            validate_fn=validate_translation_text,
        )
        self.assertTrue(outcome.attempted)
        self.assertTrue(outcome.resolved)
        self.assertFalse(outcome.manual_review_required)

    def test_unresolved_cause_never_clears_the_flag(self):
        record = {
            "id": "BALAO_9",
            "manual_review_required": True,
            "translation_final_reason": "some_future_reason_this_module_does_not_know",
        }
        outcome = srr.run_selective_retry(record, page_index=9)
        self.assertFalse(outcome.attempted)
        self.assertTrue(outcome.manual_review_required)


class LastRunStructuralInvariantTests(unittest.TestCase):
    """TEST 9: page structure invariants hold on the real last completed job.

    No page count or chapter name is hardcoded here - the test reads whatever
    the real ``quality_report.json`` recorded and checks internal
    consistency, so it stays valid for any chapter/run.
    """

    # Trimmed (fields-only, no image paths) copy of the real last E2E job's
    # quality_report.json, kept as a versioned fixture so this test stays
    # hermetic (no read of the live output/ tree) while still asserting
    # against real captured pipeline data rather than synthetic numbers.
    FIXTURE = os.path.join(
        "test_fixtures", "84f40_last_run_quality_report_trimmed.json"
    )

    def _load(self):
        with open(self.FIXTURE, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_processed_pages_match_summary_and_pdf_page_count(self):
        data = self._load()
        summary = data["summary"]
        self.assertEqual(len(data["pages"]), summary["processed_images"])
        self.assertEqual(
            summary["quality_validation"]["pdf_pages"],
            summary["quality_validation"]["expected_pdf_pages"],
        )
        self.assertEqual(summary["available_valid_images"], summary["processed_images"])

    def test_every_manual_review_flag_is_still_present_in_the_saved_report(self):
        # Regression guard for #84F39: re-derive the authoritative count from
        # the raw per-page records and confirm it still matches the summary
        # totals this run computed - i.e. nothing between page scoring and the
        # saved report silently dropped a flagged region.
        data = self._load()
        flagged = set()

        def scan(node, page_index):
            if isinstance(node, dict):
                if node.get("manual_review_required") is True:
                    flagged.add((page_index, node.get("id") or node.get("region_id")))
                for value in node.values():
                    scan(value, page_index)
            elif isinstance(node, list):
                for item in node:
                    scan(item, page_index)

        for page in data["pages"]:
            scan(page, page["index"])

        self.assertEqual(len(flagged), data["totals"]["manual_review_required_groups"])


if __name__ == "__main__":
    unittest.main()
