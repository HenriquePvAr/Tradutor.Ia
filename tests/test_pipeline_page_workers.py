import os
import threading
import time
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

import benchmark_pipeline as bp
from page_processing_contracts import PreTranslationPageResult


class PipelinePageWorkersTests(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("PIPELINE_PAGE_WORKERS")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("PIPELINE_PAGE_WORKERS", None)
        else:
            os.environ["PIPELINE_PAGE_WORKERS"] = self._old

    def _states(self, count=2):
        return [{"index": i, "image_path": f"page-{i}.png", "raw_lines": []} for i in range(count)]

    def test_default_and_invalid_worker_values(self):
        os.environ.pop("PIPELINE_PAGE_WORKERS", None)
        self.assertEqual(bp._pipeline_page_workers(), 2)
        for value in ("0", "-1", "3", "nope"):
            os.environ["PIPELINE_PAGE_WORKERS"] = value
            with self.assertRaises(ValueError):
                bp._pipeline_page_workers()

    def test_workers_one_uses_serial_path(self):
        os.environ["PIPELINE_PAGE_WORKERS"] = "1"
        calls = []

        def fake(context, **kwargs):
            calls.append(context.page_index)
            return PreTranslationPageResult(page_index=context.page_index)

        with patch.object(bp, "process_pre_translation_page", side_effect=fake):
            results = bp.process_pre_translation_pages(
                self._states(), image_paths=[], ocr_lang="eng",
                fast_ocr_budget=object(), errors_folder=".",
            )
        self.assertEqual(calls, [0, 1])
        self.assertEqual([r.page_index for r in results], [0, 1])

    def test_workers_two_overlap_and_are_deterministic(self):
        os.environ["PIPELINE_PAGE_WORKERS"] = "2"
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        active = 0
        max_active = 0

        def fake(context, **kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            barrier.wait(timeout=2)
            time.sleep(0.01)
            with lock:
                active -= 1
            return PreTranslationPageResult(page_index=context.page_index)

        with patch.object(bp, "process_pre_translation_page", side_effect=fake):
            results = bp.process_pre_translation_pages(
                list(reversed(self._states())), image_paths=[], ocr_lang="eng",
                fast_ocr_budget=object(), errors_folder=".",
            )
        self.assertGreaterEqual(max_active, 2)
        self.assertLessEqual(max_active, 2)
        self.assertEqual([r.page_index for r in results], [0, 1])

    def test_worker_error_propagates(self):
        os.environ["PIPELINE_PAGE_WORKERS"] = "2"

        def fake(context, **kwargs):
            if context.page_index == 1:
                raise RuntimeError("page failure")
            return PreTranslationPageResult(page_index=context.page_index)

        with patch.object(bp, "process_pre_translation_page", side_effect=fake):
            with self.assertRaises(RuntimeError):
                bp.process_pre_translation_pages(
                    self._states(), image_paths=[], ocr_lang="eng",
                    fast_ocr_budget=object(), errors_folder=".",
                )

    def test_rapidocr_lane_is_bounded_to_one(self):
        lock = threading.Lock()
        active = 0
        maximum = 0

        def lane():
            nonlocal active, maximum
            with bp._RAPIDOCR_INFERENCE_LOCK:
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.01)
                with lock:
                    active -= 1

        threads = [threading.Thread(target=lane) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(maximum, 1)

    def test_serial_and_parallel_post_render_overflow_policy_match(self):
        def make_states(root):
            states = []
            for index in range(2):
                path = Path(root) / f"page-{index}.png"
                group = type("Group", (), {
                    "sent_to_translation": True,
                    "text_overflow_ratio": 1.0,
                })()
                states.append({
                    "index": index,
                    "original_bgr": np.full((12, 12, 3), 255, dtype=np.uint8),
                    "raw_lines": [], "candidates": [], "groups": [group],
                    "image_path": str(path), "output_path": str(path),
                })
            return states

        class FakeTranslator:
            def translate_strict(self, *_args, **_kwargs):
                return "short"

        def fake_render(*_args, **kwargs):
            image = np.full((12, 12, 3), 255, dtype=np.uint8)
            image[2:10, 2:10] = 0
            return image, {}

        def fake_retry(groups, _translator, records, **_kwargs):
            for group in groups:
                group.text_overflow_ratio = 0.0
            records.append({"retry_type": "layout_overflow", "valid": True})
            return len(groups)

        with tempfile.TemporaryDirectory() as temp:
            serial = make_states(temp + "\\serial")
            parallel = make_states(temp + "\\parallel")
            Path(temp + "\\serial").mkdir()
            Path(temp + "\\parallel").mkdir()
            serial_records, parallel_records = [], []
            with patch.object(bp, "render_analyzed_image", side_effect=fake_render), \
                 patch.object(bp, "_retry_layout_overflow_translations", side_effect=fake_retry), \
                 patch.object(bp.config, "TRANSLATION_MAX_RETRIES", 1), \
                 patch.dict(os.environ, {"PIPELINE_PAGE_WORKERS": "1"}):
                bp.process_post_translation_pages(serial, translator=FakeTranslator(),
                                                   translation_retry_records=serial_records)
            with patch.object(bp, "render_analyzed_image", side_effect=fake_render), \
                 patch.object(bp, "_retry_layout_overflow_translations", side_effect=fake_retry), \
                 patch.object(bp.config, "TRANSLATION_MAX_RETRIES", 1), \
                 patch.dict(os.environ, {"PIPELINE_PAGE_WORKERS": "2"}):
                bp.process_post_translation_pages(parallel, translator=FakeTranslator(),
                                                   translation_retry_records=parallel_records)
            assert len(serial_records) == len(parallel_records) == 2
            assert all(g.text_overflow_ratio == 0.0 for s in serial + parallel for g in s["groups"])

    def test_sequential_ocr_honors_cooperative_cancel_before_new_work(self):
        cancel = threading.Event()
        cancel.set()
        import ocr_parallel
        with patch.object(ocr_parallel, "OCREngine") as engine:
            from ocr_parallel import _detect_sequential
            result = _detect_sequential(
                [{"index": 0, "image_path": "never-read.png"}],
                "eng", cancel_event=cancel,
            )
        assert result == {}
        engine.return_value.detect_lines.assert_not_called()


if __name__ == "__main__":
    unittest.main()
