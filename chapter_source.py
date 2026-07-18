"""Explicit per-source adapter selection for chapter URLs.

The pipeline used to send every URL to the Webtoons runner regardless of host, so an
unsupported site produced a job that could only fail late and obscurely. Selection is now
explicit and testable: a host is either claimed by a registered adapter or rejected up front
with ``unsupported_source``, which the UI can render as a real message.

Adding a source means registering an adapter — no fallback ever guesses at an unknown site,
because a wrong guess means fetching from a host nobody vetted (an SSRF-shaped risk).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse, urlunparse


class UnsupportedSource(ValueError):
    """No registered adapter claims this URL. Carries a stable, sanitized code."""

    code = "unsupported_source"

    def __init__(self, host: str = ""):
        # The host is echoed back because it is user-supplied and safe to show; nothing
        # else from the URL (query, credentials, path) is included.
        super().__init__(f"unsupported_source: {host}" if host else "unsupported_source")
        self.host = host


class ChapterSourceAdapter(Protocol):
    """Contract every chapter source implements."""

    name: str
    allowed_hosts: tuple[str, ...]

    def supports(self, url: str) -> bool: ...
    def normalize_url(self, url: str) -> str: ...
    def validate_url(self, url: str) -> None: ...
    def build_command(self, **kwargs: Any) -> list[str]: ...


def host_of(url: str) -> str:
    """Lowercased hostname without a leading www., or '' when unparseable."""
    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


@dataclass
class BaseAdapter:
    name: str = "base"
    allowed_hosts: tuple[str, ...] = field(default_factory=tuple)
    runner: str = "run_webtoon.py"

    def supports(self, url: str) -> bool:
        host = host_of(url)
        # Exact host or a subdomain of an allowed host — never a suffix match on the raw
        # string, which would let "evil-webtoons.com" impersonate "webtoons.com".
        return any(host == allowed or host.endswith(f".{allowed}")
                   for allowed in self.allowed_hosts)

    def normalize_url(self, url: str) -> str:
        """Canonical form: scheme+host lowercased, fragment dropped, path kept verbatim."""
        parsed = urlparse(str(url or "").strip())
        return urlunparse((
            (parsed.scheme or "https").lower(), (parsed.netloc or "").lower(),
            parsed.path, parsed.params, parsed.query, "",
        ))

    def validate_url(self, url: str) -> None:
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme not in ("http", "https"):
            raise ValueError("A URL precisa começar com http:// ou https://.")
        if parsed.username or parsed.password:
            raise ValueError("A URL não pode conter credenciais.")
        if not self.supports(url):
            raise UnsupportedSource(host_of(url))


WEBTOONS = BaseAdapter(
    name="webtoons",
    allowed_hosts=("webtoons.com", "webtoon.com"),
    runner="run_webtoon.py",
)

# Registry order is the resolution order. Only hosts listed here are ever fetched.
ADAPTERS: tuple[BaseAdapter, ...] = (WEBTOONS,)


def select_adapter(url: str) -> BaseAdapter:
    for adapter in ADAPTERS:
        if adapter.supports(url):
            return adapter
    raise UnsupportedSource(host_of(url))


def supported_hosts() -> list[str]:
    return sorted({host for adapter in ADAPTERS for host in adapter.allowed_hosts})
