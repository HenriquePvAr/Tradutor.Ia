"""HTTP-first chapter discovery: static-HTML collection plus the HTTP/browser strategy seam.

Hermetic: no real site is ever contacted. ``discover_via_http`` is exercised against small,
representative HTML fixtures through a fake transport; network/DNS calls are never made.
"""

import _test_bootstrap  # noqa: F401

import unittest
from unittest import mock

import chapter_source
import down
import http_source_discovery
from chapter_source import UniversalChapterAdapter, VortexScansAdapter, WEBTOONS
from http_source_discovery import discover_via_http

VORTEX_URL = "https://vortexscans.org/series/demo-series/chapter-1"


def _vortex_html(pages, *, relative=False, hidden_duplicate=False):
    """Build a minimal Vortex-shaped reader page for ``pages`` (src, alt) tuples."""
    base = "" if relative else "https://storage.vortexscans.org/upload/demo/"
    figures = "".join(
        f'<figure class="image-container m-0 w-full">'
        f'<img src="{base}{src}" width="800" height="1200" alt="{alt}" '
        f'class="h-auto w-full" data-reader-page-image data-reader-index="{index}" '
        f'loading="eager"><figcaption>Page {index + 1}</figcaption></figure>'
        for index, (src, alt) in enumerate(pages)
    )
    hidden = ""
    if hidden_duplicate:
        hidden = (
            '<section class="hidden" itemprop="articleBody" '
            'style="position:absolute;left:-99999px">'
            '<figure><meta itemprop="image" content="https://x/hidden.webp"></figure>'
            "</section>"
        )
    return (
        "<html><body>"
        '<article class="immersive-reader min-h-screen">'
        f"{hidden}"
        '<section itemprop="articleBody">'
        f'<div class="comic-images-wrapper">{figures}</div>'
        "</section></article></body></html>"
    )


class FakeTransport:
    """Stands in for ``RequestsMetadataTransport``: no network, one canned page."""

    def __init__(self, html, *, raises=None):
        self.html = html
        self.raises = raises
        self.closed = False
        self.fetched_urls = []

    def __call__(self, adapter):  # mimics RequestsMetadataTransport(adapter)
        return self

    def fetch_page(self, url):
        self.fetched_urls.append(url)
        if self.raises is not None:
            raise self.raises
        return self.html

    def close(self):
        self.closed = True


