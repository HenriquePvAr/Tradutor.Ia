"""Focused regression tests for hybrid OCR provenance and cancellation.

All tests are hermetic: OCR and NVIDIA transport are replaced with fakes and
no real network, job, or translation path is exercised.
"""
import _test_bootstrap  # noqa: F401

import threading
import time
import unittest
from unittest import mock

import numpy as np

from ocr_contract import OCRPageResult
from ocr_engine import OCRLine, mark_ocr_line_recovered, stamp_initial_ocr_provenance


def _image(_path):
    return np.zeros((8, 8, 3), dtype=np.uint8)


class OcrProvenanceTests(unittest.TestCase):
    def _line(self, engine):
        return OCRLine(
            text="hello", confidence=0.9,
            polygon=np.array([[0, 0], [4, 0], [4, 4], [0, 4]], dtype=np.float32),
            box=(0, 0, 4, 4), raw_text="hello", engine=engine,
        )

    def test_initial_engine_is_preserved_after_recovery(self):
        line = self._line("nvidia")
        stamp_initial_ocr_provenance([line])
        mark_ocr_line_recovered(line, "rapidocr")
        self.assertEqual(line.metadata["initial_ocr_engine"], "nvidia")
        self.assertEqual(line.metadata["current_ocr_engine"], "rapidocr")
        self.assertEqual(line.metadata["recovery_engine"], "rapidocr")
        self.assertTrue(line.metadata["recovered"])

    def test_rapidocr_initial_provenance(self):
        line = self._line("rapidocr")
        stamp_initial_ocr_provenance([line])
        self.assertEqual(line.metadata["initial_ocr_engine"], "rapidocr")
        self.assertFalse(line.metadata["recovered"])

    def test_group_marks_mixed_initial_engines(self):
        import ocr_balloon

        first, second = self._line("nvidia"), self._line("rapidocr")
        stamp_initial_ocr_provenance([first, second])
        group = ocr_balloon.TextGroup(group_id="g1", lines=[first, second])
        ocr_balloon._assign_region_metadata([group])
        self.assertEqual(group.ocr_provenance_engines, ("nvidia", "rapidocr"))
        self.assertTrue(group.mixed_ocr_provenance)


class HybridCancellationTests(unittest.TestCase):
    def test_cancel_stops_new_assignments_and_queue_join_returns(self):
        import ocr_hybrid_scheduler as sched

        cancel = threading.Event()
        calls = []

        def detect(_engine, _image, page=None):
            calls.append(page)
            cancel.set()
            return []

        jobs = [{"index": i, "image_path": f"p{i}.png"} for i in range(8)]
        class NoProvider:
            def is_configured(self):
                return False

        with mock.patch("cv2.imread", side_effect=_image), \
             mock.patch.object(sched.OCREngine, "detect_lines", detect):
            results, telemetry = sched.run_hybrid(
                jobs, "en", NoProvider(), rapidocr_workers=1, nvidia_workers=1,
                cancel_event=cancel,
            )
        self.assertLessEqual(len(calls), 1)
        self.assertEqual(results, {})
        self.assertEqual(telemetry["RAPIDOCR_PAGES"], 0)

    def test_cancel_during_nvidia_in_flight_does_not_start_another_page(self):
        import ocr_hybrid_scheduler as sched

        cancel = threading.Event()
        started = threading.Event()
        release = threading.Event()
        calls = []

        class Provider:
            def is_configured(self):
                return True

            def recognize_page(self, _image_value, context=None):
                calls.append(context["page_id"])
                started.set()
                release.wait(2)
                return OCRPageResult(page_id=context["page_id"], engine="nvidia", width=8, height=8)

        jobs = [{"index": i, "image_path": f"p{i}.png"} for i in range(4)]
        holder = {}

        def run():
            with mock.patch("cv2.imread", side_effect=_image):
                holder["value"] = sched.run_hybrid(
                    jobs, "en", Provider(), rapidocr_workers=1, nvidia_workers=1,
                    cancel_event=cancel,
                )

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(started.wait(1))
        cancel.set()
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(calls), 1)
        self.assertEqual(holder["value"][0], {})


if __name__ == "__main__":
    unittest.main()
