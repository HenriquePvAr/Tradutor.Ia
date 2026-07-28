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
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Protocol
from urllib.parse import urlparse

import requests

from chapter_source import (
    ALLOWED_IMAGE_MIME, INCOMPLETE_DOWNLOAD, INVALID_IMAGE_RESPONSE, SOURCE_ACCESS_DENIED,
    SOURCE_ANALYSIS_FAILED, SOURCE_NAVIGATION_TIMEOUT, SOURCE_NOT_READY, SOURCE_RATE_LIMITED,
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
    preflight_transport_attempts: int = 2

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


@dataclass(frozen=True)
class SourcePreflightResult:
    schema_version: int
    normalized_url_hash: str
    adapter: str
    status: str
    reason_code: str
    http_method: str
    http_status: int | None
    content_type: str
    redirect_count: int
    final_host: str
    response_size_class: str
    html_shell_detected: bool
    javascript_required_possible: bool
    browser_inspection_allowed: bool
    browser_inspection_reason: str
    authentication_required: bool
    access_restricted: bool
    captcha_detected: bool
    security_blocked: bool
    transport_error: str
    elapsed_ms: int
    policy_hash: str
    navigation_url: str

    def public(self) -> dict[str, Any]:
        value = {
            key: getattr(self, key)
            for key in self.__dataclass_fields__
            if key != "navigation_url"
        }
        return value


def _preflight_policy_hash(adapter) -> str:
    capabilities = getattr(adapter, "capabilities", None)
    public = capabilities.public() if hasattr(capabilities, "public") else {}
    encoded = json.dumps(
        {"schema_version": 1, "capabilities": public, "max_redirects": 3},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _response_size_class(response) -> str:
    try:
        size = int(response.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        return "unknown"
    if size <= 16 * 1024:
        return "small"
    if size <= 256 * 1024:
        return "medium"
    return "large"


def _preflight_result(adapter, url: str, *, status: str, reason_code: str,
                      started: float, http_status: int | None = None,
                      content_type: str = "", redirect_count: int = 0,
                      response_size_class: str = "unknown",
                      html_shell_detected: bool = False,
                      javascript_required_possible: bool = False,
                      browser_inspection_allowed: bool = False,
                      browser_inspection_reason: str = "",
                      authentication_required: bool = False,
                      access_restricted: bool = False,
                      captcha_detected: bool = False,
                      security_blocked: bool = False,
                      transport_error: str = "") -> SourcePreflightResult:
    normalized = str(url or "")
    return SourcePreflightResult(
        schema_version=1,
        normalized_url_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        adapter=str(getattr(adapter, "name", "") or ""),
        status=status,
        reason_code=reason_code,
        http_method="GET",
        http_status=http_status,
        content_type=content_type,
        redirect_count=redirect_count,
        final_host=host_of(normalized),
        response_size_class=response_size_class,
        html_shell_detected=html_shell_detected,
        javascript_required_possible=javascript_required_possible,
        browser_inspection_allowed=browser_inspection_allowed,
        browser_inspection_reason=browser_inspection_reason,
        authentication_required=authentication_required,
        access_restricted=access_restricted,
        captcha_detected=captcha_detected,
        security_blocked=security_blocked,
        transport_error=transport_error,
        elapsed_ms=max(0, int((time.perf_counter() - started) * 1000)),
        policy_hash=_preflight_policy_hash(adapter),
        navigation_url=normalized,
    )


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
        self.last_diagnostic_context: dict[str, Any] = {}

    def _mark(
        self,
        operation: str,
        *,
        before_request: bool,
        request_started: bool = False,
        response_received: bool = False,
    ) -> None:
        self.last_diagnostic_context = {
            "phase": "transport",
            "operation": operation,
            "before_request": before_request,
            "request_started": request_started,
            "response_received": response_received,
        }

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
        self._mark("budget", before_request=True)
        self.budget.begin_file()
        self._mark("validate_hop", before_request=True)
        self._validate_hop(url)
        self._mark("build_headers", before_request=True)
        headers = self._headers(referer)
        current = url
        for _ in range(self.limits.max_redirects + 1):
            self._mark("request", before_request=False, request_started=True)
            response = self._session.get(
                current, headers=headers, timeout=self.limits.timeout,
                stream=True, allow_redirects=False)
            self._mark(
                "response", before_request=False, request_started=True,
                response_received=True)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location") or ""
                response.close()
                if not location:
                    raise SourceError(INVALID_IMAGE_RESPONSE, "redirect_without_location")
                current = requests.compat.urljoin(current, location)
                self._mark(
                    "redirect_validation", before_request=False, request_started=True,
                    response_received=True)
                self._validate_hop(current)          # a 302 must not walk us inward
                continue
            self._mark(
                "finish_response", before_request=False, request_started=True,
                response_received=True)
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


def inspect_source_preflight(adapter, url: str, *, limits: DownloadLimits | None = None,
                             session: Any = None, cancel_check=None) -> SourcePreflightResult:
    """Return a sanitized, deterministic decision before browser navigation."""
    limits = limits or DownloadLimits()
    own_session = session is None
    session = session or requests.Session()
    if own_session and hasattr(session, "trust_env"):
        session.trust_env = False
    current = str(url or "")
    started = time.perf_counter()
    redirect_count = 0
    try:
        for _ in range(limits.max_redirects + 1):
            adapter.validate_navigation_url(current)
            # Specific readers may only enter a chapter-shaped path.  Reject a redirect to a
            # series, account or home page before Selenium is ever pointed at it; generic
            # adapters deliberately have no path markers and remain unaffected.
            adapter.validate_path(current)
            request_args = {
                "headers": {"User-Agent": DEFAULT_USER_AGENT,
                            "Accept": "text/html,application/xhtml+xml"},
                "timeout": limits.timeout,
                "stream": True,
                "allow_redirects": False,
            }
            response = None
            last_transport_error = ""
            for attempt in range(max(1, int(limits.preflight_transport_attempts))):
                try:
                    if not cancel_check:
                        response = session.get(current, **request_args)
                    else:
                        result: dict[str, Any] = {}

                        def fetch() -> None:
                            try:
                                result["response"] = session.get(current, **request_args)
                            except BaseException as exc:  # noqa: BLE001 - re-raised below
                                result["error"] = exc

                        request_thread = threading.Thread(target=fetch, daemon=True)
                        request_thread.start()
                        while request_thread.is_alive():
                            if cancel_check():
                                try:
                                    session.close()
                                except Exception:  # noqa: BLE001
                                    pass
                                request_thread.join(timeout=1.0)
                                raise SourceError(
                                    "cancelled", "during_navigation_preflight")
                            request_thread.join(timeout=0.1)
                        if "error" in result:
                            raise result["error"]
                        response = result.get("response")
                    if response is None:
                        raise requests.ConnectionError("empty preflight response")
                    break
                except SourceError:
                    raise
                except requests.Timeout:
                    last_transport_error = "timeout"
                except requests.ConnectionError:
                    last_transport_error = "connection"
                except Exception:  # noqa: BLE001 - never persist transport internals
                    last_transport_error = "unexpected"
                if attempt + 1 < max(1, int(limits.preflight_transport_attempts)):
                    continue
            if response is None:
                if last_transport_error == "timeout":
                    return _preflight_result(
                        adapter, current, status="source_navigation_timeout",
                        reason_code=SOURCE_NAVIGATION_TIMEOUT, started=started,
                        redirect_count=redirect_count, transport_error="timeout")
                reason = (
                    "source_transport_failed"
                    if last_transport_error == "connection"
                    else SOURCE_ANALYSIS_FAILED
                )
                return _preflight_result(
                    adapter, current, status=reason, reason_code=reason,
                    started=started, redirect_count=redirect_count,
                    transport_error=last_transport_error or "unexpected")
            try:
                status = int(response.status_code)
                content_type = str(
                    response.headers.get("Content-Type") or ""
                ).split(";", 1)[0].strip().casefold()
                if status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location") or ""
                    if not location:
                        return _preflight_result(
                            adapter, current, status="source_redirect_blocked",
                            reason_code="source_redirect_blocked", started=started,
                            http_status=status, redirect_count=redirect_count,
                            security_blocked=True)
                    current = requests.compat.urljoin(current, location)
                    adapter.validate_navigation_url(current)
                    adapter.validate_path(current)
                    redirect_count += 1
                    continue
                body = RequestsTransport._peek(response)
                captcha = looks_like_challenge(body)
                if captcha:
                    return _preflight_result(
                        adapter, current, status="source_captcha_detected",
                        reason_code=CHALLENGE_REQUIRED, started=started,
                        http_status=status, content_type=content_type,
                        redirect_count=redirect_count, captcha_detected=True,
                        security_blocked=True)
                capabilities = getattr(adapter, "capabilities", None)
                browser_capable = bool(
                    getattr(capabilities, "supports_browser_inspection", False))
                if status in (401, 403):
                    if (getattr(adapter, "browser_owned_readiness", False)
                            and browser_capable):
                        return _preflight_result(
                            adapter, current, status="browser_inspection_required",
                            reason_code="browser_inspection_required", started=started,
                            http_status=status, content_type=content_type,
                            redirect_count=redirect_count,
                            response_size_class=_response_size_class(response),
                            javascript_required_possible=True,
                            browser_inspection_allowed=True,
                            browser_inspection_reason="browser_owned_readiness")
                    return _preflight_result(
                        adapter, current, status="source_access_restricted",
                        reason_code=SOURCE_ACCESS_DENIED, started=started,
                        http_status=status, content_type=content_type,
                        redirect_count=redirect_count, access_restricted=True,
                        security_blocked=True)
                if status in (429, 503):
                    return _preflight_result(
                        adapter, current, status="source_unavailable",
                        reason_code=SOURCE_RATE_LIMITED, started=started,
                        http_status=status, content_type=content_type,
                        redirect_count=redirect_count)
                if status >= 400:
                    return _preflight_result(
                        adapter, current, status="source_unavailable",
                        reason_code="source_unavailable", started=started,
                        http_status=status, content_type=content_type,
                        redirect_count=redirect_count)
                if content_type and content_type not in {
                    "text/html", "application/xhtml+xml"
                }:
                    return _preflight_result(
                        adapter, current, status="source_content_type_unsupported",
                        reason_code="source_content_type_unsupported", started=started,
                        http_status=status, content_type=content_type,
                        redirect_count=redirect_count, security_blocked=True)
                html_shell = bool(
                    body and len(body.strip()) < 4096
                    and any(marker in body.casefold() for marker in (
                        "<script", "id=\"root\"", "id=\"app\"", "__next_data__"
                    ))
                )
                requires_dom = bool(
                    getattr(capabilities, "requires_rendered_dom", False))
                if browser_capable and (requires_dom or html_shell):
                    return _preflight_result(
                        adapter, current, status="browser_inspection_required",
                        reason_code="browser_inspection_required", started=started,
                        http_status=status, content_type=content_type,
                        redirect_count=redirect_count,
                        response_size_class=_response_size_class(response),
                        html_shell_detected=html_shell,
                        javascript_required_possible=requires_dom or html_shell,
                        browser_inspection_allowed=True,
                        browser_inspection_reason=(
                            "requires_rendered_dom" if requires_dom else "html_shell"))
                return _preflight_result(
                    adapter, current, status="preflight_ready",
                    reason_code="preflight_ready", started=started,
                    http_status=status, content_type=content_type,
                    redirect_count=redirect_count,
                    response_size_class=_response_size_class(response),
                    browser_inspection_allowed=browser_capable,
                    browser_inspection_reason=(
                        "adapter_browser_capable" if browser_capable else "not_required"))
            finally:
                response.close()
        return _preflight_result(
            adapter, current, status="source_redirect_blocked",
            reason_code="source_redirect_blocked", started=started,
            redirect_count=redirect_count, security_blocked=True)
    finally:
        if own_session:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass


def require_browser_navigation(result: SourcePreflightResult) -> str:
    """Return the validated navigation URL or raise the result's sanitized failure."""
    if result.status in {"preflight_ready", "browser_inspection_required"}:
        return result.navigation_url
    detail = {
        "source_navigation_timeout": "navigation_preflight_timeout",
        "source_transport_failed": "navigation_preflight_connection",
        "source_redirect_blocked": "navigation_preflight_redirect",
        "source_content_type_unsupported": "navigation_preflight_content_type",
        "source_unavailable": "navigation_preflight_http",
    }.get(result.status, "navigation_preflight")
    error = SourceError(result.reason_code or SOURCE_NOT_READY, detail)
    # Public() deliberately excludes the navigation URL, headers and response body.
    # Carry this sanitized diagnosis to the worker so a terminal job does not collapse
    # an HTTP/preflight decision into an untraceable generic message.
    error.preflight_result = result.public()
    raise error


def preflight_browser_navigation(adapter, url: str, *, limits: DownloadLimits | None = None,
                                 session: Any = None, cancel_check=None) -> str:
    """Compatibility boundary returning a safe browser URL or a coded failure."""
    result = inspect_source_preflight(
        adapter, url, limits=limits, session=session, cancel_check=cancel_check)
    return require_browser_navigation(result)
