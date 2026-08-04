"""Hermetic tests for bounded Fast OCR and progress ownership."""

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import config
import ocr_parallel
import run_webtoon
import process_options
from fast_ocr_policy import FastOCRBudget, run_ocr_with_timeout
from ui_helpers import ProgressSnapshot, parse_progress_line


def _sleep_child(connection):
    time.sleep(5)


class FastOCRPolicyTests(unittest.TestCase):
    def setUp(self):
        # run_webtoon._configure_mode() mutates arbitrary config attributes
        # as a side effect (OCR_ENGINE, RAPIDOCR_ENABLED,
        # POST_RENDER_OCR_VALIDATION, ...), not just the fallback-policy
        # names below. Snapshot the whole module namespace so nothing it
        # touches can leak into later tests, regardless of which attributes
        # _configure_mode is extended to set in the future.
        self._config_snapshot = vars(config).copy()

    def tearDown(self):
        vars(config).clear()
        vars(config).update(self._config_snapshot)

    def test_default_fast_budget_is_fail_closed_for_heavy_fallback(self):
        budget = FastOCRBudget.from_config(fast=True)
        allowed, reason = budget.allow(kind="paddle_full_page", page=1)
        self.assertFalse(allowed)
        self.assertEqual(reason, "fast_ocr_heavy_fallback_disabled")

    def test_budget_limits_full_pages_and_regions(self):
        budget = FastOCRBudget(
            enabled=True,
            heavy_fallback=True,
            max_full_pages=1,
            max_full_regions=1,
            total_budget_seconds=1,
        )
        self.assertTrue(budget.allow(kind="paddle_full_page", page=1)[0])
        budget.record(kind="paddle_full_page", elapsed=0.1, page=1)
        self.assertFalse(budget.allow(kind="paddle_full_page", page=2)[0])
        self.assertTrue(budget.allow(kind="paddle_full_region", page=1)[0])
        budget.record(kind="paddle_full_region", elapsed=0.1, page=1)
        self.assertFalse(budget.allow(kind="paddle_full_region", page=1)[0])

    def test_native_timeout_terminates_owned_child(self):
        started = time.perf_counter()
        lines, metadata = run_ocr_with_timeout(
            None,
            lang="en",
            engine_name="paddle_mobile",
            page=1,
            timeout_seconds=0.2,
            worker_target=_sleep_child,
        )
        self.assertEqual(lines, [])
        self.assertTrue(metadata["timeout"])
        self.assertLess(time.perf_counter() - started, 3.0)

    def test_windows_multiprocessing_selects_console_free_pythonw(self):
        with patch.object(process_options.os, "name", "nt"), \
                patch.object(process_options.Path, "is_file", return_value=True), \
                patch("multiprocessing.set_executable") as set_executable:
            process_options.configure_hidden_multiprocessing()
        set_executable.assert_called_once()
        self.assertTrue(str(set_executable.call_args.args[0]).lower().endswith("pythonw.exe"))

    def test_sequential_success_and_error_emit_checkpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for index in (1, 2):
                path = Path(tmp) / f"{index}.png"
                path.write_bytes(b"placeholder")
                paths.append(str(path))
            result_events = []
            progress_events = []

            class FakeEngine:
                last_run_metadata = {"final_engine": "rapidocr"}

                def detect_lines(self, image, page=None):
                    if page == 2:
                        raise RuntimeError("synthetic")
                    return []

            with patch.object(ocr_parallel, "OCREngine", return_value=FakeEngine()), patch.object(
                ocr_parallel.cv2, "imread", side_effect=[np.zeros((2, 2, 3), dtype=np.uint8)] * 2
            ):
                results = ocr_parallel._detect_sequential(
                    [{"index": 1, "image_path": paths[0]}, {"index": 2, "image_path": paths[1]}],
                    "en",
                    result_callback=lambda item: result_events.append(item["index"]),
                    progress_callback=lambda item, phase: progress_events.append((item["index"], phase)),
                )
            self.assertEqual(sorted(results), [1, 2])
            self.assertEqual(result_events, [1, 2])
            self.assertEqual(progress_events, [(1, "started"), (1, "completed"), (2, "started"), (2, "completed")])

    def test_page_progress_stays_ocr_not_rendering(self):
        snapshot = parse_progress_line("OCR: pagina 1/68 - engine=rapidocr - iniciando", ProgressSnapshot())
        snapshot = parse_progress_line("Pagina 1/68: concluida", snapshot)
        self.assertEqual(snapshot.stage, "OCR")

    def test_fast_mode_disables_unbounded_native_fallback(self):
        with patch.object(config, "FAST_OCR_HEAVY_FALLBACK", False):
            run_webtoon._configure_mode("fast")
            self.assertFalse(config.OCR_HYBRID_FALLBACK)
            self.assertFalse(config.RAPIDOCR_PAGE_FALLBACK)
            self.assertFalse(config.OCR_REGION_SELECTIVE_FALLBACK)

    def test_quality_mode_restores_its_own_fallback_policy(self):
        run_webtoon._configure_mode("quality")
        self.assertTrue(config.OCR_HYBRID_FALLBACK)
        self.assertTrue(config.RAPIDOCR_PAGE_FALLBACK)
        self.assertTrue(config.OCR_REGION_SELECTIVE_FALLBACK)


if __name__ == "__main__":
    unittest.main()
