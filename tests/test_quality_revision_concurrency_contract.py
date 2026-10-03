import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import chapter_quality_revision as cqr


class DeterministicReviewer:
    def __init__(self, delays=None, failures=None):
        self.model = "fake-reviewer"
        self.base_url = "local://fake"
        self.delays = delays or {}
        self.failures = failures or {}
        self.calls = []
        self._lock = threading.Lock()

    def review_batch(self, records, glossary, **kwargs):
        record = records[0]
        rid = str(record["region_id"])
        with self._lock:
            attempt = sum(1 for item in self.calls if item[0] == rid) + 1
            self.calls.append((rid, attempt, None))
        time.sleep(float(self.delays.get(rid, 0)))
        if attempt <= int(self.failures.get(rid, 0)):
            raise RuntimeError("temporary_failure")
        with self._lock:
            for index in range(len(self.calls) - 1, -1, -1):
                if self.calls[index][0] == rid and self.calls[index][1] == attempt:
                    self.calls[index] = (rid, attempt, time.perf_counter())
                    break
        return [{"region_id": rid, "action": "rewrite", "risk": "low",
                 "confidence": 1.0, "revised_translation": f"out-{rid}",
                 "marker": rid}]


def records(count):
    return [{"region_id": f"r{i}", "page_id": "p1", "source_text": f"src {i}",
             "current_translation": f"atual {i}", "text_type": "speech"}
            for i in range(count)]


class QualityRevisionConcurrencyContract(unittest.TestCase):
    def _run(self, recs, reviewer, budget=None, cancel=None, workers=4):
        with TemporaryDirectory() as td:
            root = Path(td)
            revision = object.__new__(cqr.ChapterQualityRevision)
            revision.should_cancel = cancel or (lambda: False)
            revision.reviewer_factory = lambda: reviewer
            paths = SimpleNamespace(raw_responses=root)
            old = cqr.config.QUALITY_REVISION_CONCURRENCY
            cqr.config.QUALITY_REVISION_CONCURRENCY = workers
            try:
                return revision._review_concurrent_batches(
                    recs, reviewer, {}, paths, {"revision_id": "contract"}, None,
                    request_budget=budget)
            finally:
                cqr.config.QUALITY_REVISION_CONCURRENCY = old

    def test_completion_order_does_not_change_logical_order_or_mapping(self):
        reviewer = DeterministicReviewer({"r0": .30, "r1": .04, "r2": .16, "r3": .08})
        out = self._run(records(4), reviewer)
        self.assertEqual([x["region_id"] for x in out], ["r0", "r1", "r2", "r3"])
        self.assertEqual([x["revised_translation"] for x in out], ["out-r0", "out-r1", "out-r2", "out-r3"])
        physical = [x[0] for x in sorted(reviewer.calls, key=lambda x: x[2] or 0)]
        self.assertNotEqual(physical, ["r0", "r1", "r2", "r3"])

    def test_global_budget_is_bounded(self):
        for _ in range(20):
            reviewer = DeterministicReviewer({f"r{i}": .005 for i in range(6)})
            self._run(records(6), reviewer, budget=2)
            self.assertEqual(len(reviewer.calls), 2)

    def test_cancel_stops_new_submissions(self):
        started = threading.Event()
        state = {"cancel": False}

        class CancelReviewer(DeterministicReviewer):
            def review_batch(self, records, glossary, **kwargs):
                started.set()
                time.sleep(.08)
                return super().review_batch(records, glossary, **kwargs)

        reviewer = CancelReviewer()
        def cancelled():
            return state["cancel"]
        def flip():
            started.wait(1)
            state["cancel"] = True
        thread = threading.Thread(target=flip)
        thread.start()
        self._run(records(8), reviewer, workers=2, cancel=cancelled)
        thread.join(1)
        self.assertLessEqual(len(reviewer.calls), 2)

    def test_cache_hits_do_not_consume_provider_budget(self):
        with TemporaryDirectory() as td:
            cache = cqr.RevisionResponseCache(Path(td) / "cache.jsonl")
            reviewer = DeterministicReviewer()
            recs = records(4)
            first = self._run_with_cache(recs, reviewer, cache, budget=4)
            calls_after_first = len(reviewer.calls)
            second = self._run_with_cache(recs, reviewer, cache, budget=2)
            self.assertEqual(calls_after_first, 4)
            self.assertEqual(len(reviewer.calls), calls_after_first)
            self.assertEqual([x["region_id"] for x in first], [x["region_id"] for x in second])
            self.assertEqual(cache.hits, 4)

    def _run_with_cache(self, recs, reviewer, cache, budget):
        with TemporaryDirectory() as td:
            revision = object.__new__(cqr.ChapterQualityRevision)
            revision.should_cancel = lambda: False
            paths = SimpleNamespace(raw_responses=Path(td))
            old = cqr.config.QUALITY_REVISION_CONCURRENCY
            cqr.config.QUALITY_REVISION_CONCURRENCY = 4
            try:
                return revision._review_concurrent_batches(
                    recs, reviewer, {}, paths, {"revision_id": "cache"}, cache,
                    request_budget=budget)
            finally:
                cqr.config.QUALITY_REVISION_CONCURRENCY = old

    def test_failure_isolation_and_retry_not_duplicated(self):
        reviewer = DeterministicReviewer(failures={"r1": 1})
        out = self._run(records(3), reviewer)
        self.assertEqual([x["region_id"] for x in out], ["r0", "r1", "r2"])
        self.assertEqual([sum(1 for c in reviewer.calls if c[0] == f"r{i}") for i in range(3)], [1, 1, 1])
        self.assertEqual(out[1]["action"], "manual_review")

    def test_retry_is_same_logical_item_and_does_not_claim_twice(self):
        class RetryReviewer(DeterministicReviewer):
            def review_batch(self, records, glossary, **kwargs):
                rid = str(records[0]["region_id"])
                with self._lock:
                    self.calls.append((rid, 1, time.perf_counter()))
                    self.calls.append((rid, 2, time.perf_counter()))
                return [{"region_id": rid, "action": "rewrite", "risk": "low",
                         "confidence": 1.0, "revised_translation": f"retry-{rid}"}]

        reviewer = RetryReviewer()
        out = self._run(records(2), reviewer, budget=2)
        self.assertEqual(len(out), 2)
        self.assertEqual(len(reviewer.calls), 4)  # two attempts per logical item

    def test_cancel_before_internal_retry_starts_no_new_attempt(self):
        state = {"cancel": False}
        class CancelRetryReviewer(DeterministicReviewer):
            def review_batch(self, records, glossary, **kwargs):
                rid = str(records[0]["region_id"])
                self.calls.append((rid, 1, time.perf_counter()))
                state["cancel"] = True
                if state["cancel"]:
                    return [{"region_id": rid, "action": "manual_review",
                             "risk": "high", "reason_code": "cancelled_before_retry"}]
                self.calls.append((rid, 2, time.perf_counter()))
                return []
        reviewer = CancelRetryReviewer()
        self._run(records(1), reviewer, budget=2, cancel=lambda: state["cancel"])
        self.assertEqual(len(reviewer.calls), 1)


if __name__ == "__main__":
    unittest.main()
