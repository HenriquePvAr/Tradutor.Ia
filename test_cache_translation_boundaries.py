from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import config
from ocr_balloon import TextGroup, apply_group_translations
from ocr_engine import OCRLine
from pipeline_cache import (
    load_ocr_cache,
    ocr_cache_key,
    ocr_runtime_fingerprint,
    save_ocr_cache,
)
from translator_nvidia import TranslatorNvidiaBatch


def _line(text="HELLO"):
    return OCRLine(
        text=text,
        confidence=0.99,
        polygon=np.array([[0, 0], [120, 0], [120, 40], [0, 40]], dtype=np.int32),
        box=(0, 0, 120, 40),
        raw_text=text,
        engine="rapidocr",
        page=1,
    )


class OcrCacheBoundaryTests(unittest.TestCase):
    def _cache_config(self, folder):
        return patch.multiple(
            config,
            CACHE_ROOT=folder,
            OCR_ENGINE="rapidocr",
            OCR_FALLBACK_ENGINE="paddle",
            OCR_HYBRID_FALLBACK=True,
            RAPIDOCR_ENABLED=True,
            RAPIDOCR_MIN_CONFIDENCE=0.35,
            RAPIDOCR_SUSPICIOUS_TEXT_FALLBACK=True,
            RAPIDOCR_PAGE_FALLBACK=True,
            OCR_TEXT_REPAIR=True,
            OCR_TEXT_REPAIR_MODE="safe",
        )

    def test_failed_engine_cache_is_rejected_without_force(self):
        with tempfile.TemporaryDirectory() as folder, self._cache_config(folder):
            key = ocr_cache_key("image-sha", "eng")
            save_ocr_cache(
                key,
                "image-sha",
                "eng",
                [],
                0.01,
                {},
                ocr_metadata={
                    "original_engine": "rapidocr",
                    "final_engine": "rapidocr",
                    "fallback_reason": "rapidocr_error:ModuleNotFoundError",
                    "engine_unavailable": True,
                },
            )

            self.assertIsNone(load_ocr_cache(key))

    def test_valid_blank_ocr_cache_remains_reusable(self):
        with tempfile.TemporaryDirectory() as folder, self._cache_config(folder):
            key = ocr_cache_key("blank-sha", "eng")
            save_ocr_cache(
                key,
                "blank-sha",
                "eng",
                [],
                0.01,
                {},
                ocr_metadata={
                    "original_engine": "rapidocr",
                    "final_engine": "rapidocr",
                    "fallback_reason": "",
                    "engine_unavailable": False,
                },
            )

            cached = load_ocr_cache(key)
            self.assertIsNotNone(cached)
            self.assertEqual(cached[0], [])
            self.assertEqual(cached[1]["status"], "empty_success")

    def test_valid_nonempty_cache_hits_and_preserves_runtime_fingerprint(self):
        with tempfile.TemporaryDirectory() as folder, self._cache_config(folder):
            key = ocr_cache_key("text-sha", "eng")
            save_ocr_cache(
                key,
                "text-sha",
                "eng",
                [_line("PLEASE WAIT")],
                0.02,
                {},
                ocr_metadata={
                    "original_engine": "rapidocr",
                    "final_engine": "rapidocr",
                    "fallback_reason": "",
                    "engine_unavailable": False,
                },
            )

            cached = load_ocr_cache(key)
            self.assertIsNotNone(cached)
            self.assertEqual([line.text for line in cached[0]], ["PLEASE WAIT"])
            self.assertEqual(cached[1]["status"], "success")
            self.assertEqual(
                cached[1]["runtime_fingerprint"],
                ocr_runtime_fingerprint(),
            )

    def test_runtime_fingerprint_mismatch_rejects_cache(self):
        with tempfile.TemporaryDirectory() as folder, self._cache_config(folder):
            key = ocr_cache_key("text-sha", "eng")
            save_ocr_cache(
                key,
                "text-sha",
                "eng",
                [_line("PLEASE WAIT")],
                0.02,
                {},
                ocr_metadata={"engine_unavailable": False},
            )
            cache_file = Path(folder) / "ocr" / f"{key}.json"
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            payload["runtime_fingerprint"]["packages"]["rapidocr-onnxruntime"] = "0.0.0"
            cache_file.write_text(json.dumps(payload), encoding="utf-8")

            self.assertIsNone(load_ocr_cache(key))


