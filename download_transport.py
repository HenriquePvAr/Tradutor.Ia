"""Bounded transports for fetching chapter images.

The downloader used a bare ``requests.get(url, timeout=20)``: no size ceiling, no
Content-Type check, and — the real hole — no revalidation of redirects. Validating the
submitted URL is worthless if a 302 can then walk the fetch to an internal address, so every
hop is re-checked against the same adapter that authorized the original host.

Cookies handed over from a browser session live in memory only, scoped to the origin
domain, and are dropped when the transport closes. They are never logged and never
persisted.
"""

from __future__ import annotations

import importlib
import os
import time
from dataclasses import dataclass
from typing import Any, Iterable, Protocol
from urllib.parse import urlparse

import requests

from chapter_source import (
    ALLOWED_IMAGE_MIME, INCOMPLETE_DOWNLOAD, INVALID_IMAGE_RESPONSE, SOURCE_ACCESS_DENIED,
    SOURCE_NOT_READY, SOURCE_RATE_LIMITED,
    SourceError, host_of, raw_host_of, looks_like_challenge,
)
from chapter_source import ChallengeRequired

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
)


def _safe_referer(value: str) -> str:
    """Reduce a reader URL to an HTTP(S) origin before sending it to a resource host."""
    try:
        parsed = urlparse(str(value or "").strip())
        scheme = parsed.scheme.casefold()
        host = parsed.hostname or ""
        if scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            return ""
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    default_port = 80 if scheme == "http" else 443
    suffix = f":{port}" if port and port != default_port else ""
    return f"{scheme}://{display_host}{suffix}/"


@dataclass(frozen=True)
class DownloadLimits:
    """Ceilings that keep one URL from turning into an unbounded fetch."""

    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    max_redirects: int = 3
    max_bytes_per_file: int = 32 * 1024 * 1024        # 32 MiB
    max_total_bytes: int = 1024 * 1024 * 1024         # 1 GiB per chapter
    max_files: int = 400
    max_duration_seconds: float = 900.0

    @property
    def timeout(self) -> tuple[float, float]:
        return (self.connect_timeout, self.read_timeout)


class LimitExceeded(SourceError):
    def __init__(self, detail: str):
        # A resource ceiling is a terminal chapter condition, not a bad candidate that can
        # be retried through another transport.
        super().__init__(INCOMPLETE_DOWNLOAD, detail)


@dataclass
class FetchResult:
    content: bytes
    content_type: str
    final_url: str
    status: int


class DownloadTransport(Protocol):
    name: str

    def fetch(self, url: str, *, referer: str = "") -> FetchResult: ...
    def close(self) -> None: ...


class _BudgetTracker:
    """Shared across a chapter so per-file limits cannot be dodged by file count."""

    def __init__(self, limits: DownloadLimits):
        self.limits = limits
        self.total_bytes = 0
        self.files = 0
        self.started = time.monotonic()

    def check_before(self) -> None:
        if self.files >= self.limits.max_files:
            raise LimitExceeded("max_files")
        if time.monotonic() - self.started > self.limits.max_duration_seconds:
            raise LimitExceeded("max_duration")

    def begin_file(self) -> None:
        self.check_before()
        self.files += 1

    def reserve_bytes(self, size: int) -> None:
        if time.monotonic() - self.started > self.limits.max_duration_seconds:
            raise LimitExceeded("max_duration")
        if size < 0 or self.total_bytes + size > self.limits.max_total_bytes:
            raise LimitExceeded("max_total_bytes")
        self.total_bytes += size

    def account(self, size: int) -> None:
        # Kept as a compatibility seam for callers/tests that account an already-read file.
        self.reserve_bytes(size)


