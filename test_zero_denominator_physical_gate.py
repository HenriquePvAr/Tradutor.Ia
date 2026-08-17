"""TDD #42 - a zero physical denominator is a question, not a pass.

Full E2E #11 selected the ``quality`` processing mode, which maps to PaddleOCR.
PaddleOCR was not installed, so all 98 text pages failed OCR, zero translation
regions survived, and the physical gate compared 0 expected against 0 checked
and reported PASS.  Two guards are covered here: the gate must not pass on an
empty population it never examined, and an unavailable engine must stop the run
before the chapter is downloaded and processed at all.
"""

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest
from unittest.mock import patch

import benchmark_pipeline
import ocr_engine
import output_manifest
import run_webtoon


def _speech_item(**overrides):
    item = {
        "id": "BALAO_1",
        "classification": "speech",
        "translation": "TEXTO TRADUZIDO",
        "translation_valid": True,
        "redrawn": True,
        "translation_final_state": "translated",
        "translation_final_reason": "ok",
        "source_completeness_status": "pass",
    }
    item.update(overrides)
    return item


def _page(index, *, status="completed", items=(), **extra):
    state = {
        "index": index,
        "status": status,
        "debug_data": {"items": list(items)},
    }
    state.update(extra)
    return state


def _ocr_failed_page(index, engine="paddle"):
    """A page exactly as Full #11 persisted it when the engine was missing."""
    return _page(
        index,
        status="completed_with_error",
        ocr_error=f"ocr_engine_unavailable:{engine}_error:ModuleNotFoundError",
        precheck={"skip": False, "reason": "uncertain_run_ocr"},
    )


def _blank_page(index):
    """A page the no-text precheck positively proved carries no text."""
    return _page(index, precheck={"skip": True, "reason": "nearly_flat_low_edges"})


class ZeroDenominatorPhysicalGateTest(unittest.TestCase):
    def test_full_11_class_no_longer_passes_on_an_empty_population(self):
        # 98 pages whose OCR never ran, 2 legitimately blank: zero expected
        # regions here means the text stage collapsed, not that the chapter had
        # no dialogue.  This is the exact shape that reported PASS before.
        states = [_ocr_failed_page(index) for index in range(1, 99)]
        states += [_blank_page(99), _blank_page(100)]

        result = benchmark_pipeline._physical_residual_accounting(states)

        self.assertEqual(result["physical_regions_expected"], 0)
        self.assertFalse(result["physical_gate_passed"])
        self.assertEqual(result["physical_decision"], "review")
        self.assertEqual(result["physical_population_status"], "incomplete")
        self.assertEqual(
            result["zero_denominator_reason"], "upstream_text_analysis_incomplete"
        )
        self.assertEqual(result["physical_population"]["pages_upstream_failed"], 98)

    def test_a_chapter_proven_to_carry_no_text_still_passes(self):
        # Every page examined, none failed, none carried text.  Zero expected
        # regions is the truth about the source here, so PASS stays available.
        states = [_blank_page(index) for index in range(1, 6)]

        result = benchmark_pipeline._physical_residual_accounting(states)

        self.assertEqual(result["physical_regions_expected"], 0)
        self.assertTrue(result["physical_gate_passed"])
        self.assertEqual(result["physical_decision"], "pass")
        self.assertEqual(
            result["zero_denominator_reason"], "no_translatable_source_text"
        )

    def test_analysed_pages_without_dialogue_also_pass(self):
        # OCR ran and found nothing translatable.  That is positive evidence
        # too, not a silent hole.
        states = [_page(index) for index in range(1, 4)]

        result = benchmark_pipeline._physical_residual_accounting(states)

        self.assertTrue(result["physical_gate_passed"])
        self.assertEqual(
            result["zero_denominator_reason"], "no_translatable_source_text"
        )
        self.assertEqual(result["physical_population"]["pages_text_analyzed"], 3)

    def test_zero_regions_without_any_page_evidence_fails_closed(self):
        result = benchmark_pipeline._physical_residual_accounting([])

        self.assertFalse(result["physical_gate_passed"])
        self.assertEqual(
            result["zero_denominator_reason"], "source_text_evidence_missing"
        )

    def test_ocr_failures_beside_surviving_regions_block_the_gate(self):
        # The surviving regions are all clean, but they only describe the pages
        # that worked.  Validating them as the whole chapter is the same
        # vacuous-truth mistake at a smaller scale.
        states = [
            _page(1, items=[_speech_item()]),
            _ocr_failed_page(2),
        ]

        result = benchmark_pipeline._physical_residual_accounting(states)

        self.assertEqual(result["physical_regions_expected"], 1)
        self.assertEqual(result["physical_regions_translated"], 1)
        self.assertEqual(result["physical_source_residual_count"], 0)
        self.assertFalse(result["physical_gate_passed"])
        self.assertEqual(result["physical_decision"], "review")
        self.assertEqual(result["physical_population_status"], "incomplete")

    def test_a_healthy_translated_chapter_is_unchanged(self):
        states = [_page(index, items=[_speech_item()]) for index in range(1, 4)]

        result = benchmark_pipeline._physical_residual_accounting(states)

        self.assertEqual(result["physical_regions_expected"], 3)
        self.assertEqual(result["physical_regions_translated"], 3)
        self.assertTrue(result["physical_gate_passed"])
        self.assertEqual(result["physical_decision"], "pass")
        self.assertNotIn("zero_denominator_reason", result)

    def test_residual_source_still_reviews_with_a_complete_population(self):
        # TDD #38 semantics: a real residual is still a residual, and it is not
        # reported as a population problem.
        states = [
            _page(
                1,
                items=[
                    _speech_item(
                        redrawn=False,
                        translation_final_state="manual_review",
                        manual_review_required=True,
                    )
                ],
            )
        ]

        result = benchmark_pipeline._physical_residual_accounting(states)

        self.assertEqual(result["physical_source_residual_count"], 1)
        self.assertFalse(result["physical_gate_passed"])
        self.assertEqual(result["physical_decision"], "review")
        self.assertEqual(result["physical_population_status"], "complete")


