"""Single source of truth for the chapter PDF filename.

The name is derived from the work and the chapter it belongs to, so an output can
be identified without opening it. Every module that needs the name asks here, and
sanitisation lives only in this file.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qs, urlparse


_FALLBACK_SERIES = "obra"
_CHAPTER_WORD = "capitulo"
_MAX_SERIES_LENGTH = 90
_MAX_FILENAME_LENGTH = 120
_EXTENSION = ".pdf"

# Names Windows refuses to create, whatever the extension.
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{digit}" for digit in range(1, 10)}
    | {f"lpt{digit}" for digit in range(1, 10)}
)

# Characters Windows rejects in a filename.
_INVALID_CHARACTERS = r'<>:"/\\|?*'

# A chapter segment names the episode, never the work.
_EPISODE_SEGMENT = re.compile(r"(?:^|-)(?:ep|episode|chapter|cap)\b|-?episode-\d", re.I)
_TRAILING_NUMBER = re.compile(r"(\d+)\s*$")


def sanitize_filename_component(text: str) -> str:
    """Reduce arbitrary text to a lowercase, Windows-safe filename component."""

    value = unicodedata.normalize("NFKD", str(text or ""))
    value = value.encode("ascii", "ignore").decode("ascii")
    value = value.lower()
    for character in _INVALID_CHARACTERS:
        value = value.replace(character, "")
    value = value.replace(".", "")  # also kills "..", so no traversal survives
    value = re.sub(r"[\s\-]+", "_", value)
    value = re.sub(r"[^a-z0-9_]", "", value)
    value = re.sub(r"_+", "_", value)
    return value.strip("_")


def series_slug_from_url(source_url: str) -> str:
    """The work's slug in the chapter URL, never the chapter's own segment."""

    path = urlparse(str(source_url or "")).path
    segments = [segment for segment in path.split("/") if segment]
    # Drop trailing viewer/reader pages and the chapter segment itself.
    candidates = [
        segment
        for segment in segments
        if segment.lower() not in {"viewer", "list", "en", "episode"}
        and not _EPISODE_SEGMENT.search(segment)
    ]
    if len(candidates) < 2:
        return ""
    # The last remaining segment is the work; earlier ones are language and genre.
    return sanitize_filename_component(candidates[-1])


def episode_number_from_url(source_url: str) -> str:
    query = parse_qs(urlparse(str(source_url or "")).query)
    for key in ("episode_no", "episode", "chapter_no"):
        values = query.get(key) or []
        for value in values:
            digits = re.sub(r"\D", "", str(value))
            if digits:
                return str(int(digits))
    return ""


def _episode_number_from_title(title: str) -> str:
    match = _TRAILING_NUMBER.search(str(title or ""))
    return str(int(match.group(1))) if match else ""


def _safe_series(series_title: str, source_url: str) -> str:
    for candidate in (
        sanitize_filename_component(series_title),
        series_slug_from_url(source_url),
    ):
        if candidate:
            series = candidate[:_MAX_SERIES_LENGTH].strip("_")
            if series and series not in _RESERVED_NAMES:
                return series
            if series in _RESERVED_NAMES:
                return f"{series}_serie"
    return _FALLBACK_SERIES


def _safe_number(episode_number, source_url: str, series_title: str, fallback_id: str) -> str:
    if episode_number is not None:
        digits = re.sub(r"\D", "", str(episode_number))
        if digits:
            return str(int(digits))
    for candidate in (
        episode_number_from_url(source_url),
        _episode_number_from_title(series_title),
    ):
        if candidate:
            return candidate
    identifier = sanitize_filename_component(fallback_id)
    return identifier[:12] if identifier else "s_n"


def build_pdf_filename(
    source_url: str = "",
    series_title: str = "",
    episode_number=None,
    fallback_id: str = "",
) -> str:
    """Name the chapter PDF after the work and the chapter, safely.

    The work comes from its title when the pipeline knows it, otherwise from the
    slug in the chapter URL; the chapter segment of the URL names the episode and
    is never used as the work's name. The number comes from the chapter metadata,
    then the URL, then a trailing number in the title, and only falls back to the
    run identifier when the chapter carries no number at all. The result is a bare
    filename: it never contains a path.
    """

    series = _safe_series(series_title, source_url)
    number = _safe_number(episode_number, source_url, series_title, fallback_id)
    suffix = f"_{_CHAPTER_WORD}_{number}{_EXTENSION}"
    budget = _MAX_FILENAME_LENGTH - len(suffix)
    if budget < 1:
        series = _FALLBACK_SERIES
        budget = _MAX_FILENAME_LENGTH - len(suffix)
    series = series[: max(1, budget)].strip("_") or _FALLBACK_SERIES
    return f"{series}{suffix}"
