"""Bounded transports and byte-level image validation. Fully hermetic: no real network."""

import _test_bootstrap  # noqa: F401

import io
import struct
import unittest
import zlib
from unittest import mock

from PIL import Image
import requests

import chapter_source
from chapter_source import (
    CHALLENGE_REQUIRED, ChallengeRequired, INVALID_IMAGE_RESPONSE, SOURCE_ACCESS_DENIED,
    INCOMPLETE_DOWNLOAD, SOURCE_RATE_LIMITED, SourceError, GenericImageChapterAdapter,
    UniversalChapterAdapter, WEBTOONS,
)
from download_transport import (
    BrowserSessionTransport, CloudscraperTransport, DownloadLimits, LimitExceeded,
    RequestsTransport, build_transports, cloudscraper_transport_enabled,
    preflight_browser_navigation,
)
from image_validation import (
    DuplicateTracker, looks_like_markup, sniff_format, validate_image_bytes,
)

HOST = "cdn.test"
PAGE = f"https://{HOST}/series/x/chapter-1"


def public_dns(*_a, **_k):
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


def png_bytes(width=800, height=1200):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (12, 34, 56)).save(buf, "PNG")
    return buf.getvalue()


def png_header(width, height):
    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IEND", b""))


class _Response:
    def __init__(self, *, status=200, content=b"", content_type="image/png", headers=None):
        self.status_code = status
        self._content = content
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i:i + chunk_size]

    def close(self):
        self.closed = True