class RequestsTransport:
    """Plain HTTP fetch with every hop revalidated by the adapter."""

    name = "requests"

    def __init__(self, adapter, *, limits: DownloadLimits | None = None,
                 budget: _BudgetTracker | None = None, session: Any = None):
        self._adapter = adapter
        self.limits = limits or DownloadLimits()
        self.budget = budget or _BudgetTracker(self.limits)
        self._session = session or requests.Session()
        # Never inherit proxy/netrc settings for a transport-owned request: the connection
        # must obey this adapter's URL and redirect validation rather than ambient config.
        if session is None and hasattr(self._session, "trust_env"):
            self._session.trust_env = False
        # Redirects are followed manually so each hop can be revalidated.
        self._session.max_redirects = self.limits.max_redirects

    def _headers(self, referer: str) -> dict[str, str]:
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        }
        safe_referer = _safe_referer(referer)
        if safe_referer:
            headers["Referer"] = safe_referer
        return headers

    def _validate_hop(self, url: str) -> None:
        """Every hop must still satisfy the adapter's host and network rules."""
        self._adapter.validate_url(url)

    def reserve_local_content(self, size: int) -> None:
        """Account an in-memory canvas page against this chapter's shared budget."""
        if size < 0 or size > self.limits.max_bytes_per_file:
            raise LimitExceeded("max_bytes_per_file")
        self.budget.begin_file()
        self.budget.reserve_bytes(size)

    def fetch(self, url: str, *, referer: str = "") -> FetchResult:
        self.budget.begin_file()
        self._validate_hop(url)
        current = url
        for _ in range(self.limits.max_redirects + 1):
            response = self._session.get(
                current, headers=self._headers(referer), timeout=self.limits.timeout,
                stream=True, allow_redirects=False)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location") or ""
                response.close()
                if not location:
                    raise SourceError(INVALID_IMAGE_RESPONSE, "redirect_without_location")
                current = requests.compat.urljoin(current, location)
                self._validate_hop(current)          # a 302 must not walk us inward
                continue
            return self._finish(response, current)
        raise LimitExceeded("max_redirects")

    def _finish(self, response: Any, final_url: str) -> FetchResult:
        status = int(response.status_code)
        if status in (401, 403):
            body = self._peek(response)
            response.close()
            if looks_like_challenge(body):
                raise ChallengeRequired("http_%d" % status)
            raise SourceError(SOURCE_ACCESS_DENIED, str(status))
        if status in (429, 503):
            response.close()
            raise SourceError(SOURCE_RATE_LIMITED, str(status))
        if status != 200:
            response.close()
            raise SourceError(INVALID_IMAGE_RESPONSE, f"status_{status}")

        content_type = str(response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        declared = response.headers.get("Content-Length")
        if declared:
            try:
                declared_size = int(declared)
            except (TypeError, ValueError):
                response.close()
                raise SourceError(INVALID_IMAGE_RESPONSE, "invalid_content_length")
            if declared_size < 0 or declared_size > self.limits.max_bytes_per_file:
                response.close()
                raise LimitExceeded("max_bytes_per_file")
            if self.budget.total_bytes + declared_size > self.limits.max_total_bytes:
                response.close()
                raise LimitExceeded("max_total_bytes")

        # Trust neither filename nor server type.  A declared non-image has no reason to be
        # streamed in full; at most a bounded peek is used to classify an interactive wall.
        if content_type and content_type not in ALLOWED_IMAGE_MIME:
            body = self._peek(response)
            response.close()
            if looks_like_challenge(body):
                raise ChallengeRequired("challenge_body")
            raise SourceError(INVALID_IMAGE_RESPONSE, f"content_type:{content_type}")

        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            size += len(chunk)
            if size > self.limits.max_bytes_per_file:
                response.close()
                raise LimitExceeded("max_bytes_per_file")
            self.budget.reserve_bytes(len(chunk))
            chunks.append(chunk)
        response.close()
        content = b"".join(chunks)

        return FetchResult(content=content, content_type=content_type,
                           final_url=final_url, status=status)

    @staticmethod
    def _peek(response: Any) -> str:
        try:
            return next(response.iter_content(chunk_size=4096), b"").decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001 - peeking is best effort
            return ""

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            pass


class BrowserSessionTransport(RequestsTransport):
    """RequestsTransport carrying the browser's cookies for the origin domain only.

    Some readers only serve images to a session that already loaded the page. Copying that
    session is legitimate; the cookies stay in memory, never touch the log or the database,
    and are cleared on close.
    """

    name = "browser_session"

    def __init__(self, adapter, driver, page_url: str, *,
                 limits: DownloadLimits | None = None, budget: _BudgetTracker | None = None,
                 session: Any = None):
        super().__init__(adapter, limits=limits, budget=budget, session=session)
        # Cookie scope follows the literal reader hostname, not an adapter's ``www`` alias.
        self._domain = raw_host_of(page_url)
        self._adopt_cookies(driver)

    def _adopt_cookies(self, driver: Any) -> None:
        try:
            cookies: Iterable[dict] = driver.get_cookies() or []
            iter(cookies)
        except Exception:  # noqa: BLE001 - a driver without cookies is not fatal
            return
        for cookie in cookies:
            name, value = cookie.get("name"), cookie.get("value")
            domain = str(cookie.get("domain") or "").lstrip(".").lower()
            if not name or value is None:
                continue
            # Scope: only cookies set for the literal reader host travel with this fallback.
            # A parent-domain cookie could otherwise be replayed by Requests to an authorised
            # CDN sibling, even though the browser analysis never needed to disclose it there.
            if domain and domain != self._domain:
                continue
            self._session.cookies.set(
                name, value, domain=domain or self._domain,
                path=str(cookie.get("path") or "/"), secure=bool(cookie.get("secure")),
            )

    def close(self) -> None:
        try:
            self._session.cookies.clear()      # cookies never outlive the run
        finally:
            super().close()


def cloudscraper_transport_enabled(environ: dict[str, str] | None = None) -> bool:
    """Return true only for the explicit, documented feature-flag value ``"1"``.

    The transport is intentionally opt-in.  An unset flag, a typo, or a truthy-looking value
    such as ``true`` cannot change the normal requests/browser-session ordering.
    """
    values = os.environ if environ is None else environ
    return str(values.get("ENABLE_CLOUDSCRAPER_TRANSPORT") or "") == "1"


class CloudscraperTransport(RequestsTransport):
    """Optional requests-compatible transport with the same validation/budget boundary.

    It is constructed only after an explicit feature flag.  The optional package is imported
    lazily so standard tests and normal chapter runs neither require nor initialize it.  It
    receives no special connection settings; all redirects, byte ceilings and challenge
    detection remain inherited from :class:`RequestsTransport`.
    """

    name = "cloudscraper"

    def __init__(self, adapter, *, limits: DownloadLimits | None = None,
                 budget: _BudgetTracker | None = None, session: Any = None,
                 session_factory: Any = None):
        if session is None:
            factory = session_factory
            if factory is None:
                try:
                    module = importlib.import_module("cloudscraper")
                    factory = getattr(module, "create_scraper")
                except (ImportError, AttributeError) as exc:
                    raise SourceError(SOURCE_NOT_READY, "cloudscraper_unavailable") from exc
            try:
                session = factory()
            except Exception as exc:  # noqa: BLE001 - do not leak optional-package diagnostics
                raise SourceError(SOURCE_NOT_READY, "cloudscraper_unavailable") from exc
        super().__init__(adapter, limits=limits, budget=budget, session=session)
        # The optional session must not silently inherit an ambient proxy/netrc route either.
        # Its actual socket destination is still subject to the same adapter validation.
        if hasattr(self._session, "trust_env"):
            self._session.trust_env = False


def build_transports(adapter, *, driver=None, page_url: str = "",
                     limits: DownloadLimits | None = None,
                     enable_cloudscraper: bool | None = None,
                     cloudscraper_factory: Any = None) -> list[DownloadTransport]:
    """Return the deterministic requests → browser → optional-cloudscraper order.

    The third transport appears only when ``ENABLE_CLOUDSCRAPER_TRANSPORT=1`` (or the
    explicit test seam says so).  It has the same budget instance and never bypasses the
    redirect/DNS/byte validation enforced by its base class.
    """
    limits = limits or DownloadLimits()
    budget = _BudgetTracker(limits)
    transports: list[DownloadTransport] = [
        RequestsTransport(adapter, limits=limits, budget=budget)]
    if driver is not None and page_url:
        transports.append(
            BrowserSessionTransport(adapter, driver, page_url, limits=limits, budget=budget))
    if enable_cloudscraper is None:
        enable_cloudscraper = cloudscraper_transport_enabled()
    if enable_cloudscraper:
        transports.append(CloudscraperTransport(
            adapter, limits=limits, budget=budget, session_factory=cloudscraper_factory))
    return transports


def reserve_local_content(transports: Iterable[DownloadTransport] | None, size: int) -> None:
    """Charge a canvas capture to the single shared chapter budget exactly once."""
    for transport in transports or ():
        reserve = getattr(transport, "reserve_local_content", None)
        if callable(reserve):
            reserve(int(size))
            return


def preflight_browser_navigation(adapter, url: str, *, limits: DownloadLimits | None = None,
                                 session: Any = None) -> str:
    """Resolve and revalidate each top-level HTTP redirect before Chrome navigates.

    Selenium exposes a final URL only after it has followed redirects.  A bounded ordinary
    HTTP preflight gives the adapter an opportunity to reject a private, credentialed or
    unrelated hop *before* a browser is pointed at it.  It has no cookies, never follows a
    redirect implicitly and persists no response data.  A preflight failure is fail-closed.
    """
    limits = limits or DownloadLimits()
    own_session = session is None
    session = session or requests.Session()
    if own_session and hasattr(session, "trust_env"):
        session.trust_env = False
    current = str(url or "")
    try:
        for _ in range(limits.max_redirects + 1):
            adapter.validate_navigation_url(current)
            # Specific readers may only enter a chapter-shaped path.  Reject a redirect to a
            # series, account or home page before Selenium is ever pointed at it; generic
            # adapters deliberately have no path markers and remain unaffected.
            adapter.validate_path(current)
            try:
                response = session.get(
                    current,
                    headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
                    timeout=limits.timeout,
                    stream=True,
                    allow_redirects=False,
                )
            except Exception as exc:  # noqa: BLE001 - do not leak network internals to UI
                raise SourceError(SOURCE_NOT_READY, "navigation_preflight") from exc
            try:
                status = int(response.status_code)
                if status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location") or ""
                    if not location:
                        raise SourceError(SOURCE_NOT_READY, "redirect_without_location")
                    current = requests.compat.urljoin(current, location)
                    adapter.validate_navigation_url(current)
                    adapter.validate_path(current)
                    continue
                if status in (401, 403):
                    if looks_like_challenge(RequestsTransport._peek(response)):
                        raise ChallengeRequired(f"http_{status}")
                    raise SourceError(SOURCE_ACCESS_DENIED, str(status))
                if status in (429, 503):
                    raise SourceError(SOURCE_RATE_LIMITED, str(status))
                if status >= 400:
                    raise SourceError(SOURCE_NOT_READY, f"http_{status}")
                return current
            finally:
                response.close()
        raise SourceError(SOURCE_NOT_READY, "max_navigation_redirects")
    finally:
        if own_session:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass
