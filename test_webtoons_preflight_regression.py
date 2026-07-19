"""The navigation preflight must not veto a browser-based registered reader.

CORRECTION OF RECORD: this was written to fix a diagnosed cause of the reported Webtoons
regression — the theory being that the cookieless preflight received 403 and turned it into
source_access_denied before Chrome ever opened. A live check against the official chapter
URL refuted that theory: the preflight receives 200 and passes end to end. Adapter
selection, normalization, path validation and the preflight all work for that URL, so the
reported regression is downstream of everything covered here and remains unidentified.

What survives is a defensive contract, not a regression fix: a registered reader whose whole
strategy is a real browser should not be vetoed by a bare HTTP client's authorization
answer, because some readers do answer that way. Generic sources still fail closed, which is
what these tests mainly pin.

Fully hermetic: fake sessions, stubbed DNS, no site is contacted.
"""

import _test_bootstrap  # noqa: F401

import unittest
from unittest import mock

import chapter_source
from chapter_source import (
    SOURCE_ACCESS_DENIED, SourceError, UniversalChapterAdapter, select_adapter,
)
from download_transport import preflight_browser_navigation

OFFICIAL = ("https://www.webtoons.com/en/drama/daytime-in-the-bunker/episode-1/viewer"
            "?title_no=9842&episode_no=1")
SWAPPED = ("https://www.webtoons.com/en/drama/daytime-in-the-bunker/episode-1/viewer"
           "?episode_no=1&title_no=9842")


def public_dns(*_a, **_k):
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


class _Response:
    def __init__(self, status=200, headers=None, body=b""):
        self.status_code = status
        self.headers = headers or {}
        self._body = body

    def iter_content(self, chunk_size=4096):
        yield self._body

    def close(self):
        pass


class _Session:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []
        self.trust_env = True

    def get(self, url, **_kw):
        self.requests.append(url)
        return self._responses.pop(0)

    def close(self):
        pass


class AdapterSelectionTests(unittest.TestCase):
    def test_official_url_selects_the_webtoons_adapter(self):
        self.assertEqual(select_adapter(OFFICIAL).name, "webtoons")

    def test_query_order_does_not_change_the_adapter_or_identity(self):
        first, second = select_adapter(OFFICIAL), select_adapter(SWAPPED)
        self.assertEqual(first.name, second.name)
        # Both carry the same functional identity: host + path + title_no + episode_no.
        for url in (OFFICIAL, SWAPPED):
            normalized = first.normalize_url(url)
            self.assertIn("title_no=9842", normalized)
            self.assertIn("episode_no=1", normalized)
            self.assertIn("/viewer", normalized)

    def test_chapter_path_is_accepted_and_non_chapter_paths_are_not(self):
        adapter = select_adapter(OFFICIAL)
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            adapter.validate_url(OFFICIAL)
            adapter.validate_path(OFFICIAL)
            for bad in ("https://www.webtoons.com/en/drama/daytime-in-the-bunker/list?title_no=9842",
                        "https://www.webtoons.com/en/creator/abc",
                        "https://www.webtoons.com/en/ranking"):
                with self.assertRaises(SourceError, msg=bad):
                    adapter.validate_path(bad)


class PreflightRegressionTests(unittest.TestCase):
    def test_403_no_longer_blocks_a_browser_based_reader(self):
        # The regression in one line: a bare HTTP client is refused, Chrome would not be.
        adapter = select_adapter(OFFICIAL)
        session = _Session([_Response(status=403, body=b"<html>bot check</html>")])
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            resolved = preflight_browser_navigation(adapter, OFFICIAL, session=session)
        self.assertEqual(resolved, OFFICIAL)

    def test_401_also_yields_to_the_browser_for_a_registered_reader(self):
        adapter = select_adapter(OFFICIAL)
        session = _Session([_Response(status=401)])
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            self.assertEqual(
                preflight_browser_navigation(adapter, OFFICIAL, session=session), OFFICIAL)

    def test_generic_source_still_fails_closed_on_403(self):
        # The universal path must not be weakened to make Webtoons work.
        adapter = UniversalChapterAdapter("https://exemplo.test/serie/x/cap-1")
        self.assertFalse(adapter.browser_owned_readiness)
        session = _Session([_Response(status=403, body=b"denied")])
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            with self.assertRaises(SourceError) as ctx:
                preflight_browser_navigation(
                    adapter, "https://exemplo.test/serie/x/cap-1", session=session)
        self.assertEqual(ctx.exception.code, SOURCE_ACCESS_DENIED)

    def test_redirect_hops_are_still_revalidated_for_a_reader(self):
        # The SSRF purpose of the preflight is untouched: a hop off the vetted host fails.
        adapter = select_adapter(OFFICIAL)
        session = _Session([
            _Response(status=302, headers={"Location": "http://127.0.0.1:8080/x"})])
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            with self.assertRaises(SourceError):
                preflight_browser_navigation(adapter, OFFICIAL, session=session)

    def test_redirect_to_a_non_chapter_path_is_still_refused(self):
        adapter = select_adapter(OFFICIAL)
        session = _Session([
            _Response(status=302,
                      headers={"Location": "https://www.webtoons.com/en/ranking"})])
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            with self.assertRaises(SourceError):
                preflight_browser_navigation(adapter, OFFICIAL, session=session)

    def test_ordinary_success_returns_the_final_url(self):
        adapter = select_adapter(OFFICIAL)
        session = _Session([_Response(status=200)])
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            self.assertEqual(
                preflight_browser_navigation(adapter, OFFICIAL, session=session), OFFICIAL)

    def test_rate_limiting_is_still_reported_for_a_reader(self):
        # 429 is about the origin being busy, not about needing a browser: keep reporting it.
        adapter = select_adapter(OFFICIAL)
        session = _Session([_Response(status=429)])
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            with self.assertRaises(SourceError) as ctx:
                preflight_browser_navigation(adapter, OFFICIAL, session=session)
        self.assertEqual(ctx.exception.code, "source_rate_limited")


class ReaderSelectorOwnershipTests(unittest.TestCase):
    def test_webtoons_selectors_live_on_the_adapter(self):
        selectors = select_adapter(OFFICIAL).reader_selectors()
        self.assertTrue(selectors.get("image"))
        self.assertTrue(selectors.get("container"))

    def test_downloader_holds_no_webtoons_selectors(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parent / "down.py").read_text(encoding="utf-8")
        for leaked in ("#_imageList", "viewer_img", "img._images", "viewer_lst"):
            self.assertNotIn(leaked, source, leaked)


if __name__ == "__main__":
    unittest.main()