class TranslationBoundaryTests(unittest.TestCase):
    def test_unconfigured_nvidia_provider_is_explicit_failure_not_api_success(self):
        translator = TranslatorNvidiaBatch(api_key="", enable_cache=False)

        translated = translator.translate_many(["PLEASE WAIT", "RUN NOW"])

        self.assertEqual(translated, ["PLEASE WAIT", "RUN NOW"])
        self.assertEqual(translator.stats["api_texts"], 0)
        self.assertEqual(translator.stats["api_requests"], 0)
        self.assertEqual(translator.stats["failed_batches"], 1)
        self.assertEqual(translator.stats["translation_configuration_missing"], 1)
        self.assertEqual(translator.stats["last_transport_reason"], "provider_not_configured")

    def test_provider_failure_is_explicit_and_keeps_batch_trace(self):
        translator = TranslatorNvidiaBatch(api_key="test", enable_cache=False)
        with patch.object(translator, "_translate_batch", side_effect=RuntimeError("boom")):
            translated = translator.translate_many(["PLEASE WAIT", "RUN NOW"])

        self.assertEqual(translated, ["PLEASE WAIT", "RUN NOW"])
        self.assertEqual(translator.stats["translation_batches"], 1)
        self.assertEqual(translator.stats["failed_batches"], 1)
        self.assertEqual(translator.stats["successful_batches"], 0)

    def test_empty_response_is_explicit_failed_batch(self):
        translator = TranslatorNvidiaBatch(api_key="test", enable_cache=False)
        with patch.object(translator, "_translate_batch", return_value=["", ""]):
            translated = translator.translate_many(["PLEASE WAIT", "RUN NOW"])

        self.assertEqual(translated, ["PLEASE WAIT", "RUN NOW"])
        self.assertEqual(translator.stats["translation_batches"], 1)
        self.assertEqual(translator.stats["successful_batches"], 0)
        self.assertEqual(translator.stats["failed_batches"], 1)
        self.assertEqual(translator.stats["last_transport_reason"], "empty_translation_response")
        self.assertEqual(translator.stats["translation_responses_received"], 2)
        self.assertEqual(translator.stats["translation_responses_nonempty"], 0)
        self.assertEqual(translator.stats["translation_results_associated"], 0)

    def test_happy_path_associates_translations_and_reaches_translated_state(self):
        translator = TranslatorNvidiaBatch(api_key="test", enable_cache=False)
        with patch.object(
            translator,
            "_translate_batch",
            return_value=["POR FAVOR ESPERE", "CORRA AGORA"],
        ):
            translated = translator.translate_many(["PLEASE WAIT", "RUN NOW"])

        groups = [
            TextGroup(group_id="G1", lines=[_line("PLEASE WAIT")]),
            TextGroup(group_id="G2", lines=[_line("RUN NOW")]),
        ]
        groups[0].text = "PLEASE WAIT"
        groups[1].text = "RUN NOW"
        apply_group_translations(groups, translated)

        self.assertEqual(translator.stats["successful_batches"], 1)
        self.assertEqual(translator.stats["translation_responses_nonempty"], 2)
        self.assertTrue(all(group.sent_to_translation for group in groups))
        self.assertEqual(
            [group.translation_final_state for group in groups],
            ["translated", "translated"],
        )

    def test_partial_batch_failure_keeps_success_and_failure_counters(self):
        translator = TranslatorNvidiaBatch(
            api_key="test",
            enable_cache=False,
            batch_size=1,
            parallel=False,
        )
        calls = iter([["POR FAVOR ESPERE"], RuntimeError("boom")])

        def fake_batch(_texts):
            result = next(calls)
            if isinstance(result, Exception):
                raise result
            return result

        with patch.object(translator, "_translate_batch", side_effect=fake_batch):
            translated = translator.translate_many(["PLEASE WAIT", "RUN NOW"])

        self.assertEqual(translated, ["POR FAVOR ESPERE", "RUN NOW"])
        self.assertEqual(translator.stats["translation_batches"], 2)
        self.assertEqual(translator.stats["successful_batches"], 1)
        self.assertEqual(translator.stats["failed_batches"], 1)


if __name__ == "__main__":
    unittest.main()
