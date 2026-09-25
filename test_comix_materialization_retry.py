"""Bounded retry for the intermittently-flaky canonical materialization of Comix.

Physically proven flakiness (scrapling_reader.jsonl history): the SAME resolver / URL /
config produced ``dynamic_resolution_succeeded pages:105`` 40x and
``canonical_materialization_failed`` 31x, interleaved.  The failure happens when >=1
discovered logical page stays an unmaterialized ``scrapling_dom`` placeholder.

``resolve()`` now retries that ONE transient mode up to ``MAX_RESOLUTION_ATTEMPTS`` full
passes.  Every non-transient outcome (no_reader_images, timeout, browser/capability/
navigation errors) never retries, and fail-closed is preserved: an incomplete chapter is
never returned.
"""
import os
import tempfile
import unittest
from unittest import mock

import scrapling_reader_resolver as resolver
from scrapling_reader_resolver import DynamicReaderError

URL = "https://comix.to/title/qk31-example/11378175-chapter-70"


class _FakeAdapter:
    name = "comix"
    adapter_version = "1"
    is_specific = True

    def validate_navigation_url(self, _url):
        return None

    def validate_path(self, _url):
        return None

    def score_cluster(self, _cluster):
        return None


def _inline_boundary(adapter):
    """A ``_resolve_in_child_process`` stand-in that runs the resolver INLINE.

    ``resolve()`` now spawns a child process for isolation (see the child-process
    boundary tests in ``test_scrapling_reader_resolver.py``).  These tests exercise the
    INLINE retry/materialization contract, so they replace only that boundary with an
    in-process call to ``_resolve_inline`` -- the parent-process mocks then apply and no
    real Chrome/subprocess is launched.  Production is unchanged.
    """
    def boundary(url, *, cancel_check=None, timeout=30.0):
        return resolver._resolve_inline(
            url, adapter=adapter, cancel_check=cancel_check, timeout=timeout)
    return boundary


def _run_resolve_with(failure_sequence):
    """Drive resolve() with a scripted per-attempt classification.

    Patches the browser/fetch/analysis boundary so no real Chrome runs; the retry LOOP
    and its fail-closed decisions are what is under test.  Returns (result_or_exc,
    fetch_call_count, telemetry_calls).
    """
    fake_fetcher = mock.MagicMock()
    telemetry = []
    adapter = _FakeAdapter()

    def record(event, **fields):
        telemetry.append({"event": event, **fields})

    with mock.patch.object(resolver, "supports_url", return_value=True), \
            mock.patch.object(resolver, "discover_system_browser", return_value=("chrome", "/fake/chrome")), \
            mock.patch.object(resolver, "_DynamicFetcher", fake_fetcher), \
            mock.patch.object(resolver, "_resolution_failure_code", side_effect=list(failure_sequence)), \
            mock.patch.object(resolver, "_append_telemetry", side_effect=record), \
            mock.patch.object(resolver, "_resolve_in_child_process", side_effect=_inline_boundary(adapter)), \
            mock.patch("universal_chapter_adapter.analyse_candidates", return_value=mock.MagicMock()):
        try:
            result = resolver.resolve(URL, adapter=adapter)
            return result, fake_fetcher.fetch.call_count, telemetry
        except DynamicReaderError as exc:
            return exc, fake_fetcher.fetch.call_count, telemetry


