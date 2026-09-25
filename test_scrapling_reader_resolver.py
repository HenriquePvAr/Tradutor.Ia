import unittest
from unittest import mock
from io import BytesIO
import base64

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
                resolver.resolve(self.URL, adapter=_FakeAdapter())

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
            with self.assertRaisesRegex(resolver.DynamicReaderError,
                                        "canonical_materialization_failed"):
                resolver.resolve(self.URL, adapter=_FakeAdapter())

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


if __name__ == "__main__":
    unittest.main()
