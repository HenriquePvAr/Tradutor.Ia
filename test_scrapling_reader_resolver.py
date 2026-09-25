import unittest
from unittest import mock
from io import BytesIO
import base64
import os
import sys
import tempfile
import time

from PIL import Image

import scrapling_reader_resolver as resolver


class _FakePage:
    def __init__(self):
        self.handlers = {}

    def on(self, name, callback):
        self.handlers[name] = callback

    def wait_for_timeout(self, _ms):
        return None

    def evaluate(self, _script, *_args):
        # The real Playwright page.evaluate is variadic: extract-candidates passes a
        # selector arg, while preload/overlay scripts pass none.  Return the DOM list
        # only for the selector-bearing call; a bare dict makes the preload phase a
        # clean no-op (no "preload all" control present in this fixture).
        if not _args:
            return {}
        return [
            {"url": "https://jdpw.wowpic2.store/a/1.webp", "naturalWidth": 659,
             "naturalHeight": 1100, "width": 659, "height": 1100, "y": 0,
             "alt": "Page 1"},
            {"url": "https://jdpw.wowpic2.store/a/2.webp", "naturalWidth": 667,
             "naturalHeight": 1100, "width": 667, "height": 1100, "y": 1100,
             "alt": "Page 3"},
        ]


class _VirtualizedPage:
    """Three DOM slots reused while the public reader advances through ten pages."""
    def __init__(self):
        self.step = 0
        self.handlers = {}

    def on(self, name, callback):
        self.handlers[name] = callback

    def wait_for_timeout(self, _ms):
        return None

    def evaluate(self, script, _arg):
        if "window.scrollTo" in script:
            self.step = min(self.step + 1, 3)
            return None
        if "slot_count" not in script:
            return [
                {"url": "https://cdn.test/1.webp", "y": 0, "width": 800,
                 "height": 1200, "naturalWidth": 800, "naturalHeight": 1200},
                {"url": "https://cdn.test/2.webp", "y": 100, "width": 800,
                 "height": 1200, "naturalWidth": 800, "naturalHeight": 1200},
                {"url": "https://cdn.test/3.webp", "y": 200, "width": 800,
                 "height": 1200, "naturalWidth": 800, "naturalHeight": 1200},
            ]
        first = self.step * 3 + 1
        count = 1 if self.step == 3 else 3
        return {
            "rows": [
                {"url": f"https://cdn.test/{index}.webp", "y": index * 100,
                 "width": 800, "height": 1200, "naturalWidth": 800,
                 "naturalHeight": 1200}
                for index in range(first, first + count)
            ],
            "slot_count": 3,
            "placeholder_count": 0,
            "scroll_y": self.step * 400,
            "viewport_height": 400,
            "document_height": 1600,
            "reader_bottom": 1600,
            "at_bottom": self.step == 3,
            "lazy_attributes": [],
        }


class _LazyAttributePage(_VirtualizedPage):
    def evaluate(self, script, arg):
        if "window.scrollTo" in script:
            return super().evaluate(script, arg)
        state = super().evaluate(script, arg)
        if isinstance(state, list):
            return state
        state["lazy_attributes"] = ["data-src", "srcset"]
        return state


class _FakeAdapter:
    name = "comix"
    adapter_version = "1"
    is_specific = True

    def validate_navigation_url(self, _url):
        return None

    def validate_path(self, _url):
        return None

    def validate_observed_url(self, _url):
        return None

    def authorize_related_url(self, _url):
        return None

    def score_cluster(self, _cluster):
        return None


class _ViewportCanvasPage:
    def __init__(self):
        self.viewport = {"width": 1280, "height": 900}

    def evaluate(self, script, _arg=None):
        if "main.rpage-main" in script:
            return {
                "reader": {"x": 0, "y": 0, "width": 1280, "height": self.viewport["height"]},
                "target": {"x": 320, "y": 0, "width": 640, "height": 1000},
                "viewport": dict(self.viewport),
                "visual": {"x": 0, "y": 0},
                "dpr": 2,
                "native": {"width": 800, "height": 1250},
            }
        return None

    def set_viewport_size(self, value):
        self.viewport = dict(value)

    def wait_for_timeout(self, _ms):
        return None

    def screenshot(self, *, type):
        image = Image.new("RGBA", (self.viewport["width"] * 2,
                                     self.viewport["height"] * 2), (12, 24, 36, 255))
        encoded = BytesIO()
        image.save(encoded, format="PNG")
        return encoded.getvalue()


