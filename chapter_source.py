"""Explicit per-source adapter selection and validation for chapter URLs.

Every source owns its own reader knowledge — selectors, readiness, candidate classification
and exclusion — so the generic downloader holds no site-specific rules. Selection is
fail-closed: a host is either claimed by a registered adapter or rejected with
``unsupported_source`` before any job exists.

There is deliberately no universal fallback. Fetching from a host nobody registered is an
SSRF-shaped risk, and "try it and see" is exactly how a downloader ends up pointed at an
internal address or at a site the operator has no right to read. Registering a source is an
explicit act that asserts both support and permission.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse, urlunparse

# Stable, sanitized failure codes. The UI maps these to messages; a job must always end on
# one of these rather than sitting in queued/running forever.
UNSUPPORTED_SOURCE = "unsupported_source"
SOURCE_NOT_READY = "source_not_ready"
CHALLENGE_REQUIRED = "challenge_required"
SOURCE_ACCESS_DENIED = "source_access_denied"
SOURCE_RATE_LIMITED = "source_rate_limited"
NO_CHAPTER_IMAGES = "no_chapter_images"
INVALID_IMAGE_RESPONSE = "invalid_image_response"
INCOMPLETE_DOWNLOAD = "incomplete_download"

ALLOWED_SCHEMES = ("http", "https")
ALLOWED_IMAGE_MIME = ("image/jpeg", "image/png", "image/webp", "image/avif", "image/gif")

# Interactive challenges we refuse to work around. Detecting one is a terminal, honest stop.
CHALLENGE_MARKERS = (
    "cf-challenge", "cf_chl", "turnstile", "hcaptcha", "recaptcha",
    "checking your browser", "verify you are human", "challenge-platform",
)


class SourceError(ValueError):
    """A sanitized, coded failure. Never carries cookies, headers or credentials."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class UnsupportedSource(SourceError):
    def __init__(self, host: str = ""):
        super().__init__(UNSUPPORTED_SOURCE, host)
        self.host = host


class ChallengeRequired(SourceError):
    """An interactive challenge stands in the way. We stop; we never solve it."""

    def __init__(self, detail: str = ""):
        super().__init__(CHALLENGE_REQUIRED, detail)


class ChapterSourceAdapter(Protocol):
    name: str
    allowed_hosts: tuple[str, ...]

    def supports(self, url: str) -> bool: ...
    def normalize_url(self, url: str) -> str: ...
    def validate_url(self, url: str) -> None: ...
    def validate_path(self, url: str) -> None: ...
    def reader_selectors(self) -> dict[str, str]: ...
    def classify_candidate(self, candidate: dict[str, Any]) -> str: ...
    def exclude_candidate(self, candidate: dict[str, Any]) -> str: ...


def host_of(url: str) -> str:
    """Lowercased hostname without a leading www., or '' when unparseable."""
    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def is_private_host(host: str) -> bool:
    """True for loopback/private/link-local/reserved targets, by literal or by DNS.

    Blocking only literals would miss a hostname that resolves inward, which is the usual
    shape of an SSRF. Resolution failures count as unsafe: we do not fetch what we cannot
    place.
    """
    if not host:
        return True
    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        return True
    candidates: list[str] = []
    try:
        ipaddress.ip_address(host)
        candidates.append(host)
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except (socket.gaierror, UnicodeError, OSError):
            return True                     # cannot resolve → refuse
        candidates.extend(str(info[4][0]) for info in infos)
    for raw in candidates:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            return True
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_reserved or address.is_multicast or address.is_unspecified):
            return True
    return False


def looks_like_challenge(text: str) -> bool:
    lowered = str(text or "").casefold()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


