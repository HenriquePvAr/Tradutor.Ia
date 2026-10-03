import unittest

import ocr_line_provenance

from page_processing_contracts import (
    PageProgressAggregator,
    PageProgressEvent,
    PageProcessingContext,
    PreTranslationPageAccumulator,
    PostTranslationPageResult,
    PreTranslationPageResult,
    merge_page_counters,
    merge_page_diagnostics,
)


class PageProcessingContractTests(unittest.TestCase):
    def test_progress_events_are_page_local_until_aggregation(self):
        acc = PreTranslationPageAccumulator(page_index=2)
        acc.progress_events.append(PageProgressEvent(2, "ocr", 1, 2, code="half"))
        result = acc.to_result()
        self.assertEqual(result.progress_events[0].page_index, 2)
        self.assertEqual(result.progress_events[0].fraction(), 0.5)

    def test_progress_main_aggregator_emits_only_on_consume(self):
        writes = []
        aggregator = PageProgressAggregator(2, emit=writes.append)
        event = PageProgressEvent(1, "pre_translation", 1, 1, code="done")
        self.assertEqual(aggregator.global_writes, 0)
        aggregator.consume((event,))
        self.assertEqual(aggregator.global_writes, 1)
        self.assertEqual(writes[0]["completed_pages"], 1)

    def test_progress_out_of_order_pages_is_monotonic(self):
        values = []
        aggregator = PageProgressAggregator(5, emit=lambda payload: values.append(payload["progress"]))
        for page in (2, 4, 1, 3, 5):
            aggregator.consume((PageProgressEvent(page, "render", 1, 1),))
        self.assertEqual(values, sorted(values))
        self.assertEqual(values[-1], 1.0)

    def test_progress_cancel_drops_late_events(self):
        writes = []
        aggregator = PageProgressAggregator(1, emit=writes.append)
        event = PageProgressEvent(1, "render", 1, 1)
        self.assertEqual(aggregator.consume((event,), cancelled=True), [])
        self.assertEqual(writes, [])
        aggregator.consume((event,), terminal=True)
        self.assertEqual(aggregator.consume((event,)), [])

    def test_progress_error_path_is_atomic(self):
        writes = []
        aggregator = PageProgressAggregator(2, emit=writes.append)
        aggregator.consume((PageProgressEvent(1, "pre_translation", 1, 1, code="error"),))
        self.assertEqual(writes[0]["completed_pages"], 1)
        self.assertEqual(aggregator.last_progress, 0.5)

    def test_two_page_state_isolation_before_merge(self):
        page1 = {"index": 1, "timings": {"ocr": 1.0}, "precheck": {"metrics": {"x": 1}}}
        page2 = {"index": 2, "timings": {"ocr": 2.0}, "precheck": {"metrics": {"x": 2}}}
        local1 = dict(page1)
        local1["timings"] = dict(page1["timings"])
        local1["precheck"] = {"metrics": dict(page1["precheck"]["metrics"])}
        local1["timings"]["classification"] = 3.0
        local1["precheck"]["metrics"]["x"] = 9
        self.assertNotIn("classification", page2["timings"])
        self.assertEqual(page2["precheck"]["metrics"]["x"], 2)

    def test_job_aggregate_unchanged_before_merge(self):
        aggregate = {"pages_done": 0}
        acc = PreTranslationPageAccumulator(page_index=1)
        acc.progress_events.append(PageProgressEvent(1, "render", 1, 1))
        self.assertEqual(aggregate, {"pages_done": 0})
        emitted = []
        PageProgressAggregator(1, emit=emitted.append).consume(tuple(acc.to_result().progress_events))
        self.assertEqual(aggregate, {"pages_done": 0})

    def test_progress_serial_sequence_preserved(self):
        events = []
        aggregator = PageProgressAggregator(2, emit=lambda payload: events.append(payload["page_indexes"]))
        aggregator.consume((PageProgressEvent(1, "render", 1, 1),))
        aggregator.consume((PageProgressEvent(2, "render", 1, 1),))
        self.assertEqual(events, [[1], [1, 2]])

    def test_pre_translation_has_no_cross_page_progress_mutation(self):
        acc1 = PreTranslationPageAccumulator(1)
        acc2 = PreTranslationPageAccumulator(2)
        acc1.progress_events.append(PageProgressEvent(1, "classification", 1, 1))
        self.assertEqual(acc2.progress_events, [])

    def test_pre_translation_thread_readiness_contract(self):
        acc = PreTranslationPageAccumulator(1)
        acc.progress_events.append(PageProgressEvent(1, "pre_translation", 1, 1))
        result = acc.to_result()
        self.assertEqual(result.page_index, 1)
        self.assertEqual(result.progress_events[0].code, "")
    def test_accumulator_freezes_page_result_without_job_state(self):
        accumulator = PreTranslationPageAccumulator(page_index=3)
        accumulator.groups.append("group-3")
        accumulator.translation_items.append("item-3")
        accumulator.counters["groups"] = 1
        accumulator.timings["grouping"] = 0.25
        accumulator.diagnostics.append({"page": 3, "event": "grouped"})
        result = accumulator.to_result()
        accumulator.groups.append("late-mutation")
        accumulator.counters["groups"] = 99
        self.assertEqual(result.groups, ("group-3",))
        self.assertEqual(result.translation_items, ("item-3",))
        self.assertEqual(result.counters, {"groups": 1})
        self.assertEqual(result.timings, {"grouping": 0.25})

    def test_page_error_counter_merges_once(self):
        accumulator = PreTranslationPageAccumulator(page_index=4)
        accumulator.counters["pages_with_error"] = 1
        result = accumulator.to_result()
        aggregate = {"pages_with_error": 0}
        merge_page_counters(aggregate, result)
        self.assertEqual(aggregate["pages_with_error"], 1)

    def test_page_scoped_provenance_merges_once_in_order(self):
        recorder = ocr_line_provenance.ProvenanceRecorder()
        with recorder.page(2):
            recorder._current["events"].append({"operation": "ocr_retry_replacement", "page": 2})
        merged = ocr_line_provenance.ProvenanceRecorder()
        merged.merge_from(recorder)
        self.assertEqual(merged.to_dict()["pages"][0]["page"], 2)
        self.assertEqual(len(merged.to_dict()["pages"][0]["events"]), 1)

    def test_results_are_page_local_and_merge_is_explicit(self):
        context = PageProcessingContext(2, "page-002.png", config_snapshot={"force": True})
        result = PreTranslationPageResult(
            page_index=context.page_index,
            translation_items=("item-2",),
            diagnostics=({"page": 2, "event": "grouped"},),
            counters={"groups": 1},
        )
        counters = {"groups": 3}
        diagnostics = []
        merge_page_counters(counters, result)
        merge_page_diagnostics(diagnostics, result)
        self.assertEqual(counters, {"groups": 4})
        self.assertEqual(diagnostics, [{"page": 2, "event": "grouped"}])
        self.assertEqual(result.page_index, 2)

    def test_post_result_does_not_require_job_state(self):
        result = PostTranslationPageResult(
            page_index=1,
            output_path="page_001.png",
            quality={"passed": True},
            timings={"render": 0.2},
        )
        self.assertEqual(result.output_path, "page_001.png")
        self.assertTrue(result.quality["passed"])

    def test_merge_order_is_caller_controlled(self):
        results = [
            PreTranslationPageResult(2, diagnostics=({"page": 2},)),
            PreTranslationPageResult(1, diagnostics=({"page": 1},)),
        ]
        merged = []
        for result in sorted(results, key=lambda item: item.page_index):
            merge_page_diagnostics(merged, result)
        self.assertEqual(merged, [{"page": 1}, {"page": 2}])


if __name__ == "__main__":
    unittest.main()