class MaterializationRetryTests(unittest.TestCase):
    # --- constants (mission items 1 & 2) ---
    def test_constants(self):
        self.assertEqual(resolver.MAX_RESOLUTION_ATTEMPTS, 3)
        self.assertEqual(resolver.MISSING_PAGE_BUDGET_SECONDS, 25.0)

    # --- E: first pass succeeds -> exactly one attempt ---
    def test_first_pass_success_single_attempt(self):
        result, attempts, _ = _run_resolve_with([None])
        self.assertNotIsInstance(result, DynamicReaderError)
        self.assertEqual(attempts, 1)

    # --- A: attempt 1 leaves a scrapling_dom, attempt 2 completes -> PASS ---
    def test_second_pass_recovers(self):
        result, attempts, telemetry = _run_resolve_with(
            ["canonical_materialization_failed", None])
        self.assertNotIsInstance(result, DynamicReaderError)  # recovered, no terminal failure
        self.assertEqual(attempts, 2)
        retries = [t for t in telemetry if t["event"] == "dynamic_resolution_retry"]
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0]["attempt"], 1)
        self.assertEqual(retries[0]["max_attempts"], 3)
        self.assertIn("pending_count", retries[0])
        self.assertIn("pending_indices", retries[0])

    # --- B: attempts 1 & 2 fail, attempt 3 completes -> PASS with 3 passes ---
    def test_third_pass_recovers(self):
        result, attempts, telemetry = _run_resolve_with(
            ["canonical_materialization_failed", "canonical_materialization_failed", None])
        self.assertNotIsInstance(result, DynamicReaderError)
        self.assertEqual(attempts, 3)
        self.assertEqual(
            len([t for t in telemetry if t["event"] == "dynamic_resolution_retry"]), 2)

    # --- C: all three passes fail -> fail-closed, never an incomplete chapter ---
    def test_exhaustion_is_fail_closed(self):
        result, attempts, telemetry = _run_resolve_with(
            ["canonical_materialization_failed"] * 3)
        self.assertIsInstance(result, DynamicReaderError)
        self.assertEqual(result.code, "canonical_materialization_failed")
        self.assertEqual(attempts, 3)  # bounded: never a 4th pass / infinite loop
        terminal = [t for t in telemetry if t["event"] == "dynamic_resolution_failed"
                    and t.get("error_code") == "canonical_materialization_failed"]
        self.assertTrue(terminal and terminal[-1]["attempt"] == 3)

    # --- non-transient modes never retry ---
    def test_timeout_mode_does_not_retry(self):
        result, attempts, _ = _run_resolve_with(["canonical_materialization_timeout"])
        self.assertIsInstance(result, DynamicReaderError)
        self.assertEqual(result.code, "canonical_materialization_timeout")
        self.assertEqual(attempts, 1)  # a missing logical page is not the flaky mode

    def test_no_reader_images_does_not_retry(self):
        result, attempts, _ = _run_resolve_with(["no_reader_images"])
        self.assertIsInstance(result, DynamicReaderError)
        self.assertEqual(result.code, "no_reader_images")
        self.assertEqual(attempts, 1)

    # --- D: capability/browser problems never spin a useless retry ---
    def test_capability_unavailable_does_not_retry(self):
        adapter = _FakeAdapter()
        with mock.patch.object(resolver, "supports_url", return_value=True), \
                mock.patch.object(resolver, "_DynamicFetcher", None), \
                mock.patch.object(resolver, "_resolve_in_child_process",
                                  side_effect=_inline_boundary(adapter)):
            with self.assertRaises(DynamicReaderError) as ctx:
                resolver.resolve(URL, adapter=adapter)
        self.assertEqual(ctx.exception.code, "capability_unavailable")

    def test_browser_error_propagates_without_retry(self):
        adapter = _FakeAdapter()
        fake_fetcher = mock.MagicMock()
        fake_fetcher.fetch.side_effect = DynamicReaderError("browser_unavailable")
        with mock.patch.object(resolver, "supports_url", return_value=True), \
                mock.patch.object(resolver, "discover_system_browser", return_value=("chrome", "/fake")), \
                mock.patch.object(resolver, "_DynamicFetcher", fake_fetcher), \
                mock.patch.object(resolver, "_append_telemetry"), \
                mock.patch.object(resolver, "_resolve_in_child_process",
                                  side_effect=_inline_boundary(adapter)):
            with self.assertRaises(DynamicReaderError) as ctx:
                resolver.resolve(URL, adapter=adapter)
        self.assertEqual(ctx.exception.code, "browser_unavailable")
        self.assertEqual(fake_fetcher.fetch.call_count, 1)  # raised, not retried


class FailureClassificationTests(unittest.TestCase):
    """The real trigger: a leftover scrapling_dom placeholder is the retryable mode."""

    def test_scrapling_dom_leftover_is_canonical_materialization_failed(self):
        candidates = [
            {"logical_page_index": 1, "source": "browser_response_body"},
            {"logical_page_index": 2, "source": "scrapling_dom"},  # unmaterialized
            {"logical_page_index": 3, "source": "canvas_capture"},
        ]
        self.assertEqual(
            resolver._resolution_failure_code(candidates, []),
            "canonical_materialization_failed")

    def test_all_materialized_is_success(self):
        candidates = [
            {"logical_page_index": 1, "source": "browser_response_body"},
            {"logical_page_index": 2, "source": "canvas_capture"},
        ]
        self.assertIsNone(resolver._resolution_failure_code(candidates, []))

    def test_missing_index_is_timeout_not_failed(self):
        candidates = [{"logical_page_index": 1, "source": "browser_response_body"}]
        self.assertEqual(
            resolver._resolution_failure_code(candidates, [2]),
            "canonical_materialization_timeout")

    def test_empty_is_no_reader_images(self):
        self.assertEqual(resolver._resolution_failure_code([], []), "no_reader_images")


class TelemetryAllowListTests(unittest.TestCase):
    """pending_count / pending_indices / attempt must survive the sanitization allow-list."""

    def test_diagnostic_fields_are_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}):
                resolver._append_telemetry(
                    "dynamic_resolution_retry", status="retry",
                    error_code="canonical_materialization_failed",
                    attempt=1, max_attempts=3, pending_count=2, pending_indices=[7, 42],
                    elapsed_ms=1234.5,
                    # a sensitive field must still be dropped by the allow-list
                    cookie="SECRET-should-not-persist",
                )
            written = (open(os.path.join(tmp, "YomuSekai", "diagnostics",
                                         "scrapling_reader.jsonl"), encoding="utf-8")
                       .read())
        self.assertIn('"pending_count": 2', written)
        self.assertIn('"pending_indices": [7, 42]', written)
        self.assertIn('"attempt": 1', written)
        self.assertIn('"max_attempts": 3', written)
        self.assertNotIn("SECRET-should-not-persist", written)  # allow-list still filters


if __name__ == "__main__":
    unittest.main()
