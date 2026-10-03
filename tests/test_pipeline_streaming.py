import threading
import time
import unittest

import config
from pipeline_streaming import StreamPage, StreamingStageError, run_streaming


def pages(count=6):
    return [StreamPage(i, f"page-{i}", f"source-{i}", f"input-{i}") for i in range(1, count + 1)]


class PipelineStreamingTests(unittest.TestCase):
    def test_flag_defaults_off(self):
        self.assertFalse(config.PIPELINE_STREAMING)

    def test_page_ready_handoff_and_order(self):
        events = []
        result, metrics = run_streaming(
            pages(), preprocess=lambda p: p.input_data.upper(),
            ocr=lambda p, value: value + ":ocr",
            render=lambda p, pre, text: {"index": p.page_index, "text": text},
            queue_capacity=2, event_callback=lambda name, data: events.append((name, data)),
        )
        self.assertEqual([item["page_index"] for item in result], list(range(1, 7)))
        self.assertEqual(metrics.pages_completed, 6)
        self.assertEqual(sum(name == "PAGE_INPUT_READY" for name, _ in events), 6)
        self.assertIsNotNone(metrics.first_render_ms)

    def test_bounded_queue_and_backpressure(self):
        result, metrics = run_streaming(
            pages(8), preprocess=lambda p: (time.sleep(.01), p.input_data)[1],
            ocr=lambda p, value: (time.sleep(.01), value)[1],
            render=lambda p, pre, value: value, queue_capacity=1,
        )
        self.assertEqual(len(result), 8)
        self.assertLessEqual(metrics.queue_depth_max, 1)
        self.assertGreaterEqual(metrics.producer_blocked_ms, 0.0)

    def test_out_of_order_stage_completion_reorders_output(self):
        def preprocess(page):
            time.sleep((7 - page.page_index) * .002)
            return page.page_index
        result, _ = run_streaming(
            pages(), preprocess=preprocess, ocr=lambda p, value: value,
            render=lambda p, pre, value: value, preprocess_workers=2,
        )
        self.assertEqual([entry["output"] for entry in result], list(range(1, 7)))

    def test_identity_is_preserved(self):
        result, _ = run_streaming(
            pages(), preprocess=lambda p: p.page_id,
            ocr=lambda p, value: (p.source_identity, value),
            render=lambda p, pre, value: value,
        )
        for entry in result:
            index = entry["page_index"]
            self.assertEqual(entry["page_id"], f"page-{index}")
            self.assertEqual(entry["source_identity"], f"source-{index}")

    def test_slow_first_page_does_not_change_final_order(self):
        def preprocess(page):
            if page.page_index == 1: time.sleep(.03)
            return page.page_index
        result, _ = run_streaming(pages(5), preprocess=preprocess,
                                  ocr=lambda p, value: value,
                                  render=lambda p, pre, value: value,
                                  preprocess_workers=2)
        self.assertEqual([item["output"] for item in result], [1, 2, 3, 4, 5])

    def test_producer_failure_is_attributed(self):
        def preprocess(page):
            if page.page_index == 3: raise ValueError("fixture")
            return page.page_index
        with self.assertRaises(StreamingStageError) as caught:
            run_streaming(pages(), preprocess=preprocess, ocr=lambda p, v: v,
                          render=lambda p, pre, v: v)
        self.assertEqual(caught.exception.stage, "preprocess")
        self.assertEqual(caught.exception.page_index, 3)

    def test_consumer_failure_is_attributed(self):
        def ocr(page, value):
            if page.page_index == 2: raise RuntimeError("fixture")
            return value
        with self.assertRaises(StreamingStageError) as caught:
            run_streaming(pages(), preprocess=lambda p: p.input_data, ocr=ocr,
                          render=lambda p, pre, v: v)
        self.assertEqual(caught.exception.stage, "ocr")
        self.assertEqual(caught.exception.page_index, 2)

    def test_cancellation_stops_without_hang(self):
        cancel = threading.Event()
        def preprocess(page):
            if page.page_index == 1: cancel.set()
            time.sleep(.01)
            return page.page_index
        started = time.perf_counter()
        result, _ = run_streaming(pages(20), preprocess=preprocess,
                                  ocr=lambda p, v: v, render=lambda p, pre, v: v,
                                  cancel_event=cancel)
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertLess(len(result), 20)

    def test_telemetry_correlation_has_page_ids(self):
        events = []
        run_streaming(pages(3), preprocess=lambda p: p.input_data,
                      ocr=lambda p, v: v, render=lambda p, pre, v: v,
                      event_callback=lambda name, data: events.append((name, data)))
        self.assertTrue(events)
        self.assertTrue(all("page_index" in data for _, data in events))

    def test_semantic_equivalence_to_legacy_sequence(self):
        legacy = [f"rendered-{p.page_index}" for p in pages()]
        streamed, _ = run_streaming(
            pages(), preprocess=lambda p: p.input_data,
            ocr=lambda p, value: value,
            render=lambda p, pre, value: f"rendered-{p.page_index}",
        )
        self.assertEqual([x["output"] for x in streamed], legacy)

    def test_invalid_capacity_rejected(self):
        with self.assertRaises(ValueError):
            run_streaming(pages(1), preprocess=lambda p: p, ocr=lambda p, v: v,
                          render=lambda p, pre, v: v, queue_capacity=0)

    def test_render_failure_does_not_deadlock(self):
        def render(page, pre, value):
            if page.page_index == 4: raise LookupError("fixture")
            return value
        with self.assertRaises(StreamingStageError) as caught:
            run_streaming(pages(), preprocess=lambda p: p.input_data,
                          ocr=lambda p, v: v, render=render)
        self.assertEqual(caught.exception.stage, "render")


if __name__ == "__main__":
    unittest.main()
