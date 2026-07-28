"""Hermetic canonical chapter identity tests.

The fixtures model public series metadata.  They never contact a real source.
"""

import _test_bootstrap  # noqa: F401

import unittest

from canonical_source_identity import (
    CanonicalSourceError,
    canonicalize_webtoons_url,
)


SERIES_URL = (
    "https://www.webtoons.com/en/fantasy/demo-series/"
    "episode-105/viewer?title_no=6465&episode_no=187"
)
CANONICAL_URL = (
    "https://www.webtoons.com/en/fantasy/demo-series/"
    "episode-105/viewer?title_no=6465&episode_no=107"
)


def listing(*links):
    return (
        "<!doctype html><html><body><ul class='episode_list'>"
        + "".join(f"<li><a href='{link}'>chapter</a></li>" for link in links)
        + "</ul></body></html>"
    )


class FakeMetadataTransport:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def fetch_page(self, url):
        self.calls.append(url)
        if not self.pages:
            return ""
        return self.pages.pop(0)


class FakeWebtoonsAdapter:
    name = "webtoons"

    def validate_navigation_url(self, _url):
        return None


class CanonicalWebtoonsIdentityTests(unittest.TestCase):
    def resolve(self, submitted=SERIES_URL, *pages):
        transport = FakeMetadataTransport(pages or (listing(CANONICAL_URL),))
        result = canonicalize_webtoons_url(
            submitted, transport=transport, adapter=FakeWebtoonsAdapter())
        return result, transport

    def test_official_link_replaces_inconsistent_internal_episode_id(self):
        result, transport = self.resolve()
        self.assertEqual(result.canonical_url, CANONICAL_URL)
        self.assertEqual(result.series_id, "6465")
        self.assertEqual(result.episode_id, "107")
        self.assertEqual(result.episode_slug, "episode-105")
        self.assertTrue(result.changed)
        self.assertEqual(len(transport.calls), 1)
        public = result.public()
        self.assertEqual(public["validation_status"], "canonical_source_resolved")
        self.assertEqual(public["episode_identifier"], "107")
        self.assertEqual(public["episode_label"], "episode-105")
        self.assertEqual(len(public["identity_hash"]), 64)
        self.assertEqual(public["canonical_url"], CANONICAL_URL)

    def test_matching_canonical_url_is_idempotent(self):
        result, _ = self.resolve(CANONICAL_URL)
        self.assertEqual(result.canonical_url, CANONICAL_URL)
        self.assertFalse(result.changed)
        self.assertEqual(
            result.public()["validation_status"], "canonical_source_confirmed")

    def test_identity_uses_official_slug_not_arithmetic_offset(self):
        unrelated = CANONICAL_URL.replace("episode-105", "episode-185")
        transport = FakeMetadataTransport([listing(unrelated)])
        with self.assertRaises(CanonicalSourceError) as caught:
            canonicalize_webtoons_url(
                SERIES_URL, transport=transport, adapter=FakeWebtoonsAdapter())
        self.assertEqual(caught.exception.code, "canonical_episode_not_found")

    def test_prologue_epilogue_special_and_bgm_labels_are_not_numeric(self):
        for slug in ("prologue", "epilogue", "special-episode", "bgm-episode"):
            submitted = SERIES_URL.replace("episode-105", slug)
            official = CANONICAL_URL.replace("episode-105", slug)
            with self.subTest(slug=slug):
                result, _ = self.resolve(submitted, listing(official))
                self.assertEqual(result.canonical_url, official)

    def test_season_label_is_preserved_without_numeric_inference(self):
        submitted = SERIES_URL.replace("episode-105", "season-2-episode-1")
        official = CANONICAL_URL.replace("episode-105", "season-2-episode-1")
        result, _ = self.resolve(submitted, listing(official))
        self.assertEqual(result.episode_slug, "season-2-episode-1")
        self.assertEqual(result.episode_id, "107")

    def test_wrong_series_identifier_fails_closed(self):
        wrong_series = CANONICAL_URL.replace("title_no=6465", "title_no=9999")
        with self.assertRaises(CanonicalSourceError) as caught:
            self.resolve(SERIES_URL, listing(wrong_series))
        self.assertEqual(caught.exception.code, "canonical_episode_not_found")

    def test_wrong_series_slug_fails_closed(self):
        wrong_slug = CANONICAL_URL.replace("/demo-series/", "/other-series/")
        with self.assertRaises(CanonicalSourceError) as caught:
            self.resolve(SERIES_URL, listing(wrong_slug))
        self.assertEqual(caught.exception.code, "canonical_episode_not_found")

    def test_ambiguous_official_links_fail_closed(self):
        second = CANONICAL_URL.replace("episode_no=107", "episode_no=108")
        with self.assertRaises(CanonicalSourceError) as caught:
            self.resolve(SERIES_URL, listing(CANONICAL_URL, second))
        self.assertEqual(caught.exception.code, "canonical_episode_ambiguous")

    def test_duplicate_official_link_is_one_identity(self):
        result, _ = self.resolve(SERIES_URL, listing(CANONICAL_URL, CANONICAL_URL))
        self.assertEqual(result.canonical_url, CANONICAL_URL)

    def test_metadata_pagination_is_bounded_and_stops_after_match(self):
        transport = FakeMetadataTransport([
            listing(CANONICAL_URL.replace("episode-105", "episode-104")),
            listing(CANONICAL_URL),
            listing(CANONICAL_URL.replace("episode-105", "episode-106")),
        ])
        result = canonicalize_webtoons_url(
            SERIES_URL, transport=transport, adapter=FakeWebtoonsAdapter(), max_pages=3)
        self.assertEqual(result.canonical_url, CANONICAL_URL)
        self.assertEqual(len(transport.calls), 2)

    def test_empty_or_exhausted_metadata_fails_closed(self):
        transport = FakeMetadataTransport(["", ""])
        with self.assertRaises(CanonicalSourceError) as caught:
            canonicalize_webtoons_url(
                SERIES_URL, transport=transport, adapter=FakeWebtoonsAdapter(), max_pages=2)
        self.assertEqual(caught.exception.code, "canonical_episode_not_found")


if __name__ == "__main__":
    unittest.main()
