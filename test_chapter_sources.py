"""Adapter contract, SSRF hardening, candidate classification and coded failures.

Hermetic: no real site is ever contacted. Host resolution is stubbed where a DNS answer
would otherwise be needed.
"""

import unittest
from unittest import mock

import chapter_source
from chapter_source import (
    ALLOWED_IMAGE_MIME, CHALLENGE_REQUIRED, GenericImageChapterAdapter, SourceError,
    UNSUPPORTED_SOURCE, UnsupportedSource, WEBTOONS, host_of, is_private_host,
    looks_like_challenge, select_adapter, supported_hosts,
)

WEBTOON_URL = "https://www.webtoons.com/en/fantasy/serie/ep-1/viewer?title_no=1&episode_no=1"


def public_dns(*_args, **_kwargs):
    """Pretend every hostname resolves to a public address."""
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


class HostTests(unittest.TestCase):
    def test_host_normalization(self):
        self.assertEqual(host_of("https://WWW.Webtoons.COM/x"), "webtoons.com")
        self.assertEqual(host_of("garbage"), "")

    def test_lookalike_hosts_rejected(self):
        for impostor in ("https://evil-webtoons.com/x", "https://webtoons.com.evil.net/x",
                         "https://notwebtoons.com/x"):
            with self.assertRaises(UnsupportedSource, msg=impostor):
                select_adapter(impostor)

    def test_subdomain_allowed(self):
        self.assertEqual(select_adapter("https://m.webtoons.com/x").name, "webtoons")

    def test_supported_hosts_exposed(self):
        self.assertIn("webtoons.com", supported_hosts())

    def test_registry_has_no_universal_fallback(self):
        # An unknown host must never resolve to some catch-all adapter.
        with self.assertRaises(UnsupportedSource):
            select_adapter("https://example.org/series/x/chapter-1")


class SsrfTests(unittest.TestCase):
    def test_private_and_loopback_literals_blocked(self):
        for host in ("127.0.0.1", "10.0.0.5", "192.168.1.10", "172.16.0.1",
                     "169.254.169.254", "::1", "0.0.0.0", "localhost"):
            self.assertTrue(is_private_host(host), host)

    def test_public_literal_allowed(self):
        self.assertFalse(is_private_host("93.184.216.34"))

    def test_hostname_resolving_inward_is_blocked(self):
        with mock.patch.object(chapter_source.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("127.0.0.1", 0))]):
            self.assertTrue(is_private_host("sneaky.example.com"))

    def test_unresolvable_host_is_refused(self):
        with mock.patch.object(chapter_source.socket, "getaddrinfo",
                               side_effect=OSError("nxdomain")):
            self.assertTrue(is_private_host("nope.invalid"))

    def test_url_validation_blocks_private_target(self):
        adapter = GenericImageChapterAdapter(name="lab", allowed_hosts=("intranet.test",))
        with mock.patch.object(chapter_source.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("10.1.1.1", 0))]):
            with self.assertRaises(SourceError) as ctx:
                adapter.validate_url("https://intranet.test/chapter-1")
        self.assertEqual(ctx.exception.detail, "private_host")

    def test_non_http_schemes_rejected(self):
        for bad in ("file:///C:/secret.txt", "data:text/html,x", "ftp://webtoons.com/x",
                    "gopher://webtoons.com/x"):
            with self.assertRaises(SourceError, msg=bad):
                WEBTOONS.validate_url(bad)

    def test_credentials_in_url_rejected(self):
        with self.assertRaises(SourceError) as ctx:
            WEBTOONS.validate_url("https://user:pw@webtoons.com/x")
        self.assertEqual(ctx.exception.detail, "credentials_in_url")

    def test_redirect_is_revalidated_against_the_adapter(self):
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            WEBTOONS.validate_redirect(WEBTOON_URL)              # allowed target is fine
            for bad in ("https://elsewhere.example/x", "http://127.0.0.1/x"):
                with self.assertRaises(SourceError, msg=bad):
                    WEBTOONS.validate_redirect(bad)


