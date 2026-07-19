"""Adapter contract, SSRF hardening, candidate classification and coded failures.

Hermetic: no real site is ever contacted. Host resolution is stubbed where a DNS answer
would otherwise be needed.
"""

import _test_bootstrap  # noqa: F401

import unittest
from unittest import mock

import chapter_source
from chapter_source import (
    ALLOWED_IMAGE_MIME, CHALLENGE_REQUIRED, GenericImageChapterAdapter, SourceError,
    UNSUPPORTED_SOURCE, UnsupportedSource, UniversalChapterAdapter, WEBTOONS, host_of, is_private_host,
    VORTEXSCANS, VortexScansAdapter, looks_like_challenge, select_adapter, supported_hosts,
)

WEBTOON_URL = "https://www.webtoons.com/en/fantasy/serie/ep-1/viewer?title_no=1&episode_no=1"
VORTEX_CHAPTER_URL = "https://vortexscans.org/series/demo-series/chapter-42"


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
            adapter = select_adapter(impostor)
            self.assertIsInstance(adapter, UniversalChapterAdapter, impostor)
            self.assertNotEqual(adapter.name, WEBTOONS.name)

    def test_subdomain_allowed(self):
        self.assertEqual(select_adapter("https://m.webtoons.com/x").name, "webtoons")

    def test_supported_hosts_exposed(self):
        self.assertIn("webtoons.com", supported_hosts())
        self.assertIn("vortexscans.org", supported_hosts())

    def test_unknown_public_host_uses_controlled_universal_fallback(self):
        adapter = select_adapter("https://example.org/series/x/chapter-1")
        self.assertIsInstance(adapter, UniversalChapterAdapter)
        self.assertFalse(adapter.is_specific)
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            adapter.validate_url("https://example.org/series/x/chapter-1")


class VortexScansAdapterTests(unittest.TestCase):
    def test_literal_host_and_valid_chapter_path_select_specific_adapter(self):
        adapter = select_adapter(VORTEX_CHAPTER_URL)
        self.assertIsInstance(adapter, VortexScansAdapter)
        # The registry retains a stable prototype, but every run receives fresh ephemeral
        # CDN authority so a previous chapter cannot leak resource-host grants.
        self.assertIsNot(adapter, VORTEXSCANS)
        self.assertEqual(adapter.name, "vortexscans")
        self.assertEqual(adapter.adapter_version, "1")
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            adapter.validate_url(VORTEX_CHAPTER_URL)
            adapter.validate_navigation_url(VORTEX_CHAPTER_URL)
        adapter.validate_path(VORTEX_CHAPTER_URL)

    def test_only_literal_vortex_host_is_claimed(self):
        for url in (
            "https://www.vortexscans.org/series/demo-series/chapter-42",
            "https://cdn.vortexscans.org/series/demo-series/chapter-42",
            "https://vortexscans.org.example.test/series/demo-series/chapter-42",
        ):
            self.assertIsInstance(select_adapter(url), UniversalChapterAdapter, url)
            self.assertFalse(VORTEXSCANS.supports(url), url)

    def test_path_requires_series_slug_and_chapter_slug(self):
        for url in (
            "https://vortexscans.org/",
            "https://vortexscans.org/series/demo-series",
            "https://vortexscans.org/series/demo-series/chapter-",
            "https://vortexscans.org/chapter-42",
            "https://vortexscans.org/series/demo_series/chapter-42",
        ):
            with self.assertRaises(SourceError) as ctx:
                VORTEXSCANS.validate_path(url)
            self.assertEqual(ctx.exception.detail, "not_a_chapter_url", url)

    def test_reader_selectors_are_owned_by_vortex_adapter(self):
        selectors = VORTEXSCANS.reader_selectors()
        self.assertIn("reading-content", selectors["container"])
        self.assertIn("reading-content", selectors["image"])
        self.assertNotIn("_imageList", selectors["image"])

    def test_observed_vortex_cdn_requires_selected_run_authorization(self):
        cdn = "https://cdn.reader-assets.example.test/pages/001.webp"
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            adapter = select_adapter(VORTEX_CHAPTER_URL)
            adapter.validate_url(VORTEX_CHAPTER_URL)
            with self.assertRaises(SourceError):
                adapter.validate_url(cdn)
            adapter.validate_observed_url(cdn)
            adapter.authorize_related_url(cdn)
            adapter.validate_url(cdn)
            # A separately selected chapter gets a fresh adapter and no inherited CDN grant.
            with self.assertRaises(SourceError):
                select_adapter(VORTEX_CHAPTER_URL).validate_url(cdn)


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

    def test_registered_www_alias_resolves_its_literal_host_before_acceptance(self):
        """Host policy can alias www; DNS policy must validate the literal destination."""
        def split_dns(host, _port):
            address = "127.0.0.1" if host == "www.webtoons.com" else "93.184.216.34"
            return [(2, 1, 6, "", (address, 0))]

        with mock.patch.object(chapter_source.socket, "getaddrinfo", side_effect=split_dns):
            with self.assertRaises(SourceError) as ctx:
                WEBTOONS.validate_url("https://www.webtoons.com/en/viewer")
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

    def test_specific_redirect_cannot_fall_back_to_a_series_or_home_page(self):
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            with self.assertRaises(SourceError) as ctx:
                VORTEXSCANS.validate_redirect("https://vortexscans.org/series/demo-series")
        self.assertEqual(ctx.exception.detail, "not_a_chapter_url")


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
    def test_unregistered_host_is_not_claimed_by_a_specific_adapter(self):
        adapter = select_adapter("https://example.org/series/x/chapter-1")
        self.assertEqual(adapter.name, "universal")
        self.assertNotIn("example.org", supported_hosts())

    def test_private_target_fails_before_any_browser_opens(self):
        import down

        with mock.patch.object(down, "_create_driver") as driver:
            with self.assertRaises(SourceError):
                down.download_images("http://127.0.0.1:8080/series/x/chapter-1")
        driver.assert_not_called()

    def test_unsafe_scheme_fails_before_any_browser_opens_even_with_fallback(self):
        import down

        with mock.patch.object(down, "_create_driver") as driver:
            with self.assertRaises(SourceError) as ctx:
                down.download_images("file:///C:/secret.txt")
        self.assertEqual(ctx.exception.code, UNSUPPORTED_SOURCE)
        driver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
