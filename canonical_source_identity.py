"""Resolve a submitted chapter URL against official public series metadata.

The resolver is deliberately narrower than a search engine.  It follows only the selected
adapter's public host, reads a bounded number of series-list pages, and accepts exactly one
official link whose series id, series slug and episode slug match the submitted identity.
It never derives an internal episode id from a visible number.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests

from chapter_source import SourceError, host_of

MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_METADATA_REDIRECTS = 3
DEFAULT_METADATA_PAGES = 12


class CanonicalSourceError(SourceError):
    """A sanitized canonical-identity failure."""


@dataclass(frozen=True)
class CanonicalSourceIdentity:
    normalized_url: str
    canonical_url: str
    series_id: str
    episode_id: str
    series_slug: str
    episode_slug: str
    changed: bool
    metadata_pages: int
    created_at: str

    def public(self) -> dict[str, Any]:
        status = (
            "canonical_source_resolved" if self.changed
            else "canonical_source_confirmed")
        identity_payload = {
            "source_type": "public_url",
            "adapter_name": "webtoons",
            "series_identifier": self.series_id,
            "episode_identifier": self.episode_id,
            "episode_label": self.episode_slug,
            "canonical_url": self.canonical_url,
            "identity_source": "official_public_metadata",
            "validation_status": status,
        }
        identity_hash = hashlib.sha256(json.dumps(
            identity_payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        return {
            "schema_version": 1,
            "normalized_url": self.normalized_url,
            **identity_payload,
            "validation_reason": (
                "submitted_identity_updated" if self.changed
                else "official_identity_matched"),
            "created_at": self.created_at,
            "identity_hash": identity_hash,
            "series_id": self.series_id,
            "episode_id": self.episode_id,
            "series_slug": self.series_slug,
            "episode_slug": self.episode_slug,
            "changed": self.changed,
            "metadata_pages": self.metadata_pages,
            "resolution": "official_public_metadata",
        }


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() != "a":
            return
        value = dict(attrs).get("href")
        if isinstance(value, str) and value.strip():
            self.links.append(value.strip())


class RequestsMetadataTransport:
    """Small fail-closed transport for public list pages; no cookies or browser headers."""

    def __init__(self, adapter, *, session=None, timeout=(10.0, 20.0)):
        self.adapter = adapter
        self.session = session or requests.Session()
        self.own_session = session is None
        self.timeout = timeout

    def close(self) -> None:
        if self.own_session:
            self.session.close()

    def fetch_page(self, url: str) -> str:
        current = url
        for _ in range(MAX_METADATA_REDIRECTS + 1):
            self.adapter.validate_navigation_url(current)
            response = self.session.get(
                current,
                headers={
                    "User-Agent": "Tradutor.Ia-source-metadata/1.0",
                    "Accept": "text/html,application/xhtml+xml",
                },
                timeout=self.timeout,
                stream=True,
                allow_redirects=False,
            )
            try:
                status = int(response.status_code)
                if status in (301, 302, 303, 307, 308):
                    location = str(response.headers.get("Location") or "")
                    if not location:
                        raise CanonicalSourceError(
                            "canonical_metadata_redirect_blocked", "missing_location")
                    current = urljoin(current, location)
                    self.adapter.validate_navigation_url(current)
                    continue
                if status >= 400:
                    raise CanonicalSourceError(
                        "canonical_metadata_unavailable", f"http_{status}")
                content_type = str(
                    response.headers.get("Content-Type") or ""
                ).split(";", 1)[0].strip().casefold()
                if content_type and content_type not in {
                    "text/html", "application/xhtml+xml"
                }:
                    raise CanonicalSourceError(
                        "canonical_metadata_invalid", "content_type")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_METADATA_BYTES:
                        raise CanonicalSourceError(
                            "canonical_metadata_invalid", "max_bytes")
                    chunks.append(chunk)
                encoding = response.encoding or "utf-8"
                return b"".join(chunks).decode(encoding, errors="replace")
            finally:
                response.close()
        raise CanonicalSourceError(
            "canonical_metadata_redirect_blocked", "max_redirects")


def _webtoons_parts(url: str) -> dict[str, str]:
    parsed = urlparse(str(url or "").strip())
    query = parse_qs(parsed.query, keep_blank_values=False)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) < 5 or segments[-1].casefold() != "viewer":
        raise CanonicalSourceError("canonical_identity_invalid", "chapter_path")
    title_no = str((query.get("title_no") or [""])[0]).strip()
    episode_no = str((query.get("episode_no") or [""])[0]).strip()
    if not title_no.isdigit() or not episode_no.isdigit():
        raise CanonicalSourceError("canonical_identity_invalid", "query_identity")
    return {
        "scheme": parsed.scheme.casefold(),
        "netloc": parsed.netloc.casefold(),
        "locale": segments[0],
        "genre": segments[1],
        "series_slug": segments[-3],
        "episode_slug": segments[-2],
        "series_id": title_no,
        "episode_id": episode_no,
    }


def _metadata_url(identity: dict[str, str], page: int) -> str:
    path = (
        f"/{identity['locale']}/{identity['genre']}/"
        f"{identity['series_slug']}/list"
    )
    query = urlencode({"title_no": identity["series_id"], "page": page})
    return urlunparse((
        identity["scheme"], identity["netloc"], path, "", query, "",
    ))


def _canonical_candidate(link: str, *, base_url: str,
                         expected: dict[str, str]) -> tuple[str, str] | None:
    absolute = urljoin(base_url, link)
    if host_of(absolute) != host_of(base_url):
        return None
    try:
        value = _webtoons_parts(absolute)
    except CanonicalSourceError:
        return None
    for key in ("series_id", "series_slug", "episode_slug"):
        if value[key].casefold() != expected[key].casefold():
            return None
    canonical_query = urlencode({
        "title_no": value["series_id"],
        "episode_no": value["episode_id"],
    })
    parsed = urlparse(absolute)
    canonical = urlunparse((
        parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path,
        "", canonical_query, "",
    ))
    return canonical, value["episode_id"]


def canonicalize_webtoons_url(url: str, *, transport=None,
                              adapter=None, max_pages=DEFAULT_METADATA_PAGES
                              ) -> CanonicalSourceIdentity:
    """Return one official canonical chapter link or fail closed."""
    expected = _webtoons_parts(url)
    if adapter is None:
        from chapter_source import select_adapter

        adapter = select_adapter(url)
    if str(getattr(adapter, "name", "")) != "webtoons":
        raise CanonicalSourceError("canonical_identity_unsupported", "adapter")
    adapter.validate_navigation_url(url)
    own_transport = transport is None
    metadata = transport or RequestsMetadataTransport(adapter)
    pages = max(1, min(int(max_pages), DEFAULT_METADATA_PAGES))
    try:
        for page in range(1, pages + 1):
            list_url = _metadata_url(expected, page)
            html = metadata.fetch_page(list_url)
            parser = _LinkParser()
            parser.feed(str(html or ""))
            matches = {
                match
                for link in parser.links
                if (match := _canonical_candidate(
                    link, base_url=list_url, expected=expected)) is not None
            }
            if len(matches) > 1:
                raise CanonicalSourceError(
                    "canonical_episode_ambiguous", "official_metadata")
            if len(matches) == 1:
                canonical, episode_id = next(iter(matches))
                submitted_normalized = urlunparse((
                    urlparse(url).scheme.casefold(),
                    urlparse(url).netloc.casefold(),
                    urlparse(url).path,
                    "",
                    urlencode({
                        "title_no": expected["series_id"],
                        "episode_no": expected["episode_id"],
                    }),
                    "",
                ))
                return CanonicalSourceIdentity(
                    normalized_url=submitted_normalized,
                    canonical_url=canonical,
                    series_id=expected["series_id"],
                    episode_id=episode_id,
                    series_slug=expected["series_slug"],
                    episode_slug=expected["episode_slug"],
                    changed=canonical != submitted_normalized,
                    metadata_pages=page,
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
            if not parser.links:
                break
    finally:
        if own_transport:
            metadata.close()
    raise CanonicalSourceError(
        "canonical_episode_not_found", "official_metadata")
