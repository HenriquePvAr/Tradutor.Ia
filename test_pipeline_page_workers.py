import os
import threading
import time
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