class VortexHTMLCollectorTests(unittest.TestCase):
    """TEST 1-6: the adapter's own static-HTML reader collector."""

    def test_extracts_pages_from_supported_html(self):
        adapter = VortexScansAdapter()
        html = _vortex_html([(f"page-{i:02}.webp", f"Page {i}") for i in range(3)])
        candidates = adapter.collect_dom_candidates_from_html(html, VORTEX_URL)
        self.assertEqual(len(candidates), 3)
        self.assertTrue(all(c["url"].endswith(".webp") for c in candidates))

    def test_order_is_preserved(self):
        adapter = VortexScansAdapter()
        html = _vortex_html([(f"page-{i:02}.webp", f"Page {i}") for i in range(5)])
        candidates = adapter.collect_dom_candidates_from_html(html, VORTEX_URL)
        urls = [c["url"] for c in candidates]
        self.assertEqual(urls, sorted(urls))  # page-00 < page-01 < ... lexicographically
        self.assertEqual([c["y"] for c in candidates], [0, 1, 2, 3, 4])

    def test_falls_back_to_data_src_when_src_is_absent(self):
        adapter = VortexScansAdapter()
        html = (
            "<html><body>"
            '<article class="immersive-reader"><section itemprop="articleBody">'
            '<figure class="image-container"><img data-src="https://x/page-1.webp" '
            'width="800" height="1200" data-reader-page-image data-reader-index="0">'
            "</figure></section></article></body></html>"
        )
        candidates = adapter.collect_dom_candidates_from_html(html, VORTEX_URL)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["url"], "https://x/page-1.webp")

    def test_relative_urls_are_absolutized_by_the_shared_pipeline(self):
        adapter = VortexScansAdapter()
        html = _vortex_html([("page-01.webp", "Page 1")], relative=True)
        candidates = adapter.collect_dom_candidates_from_html(html, VORTEX_URL)
        self.assertEqual(candidates[0]["url"], "page-01.webp")  # raw, still relative here
        # Absolutization happens downstream in analyse_candidates/_to_candidate; prove the
        # full pipeline resolves it against page_url.
        with mock.patch.object(chapter_source.socket, "getaddrinfo",
                                return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
            from universal_chapter_adapter import analyse_candidates

            analysis = analyse_candidates(
                VORTEX_URL, candidates, adapter=adapter, final_url=VORTEX_URL,
                cluster_score=adapter.score_cluster)
        self.assertEqual(len(analysis.accepted), 1)
        self.assertTrue(analysis.accepted[0].url.startswith(VORTEX_URL.rsplit("/", 1)[0]))

    def test_duplicate_images_do_not_enter_the_accepted_set_twice(self):
        adapter = VortexScansAdapter()
        html = _vortex_html([("page-01.webp", "Page 1"), ("page-01.webp", "Page 1 again")])
        candidates = adapter.collect_dom_candidates_from_html(html, VORTEX_URL)
        with mock.patch.object(chapter_source.socket, "getaddrinfo",
                                return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
            from universal_chapter_adapter import analyse_candidates

            analysis = analyse_candidates(
                VORTEX_URL, candidates, adapter=adapter, final_url=VORTEX_URL,
                cluster_score=adapter.score_cluster)
        self.assertEqual(len(analysis.accepted), 1)

    def test_hidden_seo_duplicate_section_is_ignored(self):
        adapter = VortexScansAdapter()
        html = _vortex_html([(f"page-{i:02}.webp", f"Page {i}") for i in range(2)],
                             hidden_duplicate=True)
        candidates = adapter.collect_dom_candidates_from_html(html, VORTEX_URL)
        self.assertEqual(len(candidates), 2)

    def test_unsupported_html_returns_none_not_a_partial_list(self):
        adapter = VortexScansAdapter()
        html = "<html><body><p>Not a reader page at all.</p></body></html>"
        self.assertIsNone(adapter.collect_dom_candidates_from_html(html, VORTEX_URL))

    def test_generic_adapters_have_no_http_collector_by_default(self):
        # WebtoonsAdapter/UniversalChapterAdapter never overrode this hook: the browser
        # fallback stays the only path for them.
        self.assertIsNone(WEBTOONS.collect_dom_candidates_from_html("<html></html>", "x"))
        universal = UniversalChapterAdapter("https://example.test/x")
        self.assertIsNone(universal.collect_dom_candidates_from_html("<html></html>", "x"))


class DiscoverViaHttpTests(unittest.TestCase):
    """TEST 7-8: the HTTP/browser strategy seam itself."""

    def _dns_patch(self):
        return mock.patch.object(
            chapter_source.socket, "getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 0))])

    def test_supported_adapter_returns_a_usable_analysis_without_any_browser_call(self):
        adapter = VortexScansAdapter()
        html = _vortex_html([(f"page-{i:02}.webp", f"Page {i}") for i in range(3)])
        fake = FakeTransport(html)
        with self._dns_patch(), \
             mock.patch.object(http_source_discovery, "RequestsMetadataTransport", fake):
            analysis = discover_via_http(adapter, VORTEX_URL)
        self.assertIsNotNone(analysis)
        self.assertEqual(len(analysis.accepted), 3)
        self.assertTrue(analysis.can_download)
        self.assertEqual(analysis.reader_diagnostics.get("discovery_transport"), "http")
        self.assertTrue(fake.closed)

    def test_adapter_without_http_collector_returns_none_and_never_fetches(self):
        adapter = WEBTOONS
        fake = FakeTransport("<html></html>")
        with mock.patch.object(http_source_discovery, "RequestsMetadataTransport", fake):
            result = discover_via_http(adapter, "https://webtoons.com/x")
        self.assertIsNone(result)
        self.assertEqual(fake.fetched_urls, [])  # never even tried: no browser cost paid either

    def test_transport_failure_is_a_controlled_none_not_an_exception(self):
        adapter = VortexScansAdapter()
        fake = FakeTransport("", raises=RuntimeError("boom"))
        with mock.patch.object(http_source_discovery, "RequestsMetadataTransport", fake):
            result = discover_via_http(adapter, VORTEX_URL)
        self.assertIsNone(result)

    def test_unsupported_page_shape_falls_back_to_none(self):
        adapter = VortexScansAdapter()
        fake = FakeTransport("<html><body>not a reader</body></html>")
        with mock.patch.object(http_source_discovery, "RequestsMetadataTransport", fake):
            result = discover_via_http(adapter, VORTEX_URL)
        self.assertIsNone(result)


class DiscoverChapterSourceTests(unittest.TestCase):
    """TEST 8 (again, at the public seam): browser never runs once HTTP evidence suffices."""

    def test_browser_analysis_is_never_invoked_when_http_discovery_succeeds(self):
        sentinel = mock.Mock(name="http_analysis")
        sentinel.canonical_url = ""
        with mock.patch("down._resolve_canonical_source", return_value=None), \
             mock.patch("http_source_discovery.discover_via_http",
                        return_value=sentinel) as fake_http, \
             mock.patch("down.analyze_chapter_source",
                        side_effect=AssertionError("browser must not run")) as fake_browser:
            result = down.discover_chapter_source(
                "https://vortexscans.org/series/demo-series/chapter-1")
        fake_http.assert_called_once()
        fake_browser.assert_not_called()
        self.assertIs(result, sentinel)

    def test_falls_back_to_browser_when_http_discovery_is_inconclusive(self):
        sentinel = mock.Mock(name="browser_analysis")
        with mock.patch("down._resolve_canonical_source", return_value=None), \
             mock.patch("http_source_discovery.discover_via_http", return_value=None), \
             mock.patch("down.analyze_chapter_source", return_value=sentinel) as fake_browser:
            result = down.discover_chapter_source(
                "https://vortexscans.org/series/demo-series/chapter-1")
        fake_browser.assert_called_once()
        self.assertIs(result, sentinel)


class WorkerAndUiWiringTests(unittest.TestCase):
    """TEST 9-10: the worker and UI seams both use HTTP-first discovery, not Selenium directly."""

    def test_worker_analyze_source_delegates_to_http_first_discovery(self):
        from worker_service import Worker

        sentinel = mock.Mock(name="analysis")
        worker = Worker.__new__(Worker)  # avoid full worker construction; only testing the seam
        with mock.patch("down.discover_chapter_source", return_value=sentinel) as fake:
            result = Worker._analyze_source(worker, "https://vortexscans.org/series/x/chapter-1")
        fake.assert_called_once()
        self.assertIs(result, sentinel)

    def test_ui_analyze_source_delegates_to_http_first_discovery(self):
        import ui_bridge

        sentinel = mock.Mock(name="analysis")
        with mock.patch("down.discover_chapter_source", return_value=sentinel) as fake:
            result = ui_bridge.UiBridge._analyze_source(
                "https://vortexscans.org/series/x/chapter-1")
        fake.assert_called_once()
        self.assertIs(result, sentinel)


if __name__ == "__main__":
    unittest.main()