class _Session:
    """Records requests and replays scripted responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list[str] = []
        self.cookies = mock.MagicMock()
        self.max_redirects = 3

    def get(self, url, **_kwargs):
        self.requests.append(url)
        return self._responses.pop(0)

    def close(self):
        pass


def adapter():
    return GenericImageChapterAdapter(name="t", allowed_hosts=(HOST,))


class TransportTests(unittest.TestCase):
    def transport(self, responses, **kw):
        return RequestsTransport(adapter(), session=_Session(responses), **kw)

    def test_successful_fetch_returns_content(self):
        data = png_bytes()
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            result = self.transport([_Response(content=data)]).fetch(PAGE)
        self.assertEqual(result.content, data)
        self.assertEqual(result.status, 200)

    def test_referer_is_reduced_to_safe_origin(self):
        headers = self.transport([])._headers(
            "https://reader.test/chapter/1?signature=secret-token#fragment")
        self.assertEqual(headers["Referer"], "https://reader.test/")
        self.assertNotIn("secret-token", headers["Referer"])
        self.assertNotIn("?", headers["Referer"])

    def test_credentials_are_never_replayed_in_referer(self):
        headers = self.transport([])._headers("https://user:secret@reader.test/chapter/1")
        self.assertNotIn("Referer", headers)

    def test_owned_requests_session_does_not_inherit_environment_proxy_or_netrc(self):
        transport = RequestsTransport(adapter())
        self.addCleanup(transport.close)
        self.assertFalse(transport._session.trust_env)

    def test_browser_cookie_scope_preserves_www_path_and_secure_attribute(self):
        class Driver:
            @staticmethod
            def get_cookies():
                return [{"name": "reader", "value": "ok", "domain": "www.reader.test",
                         "path": "/chapter", "secure": True}]

        transport = BrowserSessionTransport(
            GenericImageChapterAdapter(name="reader", allowed_hosts=("reader.test",)),
            Driver(), "https://www.reader.test/chapter/1",
        )
        self.addCleanup(transport.close)
        cookie = next(cookie for cookie in transport._session.cookies if cookie.name == "reader")
        self.assertTrue(cookie.secure)
        self.assertEqual(cookie.path, "/chapter")
        secure_request = transport._session.prepare_request(
            requests.Request("GET", "https://www.reader.test/chapter/2"))
        insecure_request = transport._session.prepare_request(
            requests.Request("GET", "http://www.reader.test/chapter/2"))
        self.assertIn("reader=ok", secure_request.headers.get("Cookie", ""))
        self.assertNotIn("reader=ok", insecure_request.headers.get("Cookie", ""))

    def test_pixel_ceiling_rejects_compressed_image_bomb_before_full_decode(self):
        with self.assertRaises(SourceError) as ctx:
            validate_image_bytes(png_header(8000, 8000), min_bytes=12)
        self.assertEqual(ctx.exception.detail, "too_many_pixels")

    def test_redirect_is_followed_and_revalidated(self):
        data = png_bytes()
        session = _Session([
            _Response(status=302, headers={"Location": f"https://{HOST}/real.png"}),
            _Response(content=data),
        ])
        transport = RequestsTransport(adapter(), session=session)
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            result = transport.fetch(PAGE)
        self.assertEqual(result.content, data)
        self.assertEqual(len(session.requests), 2)

    def test_redirect_to_foreign_host_is_blocked(self):
        session = _Session([
            _Response(status=302, headers={"Location": "https://elsewhere.test/x.png"})])
        transport = RequestsTransport(adapter(), session=session)
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            with self.assertRaises(SourceError):
                transport.fetch(PAGE)

    def test_redirect_to_private_address_is_blocked(self):
        # The whole point: URL validation is worthless if a 302 can walk us inward.
        session = _Session([
            _Response(status=302, headers={"Location": "http://127.0.0.1:8080/x.png"})])
        transport = RequestsTransport(adapter(), session=session)
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            with self.assertRaises(SourceError):
                transport.fetch(PAGE)

    def test_redirect_loop_is_bounded(self):
        hops = [_Response(status=302, headers={"Location": f"https://{HOST}/next{i}.png"})
                for i in range(10)]
        transport = RequestsTransport(adapter(), session=_Session(hops),
                                      limits=DownloadLimits(max_redirects=2))
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            with self.assertRaises(LimitExceeded):
                transport.fetch(PAGE)

    def test_declared_oversize_is_refused_before_reading(self):
        transport = self.transport(
            [_Response(content=png_bytes(), headers={"Content-Length": str(99 * 1024 * 1024)})],
            limits=DownloadLimits(max_bytes_per_file=1024 * 1024))
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            with self.assertRaises(LimitExceeded) as ctx:
                transport.fetch(PAGE)
        self.assertEqual(ctx.exception.detail, "max_bytes_per_file")

    def test_undeclared_oversize_is_caught_while_streaming(self):
        # A server that lies about (or omits) Content-Length must still be bounded.
        transport = self.transport([_Response(content=b"x" * 500_000)],
                                   limits=DownloadLimits(max_bytes_per_file=1000))
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            with self.assertRaises(LimitExceeded):
                transport.fetch(PAGE)

    def test_403_is_access_denied(self):
        transport = self.transport([_Response(status=403, content=b"nope")])
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            with self.assertRaises(SourceError) as ctx:
                transport.fetch(PAGE)
        self.assertEqual(ctx.exception.code, SOURCE_ACCESS_DENIED)

    def test_429_is_rate_limited(self):
        transport = self.transport([_Response(status=429)])
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            with self.assertRaises(SourceError) as ctx:
                transport.fetch(PAGE)
        self.assertEqual(ctx.exception.code, SOURCE_RATE_LIMITED)

    def test_challenge_page_stops_honestly(self):
        body = b"<html><body>Checking your browser before accessing</body></html>"
        transport = self.transport([_Response(content=body, content_type="text/html")])
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            with self.assertRaises(ChallengeRequired) as ctx:
                transport.fetch(PAGE)
        self.assertEqual(ctx.exception.code, CHALLENGE_REQUIRED)

    def test_html_content_type_is_rejected(self):
        transport = self.transport(
            [_Response(content=b"<html>ordinary error</html>", content_type="text/html")])
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            with self.assertRaises(SourceError) as ctx:
                transport.fetch(PAGE)
        self.assertEqual(ctx.exception.code, INVALID_IMAGE_RESPONSE)

    def test_file_count_and_total_budget_are_shared(self):
        limits = DownloadLimits(max_files=2)
        transport = RequestsTransport(
            adapter(), session=_Session([_Response(content=png_bytes()) for _ in range(5)]),
            limits=limits)
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            transport.fetch(PAGE)
            transport.fetch(PAGE)
            with self.assertRaises(LimitExceeded) as ctx:
                transport.fetch(PAGE)
        self.assertEqual(ctx.exception.detail, "max_files")

    def test_total_byte_ceiling_is_terminal_during_streaming(self):
        data = png_bytes()
        transport = self.transport(
            [_Response(content=data), _Response(content=data)],
            limits=DownloadLimits(max_total_bytes=len(data) + 5, max_files=10),
        )
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            transport.fetch(PAGE)
            with self.assertRaises(LimitExceeded) as ctx:
                transport.fetch(PAGE)
        self.assertEqual(ctx.exception.code, INCOMPLETE_DOWNLOAD)
        self.assertEqual(ctx.exception.detail, "max_total_bytes")

    def test_duration_ceiling_is_checked_before_a_new_fetch(self):
        transport = self.transport(
            [_Response(content=png_bytes())], limits=DownloadLimits(max_duration_seconds=0.01))
        transport.budget.started -= 1.0
        with self.assertRaises(LimitExceeded) as ctx:
            transport.fetch(PAGE)
        self.assertEqual(ctx.exception.detail, "max_duration")

    def test_specific_adapter_explicitly_allows_its_resource_cdn(self):
        page = "https://webtoon-phinf.pstatic.net/chapter/001.webp"
        transport = RequestsTransport(WEBTOONS, session=_Session([_Response(content=png_bytes())]))
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            self.assertEqual(transport.fetch(page).status, 200)


class NavigationPreflightTests(unittest.TestCase):
    def test_each_navigation_redirect_is_checked_before_browser_use(self):
        session = _Session([
            _Response(status=302, headers={"Location": "/reader"}),
            _Response(status=200, content=b"<html>reader</html>", content_type="text/html"),
        ])
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            final = preflight_browser_navigation(adapter(), PAGE, session=session)
        self.assertEqual(final, f"https://{HOST}/reader")
        self.assertEqual(session.requests, [PAGE, final])

    def test_navigation_redirect_to_private_or_non_http_target_fails_closed(self):
        for location in ("http://127.0.0.1/admin", "file:///C:/secret.txt"):
            session = _Session([_Response(status=302, headers={"Location": location})])
            with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
                with self.assertRaises(SourceError):
                    preflight_browser_navigation(adapter(), PAGE, session=session)

    def test_universal_adapter_rejects_dns_rebinding_between_validations(self):
        universal = UniversalChapterAdapter("https://reader.example.test/chapter/1")
        answers = [public_dns(), [(2, 1, 6, "", ("93.184.216.35", 0))]]
        with mock.patch.object(chapter_source.socket, "getaddrinfo", side_effect=answers):
            universal.validate_url("https://reader.example.test/chapter/1")
            with self.assertRaises(SourceError) as ctx:
                universal.validate_url("https://reader.example.test/chapter/1")
        self.assertEqual(ctx.exception.detail, "dns_rebinding")


class BrowserSessionTests(unittest.TestCase):
    class _Driver:
        def __init__(self, cookies):
            self._cookies = cookies

        def get_cookies(self):
            return self._cookies

    def test_only_same_domain_cookies_are_adopted(self):
        driver = self._Driver([
            {"name": "session", "value": "abc", "domain": f".{HOST}"},
            {"name": "tracker", "value": "xyz", "domain": "ads.evil.test"},
        ])
        session = _Session([])
        BrowserSessionTransport(adapter(), driver, PAGE, session=session)
        names = [call.args[0] for call in session.cookies.set.call_args_list]
        self.assertIn("session", names)
        self.assertNotIn("tracker", names)

    def test_cookies_are_cleared_on_close(self):
        session = _Session([])
        transport = BrowserSessionTransport(
            adapter(), self._Driver([{"name": "s", "value": "v", "domain": HOST}]),
            PAGE, session=session)
        transport.close()
        session.cookies.clear.assert_called_once()

    def test_driver_without_cookies_is_not_fatal(self):
        class Broken:
            def get_cookies(self):
                raise RuntimeError("no session")

        BrowserSessionTransport(adapter(), Broken(), PAGE, session=_Session([]))

    def test_cookie_values_never_reach_the_transport_repr(self):
        session = _Session([])
        transport = BrowserSessionTransport(
            adapter(), self._Driver([{"name": "s", "value": "SECRET-VALUE", "domain": HOST}]),
            PAGE, session=session)
        self.assertNotIn("SECRET-VALUE", repr(transport))


class TransportSelectionTests(unittest.TestCase):
    def test_requests_first_then_browser_session(self):
        transports = build_transports(adapter(), driver=mock.MagicMock(), page_url=PAGE)
        self.assertEqual([t.name for t in transports], ["requests", "browser_session"])

    def test_cloudscraper_is_not_a_default_dependency(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parent / "download_transport.py").read_text(
            encoding="utf-8")
        self.assertNotIn("import cloudscraper", source)
        for banned in ("proxy_rotation", "stealth", "fingerprint", "captcha", "solver"):
            self.assertNotIn(banned, source.lower().replace("captcha solver", ""), banned)

    def test_optional_cloudscraper_requires_exact_feature_flag_and_shares_budget(self):
        factory = mock.Mock(return_value=_Session([]))
        self.assertFalse(cloudscraper_transport_enabled({}))
        self.assertFalse(cloudscraper_transport_enabled({"ENABLE_CLOUDSCRAPER_TRANSPORT": "true"}))
        self.assertTrue(cloudscraper_transport_enabled({"ENABLE_CLOUDSCRAPER_TRANSPORT": "1"}))
        disabled = build_transports(adapter(), driver=mock.MagicMock(), page_url=PAGE,
                                    enable_cloudscraper=False, cloudscraper_factory=factory)
        self.assertEqual([item.name for item in disabled], ["requests", "browser_session"])
        factory.assert_not_called()
        enabled = build_transports(adapter(), driver=mock.MagicMock(), page_url=PAGE,
                                   enable_cloudscraper=True, cloudscraper_factory=factory)
        self.assertEqual([item.name for item in enabled], [
            "requests", "browser_session", "cloudscraper",
        ])
        self.assertIsInstance(enabled[-1], CloudscraperTransport)
        self.assertIs(enabled[0].budget, enabled[-1].budget)
        factory.assert_called_once_with()

    def test_optional_cloudscraper_unavailable_fails_closed_when_explicitly_enabled(self):
        with mock.patch("download_transport.importlib.import_module", side_effect=ImportError):
            with self.assertRaises(SourceError) as ctx:
                build_transports(adapter(), enable_cloudscraper=True)
        self.assertEqual(ctx.exception.detail, "cloudscraper_unavailable")

    def test_budget_is_shared_between_transports(self):
        transports = build_transports(adapter(), driver=mock.MagicMock(), page_url=PAGE)
        self.assertIs(transports[0].budget, transports[1].budget)


class ImageValidationTests(unittest.TestCase):
    def test_real_png_accepted(self):
        image = validate_image_bytes(png_bytes())
        self.assertEqual((image.fmt, image.width, image.height), ("png", 800, 1200))
        self.assertEqual(len(image.sha256), 64)

    def test_signature_sniffing(self):
        self.assertEqual(sniff_format(png_bytes()), "png")
        self.assertEqual(sniff_format(b"RIFF" + b"0000" + b"WEBP" + b"x" * 32), "webp")
        self.assertEqual(sniff_format(b"\x00\x00\x00 ftypavif" + b"x" * 32), "avif")
        self.assertEqual(sniff_format(b"nonsense-bytes-here"), "")

    def test_html_disguised_as_image_rejected(self):
        for payload in (b"<!DOCTYPE html><html>403</html>" + b"x" * 2000,
                        b"<html><body>error</body></html>" + b"y" * 2000,
                        b'{"error":"denied"}' + b" " * 2000):
            with self.assertRaises(SourceError) as ctx:
                validate_image_bytes(payload)
            self.assertEqual(ctx.exception.detail, "markup_not_image")

    def test_markup_detection_ignores_leading_whitespace(self):
        self.assertTrue(looks_like_markup(b"\n\n   <!DOCTYPE html>"))

    def test_truncated_image_rejected(self):
        with self.assertRaises(SourceError) as ctx:
            validate_image_bytes(png_bytes()[:1500])
        self.assertTrue(ctx.exception.detail.startswith("decode:"))

    def test_valid_signature_but_undecodable_rejected(self):
        with self.assertRaises(SourceError):
            validate_image_bytes(b"\xff\xd8\xff" + b"\x00" * 4000)

    def test_too_small_dimensions_rejected(self):
        with self.assertRaises(SourceError) as ctx:
            validate_image_bytes(png_bytes(20, 20))
        self.assertTrue(ctx.exception.detail.startswith(("dimensions", "too_small")))

    def test_empty_and_oversize_rejected(self):
        with self.assertRaises(SourceError):
            validate_image_bytes(b"")
        with self.assertRaises(SourceError) as ctx:
            validate_image_bytes(png_bytes(), max_bytes=10)
        self.assertEqual(ctx.exception.detail, "too_large")

    def test_error_details_never_leak_bytes_or_urls(self):
        try:
            validate_image_bytes(b"<html>https://secret.example/token=abc</html>" + b"x" * 2000)
        except SourceError as exc:
            self.assertNotIn("secret.example", str(exc))
            self.assertNotIn("token", str(exc))


class DuplicateTests(unittest.TestCase):
    def test_identical_bytes_detected_order_preserved(self):
        tracker = DuplicateTracker()
        first = validate_image_bytes(png_bytes())
        second = validate_image_bytes(png_bytes())        # byte-identical
        third = validate_image_bytes(png_bytes(801, 1200))
        self.assertFalse(tracker.is_duplicate(first))
        self.assertTrue(tracker.is_duplicate(second))
        self.assertFalse(tracker.is_duplicate(third))
        self.assertEqual(len(tracker), 2)
        self.assertEqual(tracker.duplicates, 1)


if __name__ == "__main__":
    unittest.main()