class PhysicalPopulationManifestTest(unittest.TestCase):
    def test_population_evidence_survives_manifest_sanitisation(self):
        accounting = benchmark_pipeline._physical_residual_accounting(
            [_ocr_failed_page(1)]
        )

        sanitized = output_manifest.sanitize_physical_quality(accounting)

        self.assertFalse(sanitized["physical_gate_passed"])
        self.assertEqual(sanitized["physical_decision"], "review")
        self.assertEqual(sanitized["physical_population_status"], "incomplete")
        self.assertEqual(sanitized["physical_population"]["pages_upstream_failed"], 1)
        self.assertEqual(
            sanitized["zero_denominator_reason"], "upstream_text_analysis_incomplete"
        )
        # Idempotent, so manifest validation still accepts what we just wrote.
        self.assertEqual(
            sanitized, output_manifest.sanitize_physical_quality(sanitized)
        )

    def test_a_legacy_block_without_the_new_evidence_stays_valid(self):
        legacy = {
            "physical_regions_expected": 4,
            "physical_regions_translated": 4,
            "physical_regions_preserved": 0,
            "physical_regions_review_source_retained": 0,
            "physical_regions_render_failed": 0,
            "physical_regions_other_explicit": 0,
            "physical_source_residual_count": 0,
            "physical_source_residual_group_ids": [],
            "physical_gate_passed": True,
        }

        sanitized = output_manifest.sanitize_physical_quality(legacy)

        self.assertNotIn("physical_decision", sanitized)
        self.assertNotIn("zero_denominator_reason", sanitized)
        self.assertTrue(sanitized["physical_gate_passed"])