class ScraplingResolverTests(unittest.TestCase):
    URL = "https://comix.to/title/k72ge-home/1536897-chapter-1"

    def test_policy_is_selected_only(self):
        self.assertTrue(resolver.supports_url(self.URL))
        self.assertFalse(resolver.supports_url("https://example.test/chapter/1"))

    def test_dom_extraction_preserves_order_and_no_body(self):
        events = []
        rows, selector = resolver._extract_candidates(_FakePage(), self.URL, events, None)
        self.assertEqual(selector, "img.rpage-page__img")
        self.assertEqual([item["order"] for item in rows], [0, 1])
        self.assertEqual([item["logical_page_index"] for item in rows], [1, 3])
        self.assertEqual(events, [])

    def test_missing_capability_is_controlled(self):
        with mock.patch.object(resolver, "_DynamicFetcher", None):
            with self.assertRaisesRegex(resolver.DynamicReaderError, "capability_unavailable"):
                resolver._resolve_inline(self.URL, adapter=_FakeAdapter())

    def test_capability_snapshot_is_sanitized(self):
        snapshot = resolver.capabilities()
        self.assertIsInstance(snapshot.available, bool)
        self.assertNotIn("C:\\", snapshot.import_error)

    def test_opaque_xhr_is_not_decrypted(self):
        fake_fetcher = mock.Mock()

        def fetch(_url, **kwargs):
            page = _FakePage()
            kwargs["page_setup"](page)
            kwargs["page_action"](page)
            return object()

        fake_fetcher.fetch.side_effect = fetch
        with mock.patch.object(resolver, "_DynamicFetcher", fake_fetcher):
            # Opaque XHR is never decrypted, so nothing materializes: the resolver fails
            # closed on the first pass (no-progress) instead of wasting further retries.
            with self.assertRaisesRegex(resolver.DynamicReaderError,
                                        "canonical_materialization_no_progress"):
                resolver._resolve_inline(self.URL, adapter=_FakeAdapter())

    def test_global_deadline_is_passed_as_playwright_operation_timeout(self):
        fake_fetcher = mock.Mock()

        def bounded_fetch(_url, **kwargs):
            # Model a blocking browser operation honoring Playwright's supplied timeout.
            time.sleep(kwargs["timeout"] / 1000 + 0.02)
            raise TimeoutError("simulated browser operation timeout")

        fake_fetcher.fetch.side_effect = bounded_fetch
        with mock.patch.object(resolver, "_DynamicFetcher", fake_fetcher), \
                mock.patch.object(resolver, "discover_system_browser", return_value=("Chrome", "chrome.exe")), \
                mock.patch.object(resolver, "DYNAMIC_RESOLVER_DEADLINE_SECONDS", 0.05):
            started = time.monotonic()
            with self.assertRaisesRegex(resolver.DynamicReaderError,
                                        "dynamic_resolver_deadline_exceeded"):
                resolver._resolve_inline(self.URL, adapter=_FakeAdapter(), timeout=1.0)
            self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(fake_fetcher.fetch.call_count, 1)
        self.assertLessEqual(fake_fetcher.fetch.call_args.kwargs["timeout"], 50)

    def test_expired_global_deadline_prevents_retry(self):
        fake_fetcher = mock.Mock()
        fake_fetcher.fetch.return_value = object()
        with mock.patch.object(resolver, "_DynamicFetcher", fake_fetcher), \
                mock.patch.object(resolver, "discover_system_browser", return_value=("Chrome", "chrome.exe")), \
                mock.patch.object(resolver, "DYNAMIC_RESOLVER_DEADLINE_SECONDS", 0):
            with self.assertRaisesRegex(resolver.DynamicReaderError,
                                        "dynamic_resolver_deadline_exceeded"):
                resolver._resolve_inline(self.URL, adapter=_FakeAdapter())
        fake_fetcher.fetch.assert_not_called()

    def test_full_resolution_retry_is_skipped_without_its_required_budget(self):
        # A first pass leaves pages pending but the remaining budget cannot fund another
        # bounded full pass (with its cleanup margin): fail closed with the budget code,
        # and never start a second pass.
        fake_fetcher = mock.Mock()

        def fetch(_url, **kwargs):
            page = _FakePage()
            kwargs["page_setup"](page)
            kwargs["page_action"](page)
            return object()

        fake_fetcher.fetch.side_effect = fetch
        with mock.patch.object(resolver, "_DynamicFetcher", fake_fetcher), \
                mock.patch.object(resolver, "DYNAMIC_RESOLVER_DEADLINE_SECONDS", 1.0), \
                mock.patch.object(resolver, "_missing_chapter_budget_seconds", return_value=100.0):
            with self.assertRaisesRegex(
                resolver.DynamicReaderError,
                "canonical_materialization_retry_budget_insufficient",
            ):
                resolver._resolve_inline(self.URL, adapter=_FakeAdapter())
        self.assertEqual(fake_fetcher.fetch.call_count, 1)

    @unittest.skipUnless(os.name == "nt", "process-tree kill job contract is Windows-specific")
    def test_hard_deadline_terminates_and_reaps_child_and_browser_descendant(self):
        import psutil

        with tempfile.TemporaryDirectory(prefix="yomu-resolver-test-") as temp:
            marker = os.path.join(temp, "descendant.pid")
            child_code = (
                "import subprocess,sys,time; sys.stdin.readline(); "
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                f"open({marker!r},'w').write(str(p.pid)); time.sleep(30)"
            )
            started = time.monotonic()
            outcome = resolver._run_child_command(
                [sys.executable, "-c", child_code], deadline=started + 0.5)
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertTrue(outcome["terminated"])
            self.assertTrue(outcome["reaped"])
            self.assertEqual(outcome["active_processes"], 0)
            self.assertTrue(os.path.isfile(marker))
            with open(marker, encoding="utf-8") as stream:
                descendant_pid = int(stream.read())
            end = time.monotonic() + 2.0
            while psutil.pid_exists(descendant_pid) and time.monotonic() < end:
                time.sleep(0.02)
            self.assertFalse(psutil.pid_exists(descendant_pid))

    def test_dynamic_resolver_child_command_is_available_for_dev_and_frozen(self):
        import start_tradutor

        dev_command = start_tradutor.build_child_command("dynamic-resolver", frozen=False)
        frozen_command = start_tradutor.build_child_command("dynamic-resolver", frozen=True)
        self.assertTrue(os.path.isfile(dev_command[-1]))
        self.assertEqual(frozen_command[-2:], ["--internal-child", "dynamic-resolver"])

    def test_cancelled_before_extraction(self):
        with self.assertRaisesRegex(resolver.DynamicReaderError, "cancelled"):
            resolver._extract_candidates(_FakePage(), self.URL, [], lambda: True)

    def test_virtualized_slots_are_accumulated_in_reader_order(self):
        page = _VirtualizedPage()
        initial, _ = resolver._extract_candidates(page, self.URL, [], None)
        rows, diagnostics = resolver._scroll_normal_reader(page, resolver._PAGE_SELECTOR,
                                                            initial, None)
        self.assertEqual([row["url"] for row in rows],
                         [f"https://cdn.test/{index}.webp" for index in range(1, 11)])
        self.assertEqual(diagnostics["initial_slot_count"], 3)
        self.assertEqual(diagnostics["unique_page_urls_seen"], 10)
        self.assertTrue(diagnostics["dom_nodes_reused"])
        self.assertTrue(diagnostics["image_urls_change_while_slots_stay_constant"])

    def test_lazy_attributes_are_reported_without_requiring_download(self):
        page = _LazyAttributePage()
        initial, _ = resolver._extract_candidates(page, self.URL, [], None)
        _, diagnostics = resolver._scroll_normal_reader(page, resolver._PAGE_SELECTOR,
                                                         initial, None)
        self.assertEqual(diagnostics["lazy_attributes"], ["data-src", "srcset"])

    def test_missing_page_merge_is_generic_and_ordered(self):
        preload = [
            {"logical_page_index": index, "url": f"https://cdn.test/{index}.webp"}
            for index in range(1, 11) if index != 7
        ]
        fallback = [{"logical_page_index": 7, "url": "data:image/png;base64,ok"}]
        merged, missing = resolver._merge_logical_candidates(preload, fallback, 10)
        self.assertEqual([item["logical_page_index"] for item in merged], list(range(1, 11)))
        self.assertEqual(missing, [])

    def test_missing_page_merge_accepts_multiple_of_ten_pattern_without_hardcode(self):
        preload = [
            {"logical_page_index": index, "url": f"https://cdn.test/{index}.webp"}
            for index in range(1, 106) if index % 10 != 0
        ]
        fallback = [
            {"logical_page_index": index, "url": f"data:image/png;base64,{index}"}
            for index in range(10, 106, 10)
        ]
        merged, missing = resolver._merge_logical_candidates(preload, fallback, 105)
        self.assertEqual(len(merged), 105)
        self.assertEqual(missing, [])

    def test_missing_page_merge_fails_closed(self):
        preload = [
            {"logical_page_index": index, "url": f"https://cdn.test/{index}.webp"}
            for index in range(1, 6)
        ]
        merged, missing = resolver._merge_logical_candidates(preload, [], 6)
        self.assertEqual(len(merged), 5)
        self.assertEqual(missing, [6])

    def test_dynamic_fallback_materializes_rendered_canvas_not_network_resource(self):
        page = _ViewportCanvasPage()
        candidate = resolver._capture_rendered_canvas_candidate(
            page, 2, {"width": 800, "height": 1250, "resource_url": "https://cdn.test/scrambled"}
        )
        self.assertEqual(candidate["source"], "canvas_capture")
        self.assertTrue(candidate["url"].startswith("data:image/png;base64,"))
        self.assertEqual(candidate["capture_method"], "headless_dynamic_viewport_crop")
        self.assertEqual(candidate["capture_width"], 800)
        self.assertEqual(candidate["capture_height"], 1250)
        self.assertNotEqual(candidate["url"], "https://cdn.test/scrambled")

    def test_browser_png_is_promoted_without_transcode(self):
        encoded = BytesIO()
        Image.new("RGBA", (17, 29), (1, 2, 3, 255)).save(encoded, format="PNG")
        raw = {"logical_page_index": 89, "order": 88, "url": "https://cdn.test/89"}
        candidate = resolver._promote_browser_response_candidate(
            raw, encoded.getvalue(), "image/png"
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["source_content_type"], "image/png")
        self.assertEqual(candidate["canonical_content_type"], "image/png")
        self.assertFalse(candidate["transcoded"])
        self.assertEqual(candidate["logical_page_index"], 89)

    def test_browser_webp_is_decoded_and_normalized_without_resize(self):
        encoded = BytesIO()
        Image.new("RGB", (23, 31), (4, 5, 6)).save(encoded, format="WEBP", lossless=True)
        raw = {"logical_page_index": 89, "order": 88, "url": "https://cdn.test/89"}
        candidate = resolver._promote_browser_response_candidate(
            raw, encoded.getvalue(), "image/webp"
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["source_content_type"], "image/webp")
        self.assertEqual(candidate["canonical_content_type"], "image/png")
        self.assertTrue(candidate["transcoded"])
        self.assertEqual((candidate["width"], candidate["height"]), (23, 31))
        with Image.open(BytesIO(base64.b64decode(candidate["url"].split(",", 1)[1]))) as image:
            self.assertEqual(image.size, (23, 31))

    def test_browser_jpeg_is_supported_and_invalid_mime_is_rejected(self):
        encoded = BytesIO()
        Image.new("RGB", (19, 27), (7, 8, 9)).save(encoded, format="JPEG")
        raw = {"logical_page_index": 91, "order": 90, "url": "https://cdn.test/91"}
        candidate = resolver._promote_browser_response_candidate(
            raw, encoded.getvalue(), "image/jpeg; charset=binary"
        )
        self.assertIsNotNone(candidate)
        self.assertEqual((candidate["width"], candidate["height"]), (19, 27))
        self.assertIsNone(
            resolver._promote_browser_response_candidate(raw, encoded.getvalue(), "image/svg+xml")
        )
        self.assertIsNone(
            resolver._promote_browser_response_candidate(raw, b"not-an-image", "image/webp")
        )


