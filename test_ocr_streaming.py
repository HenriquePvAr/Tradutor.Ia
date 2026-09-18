import threading
import time
import unittest
from unittest.mock import patch

import ocr_streaming


class _FakeEngine:
    def __init__(self, *_args, **_kwargs):
        pass


def _fake_detect(job, _engine, **_kwargs):
    time.sleep(float(job.get("delay", 0)))
    return {"index": job["index"], "error": None, "lines": [], "ocr_metadata": {}}


class OCRStreamingTests(unittest.TestCase):
    def test_iterable_is_consumed_incrementally(self):
        yielded = []

        def jobs():
            for index in range(4):
                yielded.append(index)
                yield {"index": index, "image_path": f"p{index}.png"}

        with patch.object(ocr_streaming, "detect_ocr_job", side_effect=_fake_detect):
            result, metrics = ocr_streaming.run_ocr_stream(
                jobs(), "en", capacity=1, engine_factory=_FakeEngine,
            )
        self.assertEqual(list(result), [0, 1, 2, 3])
        self.assertEqual(metrics["jobs_produced"], 4)
        self.assertEqual(metrics["jobs_completed"], 4)
        self.assertEqual(yielded, [0, 1, 2, 3])

    def test_queue_is_bounded_and_producer_blocks(self):
        jobs = ({"index": i, "image_path": f"p{i}.png", "delay": .01} for i in range(8))
        with patch.object(ocr_streaming, "detect_ocr_job", side_effect=_fake_detect):
            result, metrics = ocr_streaming.run_ocr_stream(
                jobs, "en", capacity=1, engine_factory=_FakeEngine,
            )
        self.assertEqual(len(result), 8)
        self.assertLessEqual(metrics["queue_depth_max"], 1)
        self.assertGreater(metrics["producer_blocked_ms"], 0.0)

    def test_cancelled_stream_exits(self):
        cancel = threading.Event()
        jobs = ({"index": i, "image_path": f"p{i}.png", "delay": .02} for i in range(20))

        def detect(job, engine):
            cancel.set()
            return _fake_detect(job, engine)

        with patch.object(ocr_streaming, "detect_ocr_job", side_effect=detect):
            with self.assertRaises(RuntimeError):
                ocr_streaming.run_ocr_stream(
                    jobs, "en", capacity=1, cancel_event=cancel, engine_factory=_FakeEngine,
                )

    def test_consumer_starts_before_iterable_exhaustion(self):
        yielded = []
        started = []

        def jobs():
            for index in range(3):
                yielded.append(index)
                yield {"index": index, "image_path": f"p{index}.png"}
                if index == 0:
                    time.sleep(.05)

        def detect(job, engine, **kwargs):
            started.append(job["index"])
            return _fake_detect(job, engine, **kwargs)

        with patch.object(ocr_streaming, "detect_ocr_job", side_effect=detect):
            ocr_streaming.run_ocr_stream(jobs(), "en", capacity=1, engine_factory=_FakeEngine)
        self.assertEqual(started[0], 0)
        self.assertLess(started[0], max(yielded))


if __name__ == "__main__":
    unittest.main()