class PathTests(unittest.TestCase):
    def test_chapter_path_required_when_markers_declared(self):
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            WEBTOONS.validate_path(WEBTOON_URL)                   # /viewer
            with self.assertRaises(SourceError) as ctx:
                WEBTOONS.validate_path("https://www.webtoons.com/en/fantasy/serie/list")
        self.assertEqual(ctx.exception.detail, "not_a_chapter_url")

    def test_adapter_without_markers_accepts_any_path(self):
        GenericImageChapterAdapter(name="x", allowed_hosts=("h.test",)).validate_path(
            "https://h.test/anything")


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.adapter = WEBTOONS

    def big(self, **over):
        base = {"url": "https://img.example/p1.webp", "naturalWidth": 800,
                "naturalHeight": 1200, "inContainer": True, "className": "", "id": "",
                "alt": ""}
        base.update(over)
        return base

    def test_container_membership_marks_a_chapter_page(self):
        self.assertEqual(self.adapter.classify_candidate(self.big()), "chapter")

    def test_large_image_outside_container_still_counts(self):
        # Class names churn, so size is a fallback signal rather than the only one.
        self.assertEqual(
            self.adapter.classify_candidate(self.big(inContainer=False)), "chapter")

    def test_small_image_outside_container_is_interface(self):
        self.assertEqual(
            self.adapter.classify_candidate(
                self.big(inContainer=False, naturalWidth=32, naturalHeight=32)),
            "interface")

    def test_interface_furniture_is_excluded_by_evidence(self):
        for marker, field in (("logo", "className"), ("favicon", "url"),
                              ("banner", "id"), ("avatar", "className"),
                              ("advert", "className"), ("thumbnail", "url"),
                              ("recommend", "className"), ("footer", "id"),
                              ("comment", "className"), ("icon", "url")):
            candidate = self.big(**{field: f"x-{marker}-y"})
            self.assertEqual(self.adapter.exclude_candidate(candidate), marker, marker)
            self.assertEqual(self.adapter.classify_candidate(candidate), "interface")

    def test_tracking_pixel_excluded(self):
        pixel = self.big(naturalWidth=1, naturalHeight=1, url="https://t.example/p.gif")
        self.assertEqual(self.adapter.exclude_candidate(pixel), "tracking_pixel")

    def test_relative_urls_are_absolutized(self):
        self.assertEqual(
            self.adapter.absolutize("https://h.test/series/x/chapter-1", "../img/p1.webp"),
            "https://h.test/series/img/p1.webp")
        self.assertEqual(
            self.adapter.absolutize("https://h.test/a/b", "/p1.png"), "https://h.test/p1.png")


class SelectorOwnershipTests(unittest.TestCase):
    def test_adapter_owns_its_selectors(self):
        selectors = WEBTOONS.reader_selectors()
        self.assertIn("_imageList", selectors["image"])
        self.assertIn("container", selectors)

    def test_downloader_holds_no_site_specific_selectors(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parent / "down.py").read_text(encoding="utf-8")
        for leaked in ("#_imageList", "_viewerBox", "viewer_img", "img._images"):
            self.assertNotIn(leaked, source, leaked)

    def test_generic_adapter_is_not_registered_by_default(self):
        # It is a template requiring explicit hosts, never an automatic fallback.
        self.assertNotIn("generic", [a.name for a in chapter_source.ADAPTERS])


class ChallengeTests(unittest.TestCase):
    def test_challenge_markers_detected(self):
        for page in ("<div id='cf-challenge'>", "Checking your browser before accessing",
                     "<script src='turnstile'>", "Verify you are human",
                     "challenge-platform"):
            self.assertTrue(looks_like_challenge(page), page)

    def test_ordinary_page_is_not_a_challenge(self):
        self.assertFalse(looks_like_challenge("<div class='reader'><img src='p1.webp'>"))

    def test_challenge_is_a_terminal_coded_failure(self):
        error = chapter_source.ChallengeRequired("turnstile")
        self.assertEqual(error.code, CHALLENGE_REQUIRED)
        # Sanitized: the code only, never a page body or headers.
        self.assertEqual(WEBTOONS.sanitize_error(error), CHALLENGE_REQUIRED)


class SanitizationTests(unittest.TestCase):
    def test_arbitrary_errors_reduce_to_a_class_name(self):
        self.assertEqual(
            WEBTOONS.sanitize_error(RuntimeError("cookie=abc; Authorization: Bearer xyz")),
            "RuntimeError")

    def test_coded_errors_keep_their_code_only(self):
        self.assertEqual(WEBTOONS.sanitize_error(UnsupportedSource("h.test")),
                         UNSUPPORTED_SOURCE)

    def test_allowed_image_mimes_declared(self):
        for mime in ("image/jpeg", "image/png", "image/webp", "image/avif"):
            self.assertIn(mime, ALLOWED_IMAGE_MIME)
        self.assertNotIn("text/html", ALLOWED_IMAGE_MIME)


class DownloaderGateTests(unittest.TestCase):
    def test_unregistered_host_fails_before_any_browser_opens(self):
        import down

        with mock.patch.object(down, "_create_driver") as driver:
            with self.assertRaises(SourceError) as ctx:
                down.download_images("https://example.org/series/x/chapter-1")
        self.assertEqual(ctx.exception.code, UNSUPPORTED_SOURCE)
        driver.assert_not_called()          # no Selenium, no network

    def test_private_target_fails_before_any_browser_opens(self):
        import down

        with mock.patch.object(down, "_create_driver") as driver:
            with self.assertRaises(SourceError):
                down.download_images("http://127.0.0.1:8080/series/x/chapter-1")
        driver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