class MaterializationRetryDecisionTests(unittest.TestCase):
    """Pure budget/progress policy: futile retries stopped and the cleanup margin
    reserved, while a flaky first pass still keeps its bounded retry."""

    def _decide(self, **overrides):
        base = dict(pending_count=3, prev_pending_count=None,
                    remaining_budget=200.0, attempt=1, max_attempts=3, pending_budget=80.0)
        base.update(overrides)
        return resolver._materialization_retry_decision(**base)

    def test_first_pass_with_budget_retries(self):
        self.assertEqual(self._decide(prev_pending_count=None, remaining_budget=200.0,
                                      pending_budget=80.0), "retry")

    def test_first_pass_without_budget_is_budget_insufficient(self):
        self.assertEqual(self._decide(prev_pending_count=None, remaining_budget=90.0,
                                      pending_budget=80.0), "budget_insufficient")

    def test_first_pass_is_never_no_progress(self):
        # prev is None -> an intermittently-flaky first pass is always allowed its retry,
        # even at zero materialization (large pending), so recovery on a later pass works.
        self.assertEqual(self._decide(prev_pending_count=None, pending_count=147,
                                      remaining_budget=5000.0, pending_budget=10.0), "retry")

    def test_retry_pass_without_shrink_is_no_progress(self):
        self.assertEqual(self._decide(attempt=2, pending_count=6, prev_pending_count=6,
                                      remaining_budget=5000.0, pending_budget=50.0),
                         "no_progress")

    def test_retry_pass_with_shrink_continues(self):
        self.assertEqual(self._decide(attempt=2, pending_count=4, prev_pending_count=6,
                                      remaining_budget=5000.0, pending_budget=50.0), "retry")

    def test_unavailable_pending_count_is_not_no_progress(self):
        # pending_count 0 (counts unavailable, e.g. mocked failure code) must fall back to
        # the bounded attempt-count retry, never a spurious no_progress.
        self.assertNotEqual(
            self._decide(attempt=2, pending_count=0, prev_pending_count=0,
                         remaining_budget=5000.0, pending_budget=8.0),
            "no_progress")

    def test_exhausted_is_failed(self):
        self.assertEqual(self._decide(attempt=3, pending_count=1, prev_pending_count=3,
                                      remaining_budget=5000.0, pending_budget=30.0), "failed")

    def test_cleanup_margin_is_reserved(self):
        # exactly pending_budget remaining is NOT enough: the safety margin must fit too.
        self.assertEqual(self._decide(remaining_budget=50.0, pending_budget=50.0),
                         "budget_insufficient")
        self.assertEqual(
            self._decide(remaining_budget=50.0 + resolver.RETRY_BUDGET_SAFETY_MARGIN_SECONDS,
                         pending_budget=50.0),
            "retry")