class OcrEngineAvailabilityTest(unittest.TestCase):
    def test_a_missing_dependency_is_reported_as_unavailable(self):
        with patch.object(ocr_engine.importlib.util, "find_spec", return_value=None):
            available, reason = ocr_engine.engine_availability("paddle")

        self.assertFalse(available)
        self.assertEqual(reason, "dependency_unavailable")

    def test_an_installed_engine_is_reported_as_available(self):
        with patch.object(
            ocr_engine.importlib.util, "find_spec", return_value=object()
        ):
            self.assertEqual(ocr_engine.engine_availability("paddle"), (True, ""))
            self.assertEqual(ocr_engine.engine_availability("rapidocr"), (True, ""))

    def test_an_unknown_engine_fails_closed_without_a_default(self):
        available, reason = ocr_engine.engine_availability("definitely_not_an_engine")

        self.assertFalse(available)
        self.assertEqual(reason, "unknown_engine")
        with self.assertRaises(ocr_engine.OCREngineUnavailableError) as caught:
            ocr_engine.require_available_engine("definitely_not_an_engine")
        self.assertEqual(caught.exception.reason_class, "unknown_engine")

    def test_the_diagnostic_names_the_engine_without_leaking_the_environment(self):
        with patch.object(ocr_engine.importlib.util, "find_spec", return_value=None):
            with self.assertRaises(ocr_engine.OCREngineUnavailableError) as caught:
                ocr_engine.require_available_engine("paddle")

        message = str(caught.exception)
        self.assertIn("paddle", message)
        self.assertIn("disponivel=false", message)
        self.assertIn("dependency_unavailable", message)
        for leaked in ("\\", "/", "=C:", "TOKEN", "KEY"):
            self.assertNotIn(leaked, message)


class ModeEnginePreflightTest(unittest.TestCase):
    """The mode -> engine mapping is where an unavailable engine has to stop."""

    def setUp(self):
        import config

        # _configure_mode writes both os.environ and config module attributes;
        # neither may leak into another test.
        env = patch.dict("os.environ", {"TRADUTOR_OCR_ENGINE_OVERRIDE": ""})
        env.start()
        self.addCleanup(env.stop)
        for name in (
            "OCR_ENGINE",
            "OCR_FALLBACK_ENGINE",
            "OCR_HYBRID_FALLBACK",
            "RAPIDOCR_ENABLED",
            "RAPIDOCR_PAGE_FALLBACK",
            "OCR_REGION_SELECTIVE_FALLBACK",
            "FAST_OCR_MODE",
            "POST_RENDER_OCR_VALIDATION",
        ):
            attr = patch.object(config, name, getattr(config, name, None))
            attr.start()
            self.addCleanup(attr.stop)

    def test_quality_mode_selects_paddle_and_fast_mode_selects_rapidocr(self):
        seen = []

        def record(engine):
            seen.append(engine)
            return engine

        with patch.object(ocr_engine, "require_available_engine", record):
            self.assertEqual(run_webtoon._configure_mode("quality"), "paddle")
            self.assertEqual(run_webtoon._configure_mode("fast"), "rapidocr")

        self.assertEqual(seen, ["paddle", "rapidocr"])

    def test_quality_mode_stops_before_any_chapter_work_when_paddle_is_missing(self):
        with patch.object(ocr_engine.importlib.util, "find_spec", return_value=None):
            with self.assertRaises(ocr_engine.OCREngineUnavailableError) as caught:
                run_webtoon._configure_mode("quality")

        self.assertEqual(caught.exception.engine, "paddle")
        self.assertEqual(caught.exception.reason_class, "dependency_unavailable")

    def test_no_silent_fallback_to_the_other_engine(self):
        # RapidOCR being installed must not rescue a run that asked for Paddle.
        def only_rapidocr(name):
            return object() if name.startswith("rapidocr") else None

        with patch.object(
            ocr_engine.importlib.util, "find_spec", side_effect=only_rapidocr
        ):
            with self.assertRaises(ocr_engine.OCREngineUnavailableError):
                run_webtoon._configure_mode("quality")

    def test_fast_mode_runs_normally_when_rapidocr_is_available(self):
        import config

        def only_rapidocr(name):
            return object() if name.startswith("rapidocr") else None

        with patch.object(
            ocr_engine.importlib.util, "find_spec", side_effect=only_rapidocr
        ):
            self.assertEqual(run_webtoon._configure_mode("fast"), "rapidocr")

        self.assertEqual(config.OCR_ENGINE, "rapidocr")
        self.assertTrue(config.FAST_OCR_MODE)


if __name__ == "__main__":
    unittest.main()
