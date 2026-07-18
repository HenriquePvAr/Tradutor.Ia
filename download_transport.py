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

import time
from dataclasses import dataclass
from typing import Any, Iterable, Protocol
from urllib.parse import urlparse

import requests

from chapter_source import (
    ALLOWED_IMAGE_MIME, INVALID_IMAGE_RESPONSE, SOURCE_ACCESS_DENIED, SOURCE_RATE_LIMITED,
    SourceError, host_of, looks_like_challenge,
)
from chapter_source import ChallengeRequired

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
)


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
        super().__init__(INVALID_IMAGE_RESPONSE, detail)


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

    def account(self, size: int) -> None:
        self.files += 1
        self.total_bytes += size
        if self.total_bytes > self.limits.max_total_bytes:
            raise LimitExceeded("max_total_bytes")


class RequestsTransport:
    """Plain HTTP fetch with every hop revalidated by the adapter."""

    name = "requests"

    def __init__(self, adapter, *, limits: DownloadLimits | None = None,
                 budget: _BudgetTracker | None = None, session: Any = None):
        self._adapter = adapter
        self.limits = limits or DownloadLimits()
        self.budget = budget or _BudgetTracker(self.limits)
        self._session = session or requests.Session()
        # Redirects are followed manually so each hop can be revalidated.
        self._session.max_redirects = self.limits.max_redirects

    def _headers(self, referer: str) -> dict[str, str]:
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        }
        if referer:
            headers["Referer"] = referer
        return headers

    def _validate_hop(self, url: str) -> None:
        """Every hop must still satisfy the adapter's host and network rules."""
        self._adapter.validate_url(url)

    def fetch(self, url: str, *, referer: str = "") -> FetchResult:
        self.budget.check_before()
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
        if declared and int(declared) > self.limits.max_bytes_per_file:
            response.close()
            raise LimitExceeded("max_bytes_per_file")

        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            size += len(chunk)
            if size > self.limits.max_bytes_per_file:
                response.close()
                raise LimitExceeded("max_bytes_per_file")
            chunks.append(chunk)
        response.close()
        content = b"".join(chunks)

        if content_type and content_type not in ALLOWED_IMAGE_MIME:
            # A challenge page served as 200 is a common shape; report it honestly.
            if looks_like_challenge(content[:4096].decode("utf-8", "ignore")):
                raise ChallengeRequired("challenge_body")
            raise SourceError(INVALID_IMAGE_RESPONSE, f"content_type:{content_type}")

        self.budget.account(size)
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
        self._domain = host_of(page_url)
        self._adopt_cookies(driver)

    def _adopt_cookies(self, driver: Any) -> None:
        try:
            cookies: Iterable[dict] = driver.get_cookies() or []
        except Exception:  # noqa: BLE001 - a driver without cookies is not fatal
            return
        for cookie in cookies:
            name, value = cookie.get("name"), cookie.get("value")
            domain = str(cookie.get("domain") or "").lstrip(".").lower()
            if not name or value is None:
                continue
            # Scope: only cookies belonging to the page's own domain travel with us.
            if domain and not (domain == self._domain or self._domain.endswith(f".{domain}")):
                continue
            self._session.cookies.set(name, value, domain=domain or self._domain)

    def close(self) -> None:
        try:
            self._session.cookies.clear()      # cookies never outlive the run
        finally:
            super().close()


def build_transports(adapter, *, driver=None, page_url: str = "",
                     limits: DownloadLimits | None = None) -> list[DownloadTransport]:
    """Ordered transports to try. Cloudscraper is deliberately absent.

    It is an optional, opt-in seam rather than a dependency: pulling it in by default would
    ship an anti-bot workaround to every user, and a site that needs one is a site asking us
    not to fetch it. An interactive challenge ends the job with challenge_required.
    """
    limits = limits or DownloadLimits()
    budget = _BudgetTracker(limits)
    transports: list[DownloadTransport] = [
        RequestsTransport(adapter, limits=limits, budget=budget)]
    if driver is not None and page_url:
        transports.append(
            BrowserSessionTransport(adapter, driver, page_url, limits=limits, budget=budget))
    return transports