class ResponseBodyFilterTests(unittest.TestCase):
    """Coverage-preserving pre-filter for response.body(): skip only bodies that promotion
    would reject anyway (duplicate URL / oversize / wrong content-type), never on missing
    metadata, and always read a fresh allowed page image."""

    HOST_URL = "https://jloo.wowpic1.store/a/7.webp"

    def _decide(self, **overrides):
        base = dict(resource_type="image", resource_url=self.HOST_URL,
                    content_type_header="image/webp", content_length_header="1024",
                    already_captured=False)
        base.update(overrides)
        return resolver._should_read_response_body(**base)

    def test_fresh_allowed_page_image_is_read(self):
        self.assertEqual(self._decide(), (True, ""))

    def test_non_image_resource_is_skipped(self):
        read, reason = self._decide(resource_type="fetch")
        self.assertFalse(read)
        self.assertEqual(reason, "not_page_image")

    def test_foreign_host_is_skipped(self):
        read, reason = self._decide(resource_url="https://cdn.other.example/7.webp")
        self.assertFalse(read)
        self.assertEqual(reason, "not_page_image")

    def test_duplicate_url_is_skipped(self):
        self.assertEqual(self._decide(already_captured=True), (False, "dedup"))

    def test_disallowed_content_type_is_skipped(self):
        self.assertEqual(self._decide(content_type_header="image/gif"),
                         (False, "content_type"))

    def test_oversize_declared_length_is_skipped(self):
        big = str(resolver.MAX_BROWSER_RESPONSE_BODY_BYTES + 1)
        self.assertEqual(self._decide(content_length_header=big), (False, "oversize"))

    def test_allowed_types_are_read(self):
        for ctype in ("image/png", "image/jpeg", "image/webp", "image/webp; charset=binary"):
            self.assertEqual(self._decide(content_type_header=ctype), (True, ""), ctype)

    def test_missing_metadata_is_never_a_skip_reason(self):
        # No content-type / no content-length must still be read (promotion decides), so a
        # page that would have promoted is never dropped by a cautious filter.
        self.assertEqual(self._decide(content_type_header=None, content_length_header=None),
                         (True, ""))

    def test_unparseable_length_is_read(self):
        self.assertEqual(self._decide(content_length_header="not-a-number"), (True, ""))


if __name__ == "__main__":
    unittest.main()
