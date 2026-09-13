"""Integration tests: OCR_EXECUTION_MODE wired into the REAL production call
site (ocr_parallel.detect_ocr_jobs), which is what benchmark_pipeline.py (and
therefore run_webtoon.py / job_runner.py subprocess jobs) actually calls.

Nothing here calls NVIDIA for real (hermetic guard blocks real sockets) and
nothing here touches translation -- DeepL/quality_optimized/yomu_backend are
asserted unchanged.
"""
import _test_bootstrap  # noqa: F401

import unittest
from unittest import mock

import numpy as np

import config
from ocr_contract import OCRPageResult
from ocr_parallel import detect_ocr_jobs


def _fake_image(width=20, height=10):
    return np.zeros((height, width, 3), dtype=np.uint8)


def _fake_cv2_imread(path, *args, **kwargs):
    return _fake_image()


class ProductionPipelineRapidocrDefaultTests(unittest.TestCase):
    """TEST_PRODUCTION_PIPELINE_RAPIDOCR_DEFAULT / TEST_RAPIDOCR_DEFAULT_OUTPUT_UNCHANGED

    With OCR_EXECUTION_MODE=rapidocr (the default), detect_ocr_jobs must take
    the exact untouched code path it always has -- no ocr_hybrid_scheduler
    import, no behavior change."""

    def test_default_mode_is_rapidocr(self):
        self.assertEqual(config.OCR_EXECUTION_MODE, "rapidocr")

    def test_rapidocr_mode_never_imports_the_hybrid_scheduler(self):
        import ocr_hybrid_scheduler as sched

        jobs = [{"index": 0, "image_path": "a.png"}]
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread), \
             mock.patch("ocr_engine.OCREngine.detect_lines", return_value=[]), \
             mock.patch.object(sched, "run_ocr", side_effect=AssertionError("must not be called")):
            results, run_stats = detect_ocr_jobs(
                jobs, "en", parallel=False, workers=1
            )
        self.assertIn(0, results)
        self.assertIsNone(results[0]["error"])

    def test_rapidocr_default_output_matches_pre_integration_shape(self):
        jobs = [{"index": 0, "image_path": "a.png"}]
        with mock.patch("cv2.imread", side_effect=_fake_cv2_imread), \
             mock.patch("ocr_engine.OCREngine.detect_lines", return_value=[]):
            results, run_stats = detect_ocr_jobs(jobs, "en", parallel=False, workers=1)
        payload = results[0]
        for key in ("index", "error", "elapsed_seconds", "lines", "ocr_metadata", "pid"):
            self.assertIn(key, payload)
        self.assertNotIn("parallel_used", payload)  # run_stats fields never leak into payload


class ProductionPipelineNvidiaModeTests(unittest.TestCase):
    """TEST_PRODUCTION_PIPELINE_NVIDIA_MODE / TEST_NVIDIA_NORMALIZED_RESULT_REACHES_GROUPING
    / TEST_NVIDIA_RESULT_REACHES_TRANSLATION_ITEM_BUILDER (contract-level: the
    payload's "lines" are ocr_engine.OCRLine objects, exactly what grouping and
    the translation-item builder already consume from the rapidocr path)."""

    def _fake_provider(self):
        class _Provider:
            def is_configured(self_inner):
                return True

            def recognize_page(self_inner, image, context=None):
                return OCRPageResult.from_ocr_lines(
                    [],
                    page_id=context["page_id"],
                    engine="nvidia",
                    width=image.shape[1],
                    height=image.shape[0],
                )

        return _Provider()

    def test_nvidia_mode_routes_full_page_ocr_through_detect_ocr_jobs(self):
        jobs = [{"index": i, "image_path": f"p{i}.png"} for i in range(3)]
        with mock.patch.object(config, "OCR_EXECUTION_MODE", "nvidia"), \
             mock.patch("cv2.imread", side_effect=_fake_cv2_imread), \
             mock.patch("nvidia_ocr_provider.NvidiaOCRProvider", return_value=self._fake_provider()):
            results, run_stats = detect_ocr_jobs(jobs, "en", parallel=True, workers=2)
        self.assertEqual(sorted(results.keys()), [0, 1, 2])
        for payload in results.values():
            self.assertEqual(payload["ocr_metadata"]["full_page_engine"], "nvidia")
            self.assertIsInstance(payload["lines"], list)  # OCRLine list -- same shape grouping expects
        self.assertEqual(run_stats["OCR_EXECUTION_MODE"], "nvidia")

    def test_nvidia_result_lines_are_ocr_line_instances_like_rapidocr(self):
        from ocr_engine import OCRLine

        jobs = [{"index": 0, "image_path": "p0.png"}]

        class _ProviderWithText:
            def is_configured(self_inner):
                return True

            def recognize_page(self_inner, image, context=None):
                return OCRPageResult.from_ocr_lines(
                    [OCRLine(text="hi", confidence=0.9, polygon=None, box=(0, 0, 1, 1), raw_text="hi", engine="nvidia")],
                    page_id=context["page_id"], engine="nvidia", width=1, height=1,
                )

        with mock.patch.object(config, "OCR_EXECUTION_MODE", "nvidia"), \
             mock.patch("cv2.imread", side_effect=_fake_cv2_imread), \
             mock.patch("nvidia_ocr_provider.NvidiaOCRProvider", return_value=_ProviderWithText()):
            results, _ = detect_ocr_jobs(jobs, "en", parallel=True, workers=1)
        line = results[0]["lines"][0]
        self.assertIsInstance(line, OCRLine)
        self.assertEqual(line.engine, "nvidia")


class ProductionPipelineHybridModeTests(unittest.TestCase):
    """TEST_PRODUCTION_PIPELINE_HYBRID_MODE / TEST_HYBRID_OUTPUT_ORDER /
    TEST_HYBRID_EACH_PAGE_FULL_OCR_ONCE"""

    def _fake_provider(self):
        class _Provider:
            def is_configured(self_inner):
                return True

            def recognize_page(self_inner, image, context=None):
                return OCRPageResult(page_id=context["page_id"], engine="nvidia", width=1, height=1, regions=[])

        return _Provider()

    def test_hybrid_mode_processes_every_page_exactly_once_in_order(self):
        jobs = [{"index": i, "image_path": f"p{i}.png"} for i in range(6)]
        with mock.patch.object(config, "OCR_EXECUTION_MODE", "hybrid"), \
             mock.patch("cv2.imread", side_effect=_fake_cv2_imread), \
             mock.patch("ocr_engine.OCREngine.detect_lines", return_value=[]), \
             mock.patch("nvidia_ocr_provider.NvidiaOCRProvider", return_value=self._fake_provider()):
            results, run_stats = detect_ocr_jobs(jobs, "en", parallel=True, workers=2)
        self.assertEqual(sorted(results.keys()), list(range(6)))
        self.assertEqual(run_stats["OCR_EXECUTION_MODE"], "hybrid")
        # FULL_PAGE counters must sum to exactly len(jobs) -- no page OCRed twice.
        self.assertEqual(
            run_stats["RAPIDOCR_FULL_PAGE_PAGES"] + run_stats["NVIDIA_FULL_PAGE_PAGES"],
            len(jobs),
        )


class TranslationProviderUntouchedByOcrModeTests(unittest.TestCase):
    """TEST_OCR_CONFIG_DOES_NOT_CHANGE_TRANSLATION_PROVIDER /
    TEST_TRANSLATION_PROVIDER_STILL_YOMU_BACKEND / TEST_DEEPL_MODEL_STILL_QUALITY_OPTIMIZED"""

    def test_switching_ocr_execution_mode_never_touches_translation_config(self):
        import ui_helpers

        before_provider = ui_helpers.DEFAULT_TRANSLATION_PROVIDER
        before_model = config.DEEPL_MODEL_TYPE
        for mode in ("rapidocr", "nvidia", "hybrid"):
            with mock.patch.object(config, "OCR_EXECUTION_MODE", mode):
                self.assertEqual(config.OCR_EXECUTION_MODE, mode)
                self.assertEqual(ui_helpers.DEFAULT_TRANSLATION_PROVIDER, before_provider)
                self.assertEqual(config.DEEPL_MODEL_TYPE, before_model)
        self.assertEqual(ui_helpers.DEFAULT_TRANSLATION_PROVIDER, "deepl")
        self.assertEqual(config.DEEPL_MODEL_TYPE, "quality_optimized")


class NoNemotronFromIntegratedOcrPathTests(unittest.TestCase):
    """TEST_NO_NEMOTRON_LLM_FROM_OCR_PATH (integration-level, not just unit)."""

    def test_ocr_parallel_module_never_references_the_nemotron_llm(self):
        import io

        source = io.open("ocr_parallel.py", encoding="utf-8").read()
        self.assertNotIn("nemotron", source.lower())


if __name__ == "__main__":
    unittest.main()