@dataclass
class BaseAdapter:
    """Default behaviour. A concrete source overrides only what differs."""

    name: str = "base"
    allowed_hosts: tuple[str, ...] = field(default_factory=tuple)
    runner: str = "run_webtoon.py"
    chapter_path_markers: tuple[str, ...] = field(default_factory=tuple)
    # Selectors are per-source data, not downloader logic.
    container_selector: str = ""
    image_selector: str = "img"
    min_image_width: int = 200
    min_image_height: int = 200

    # ---- host / url ---------------------------------------------------------
    def supports(self, url: str) -> bool:
        host = host_of(url)
        # Exact host or a dot-suffix subdomain. Never a raw string suffix, which would let
        # "evil-webtoons.com" or "webtoons.com.evil.net" impersonate an allowed host.
        return any(host == allowed or host.endswith(f".{allowed}")
                   for allowed in self.allowed_hosts)

    def normalize_url(self, url: str) -> str:
        parsed = urlparse(str(url or "").strip())
        return urlunparse((
            (parsed.scheme or "https").lower(), (parsed.netloc or "").lower(),
            parsed.path, parsed.params, parsed.query, "",
        ))

    def validate_url(self, url: str) -> None:
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme not in ALLOWED_SCHEMES:
            raise SourceError(UNSUPPORTED_SOURCE, "scheme")   # file:, data:, ftp:, …
        if parsed.username or parsed.password:
            raise SourceError(UNSUPPORTED_SOURCE, "credentials_in_url")
        host = host_of(url)
        if not self.supports(url):
            raise UnsupportedSource(host)
        if is_private_host(host):
            raise SourceError(UNSUPPORTED_SOURCE, "private_host")

    def validate_path(self, url: str) -> None:
        """The URL must look like a chapter, not a series index or a profile."""
        if not self.chapter_path_markers:
            return
        path = urlparse(str(url or "")).path.casefold()
        if not any(marker in path for marker in self.chapter_path_markers):
            raise SourceError(UNSUPPORTED_SOURCE, "not_a_chapter_url")

    def validate_redirect(self, final_url: str) -> None:
        """A redirect must land somewhere this adapter still claims."""
        self.validate_url(final_url)

    # ---- reader knowledge ---------------------------------------------------
    def reader_selectors(self) -> dict[str, str]:
        return {"container": self.container_selector, "image": self.image_selector}

    def classify_candidate(self, candidate: dict[str, Any]) -> str:
        """'chapter' | 'interface' — evidence-based, not class-name-based.

        Class names on these sites churn, so membership combines container context,
        rendered size and DOM order rather than trusting any single fragile signal.
        """
        if self.exclude_candidate(candidate):
            return "interface"
        if candidate.get("inContainer"):
            return "chapter"
        width = int(candidate.get("naturalWidth") or candidate.get("width") or 0)
        height = int(candidate.get("naturalHeight") or candidate.get("height") or 0)
        if width >= self.min_image_width and height >= self.min_image_height:
            return "chapter"
        return "interface"

    def exclude_candidate(self, candidate: dict[str, Any]) -> str:
        """Non-empty reason when this is clearly interface furniture, not a page."""
        haystack = " ".join(str(candidate.get(key) or "") for key in
                            ("url", "className", "id", "alt")).casefold()
        for reason in ("logo", "favicon", "banner", "avatar", "sprite", "icon",
                       "thumbnail", "thumb", "advert", "ads", "promo", "recommend",
                       "footer", "header", "button", "comment", "pixel", "tracking"):
            if reason in haystack:
                return reason
        width = int(candidate.get("naturalWidth") or candidate.get("width") or 0)
        height = int(candidate.get("naturalHeight") or candidate.get("height") or 0)
        if 0 < width <= 2 and 0 < height <= 2:
            return "tracking_pixel"
        return ""

    def absolutize(self, base_url: str, candidate_url: str) -> str:
        return urljoin(base_url, str(candidate_url or "").strip())

    def sanitize_error(self, error: BaseException) -> str:
        """Only the class name or a known code — never a message that may carry a URL."""
        return getattr(error, "code", None) or type(error).__name__


# Webtoons keeps its own selectors; the downloader no longer knows about them.
WEBTOONS = BaseAdapter(
    name="webtoons",
    allowed_hosts=("webtoons.com", "webtoon.com"),
    runner="run_webtoon.py",
    chapter_path_markers=("/viewer", "/episode"),
    container_selector="#_imageList, .viewer_img, .viewer_lst",
    image_selector="#_imageList img, .viewer_img img._images",
)


class GenericImageChapterAdapter(BaseAdapter):
    """Template for a site whose reader is plain lazy-loaded images.

    Not registered by default and never a fallback: it must be instantiated with the
    specific hosts an operator has both support for and the right to read.
    """

    def __init__(self, *, name: str, allowed_hosts: tuple[str, ...],
                 container_selector: str = "", chapter_path_markers: tuple[str, ...] = ()):
        super().__init__(
            name=name, allowed_hosts=allowed_hosts, runner="run_webtoon.py",
            chapter_path_markers=chapter_path_markers,
            container_selector=container_selector, image_selector="img",
        )


# Registry order is resolution order. Only these hosts are ever fetched.
ADAPTERS: tuple[BaseAdapter, ...] = (WEBTOONS,)


def select_adapter(url: str) -> BaseAdapter:
    for adapter in ADAPTERS:
        if adapter.supports(url):
            return adapter
    raise UnsupportedSource(host_of(url))


def supported_hosts() -> list[str]:
    return sorted({host for adapter in ADAPTERS for host in adapter.allowed_hosts})
