"""Byte-level validation for downloaded chapter pages.

Extension and Content-Type are both claims made by the server, so neither is trusted on its
own. A file is accepted only when its actual bytes decode as an image of plausible size.
The common failure this catches is an error page or a challenge served with an image
Content-Type — which would otherwise reach the pipeline as a "page" and end up in the PDF.
"""

from __future__ import annotations

import hashlib
import io
import warnings
from dataclasses import dataclass

from chapter_source import INVALID_IMAGE_RESPONSE, SourceError

# Real signatures, checked before handing anything to a decoder.
MAGIC = {
    "jpeg": (b"\xff\xd8\xff",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "gif": (b"GIF87a", b"GIF89a"),
    "webp": (b"RIFF",),          # plus 'WEBP' at offset 8
    "avif": (b"\x00\x00\x00",),  # ftyp box; 'ftypavif'/'ftypavis' at offset 4
    "bmp": (b"BM",),
}

# Anything starting like markup is a page, not an image, whatever the server claimed.
MARKUP_PREFIXES = (b"<!doctype", b"<html", b"<?xml", b"<svg", b"{", b"[")

MIN_WIDTH = 64
MIN_HEIGHT = 64
MIN_BYTES = 1024
MAX_IMAGE_PIXELS = 50_000_000


@dataclass(frozen=True)
class ValidatedImage:
    data: bytes
    width: int
    height: int
    fmt: str
    sha256: str

    @property
    def size(self) -> int:
        return len(self.data)


def sniff_format(data: bytes) -> str:
    """Format from the actual bytes, or '' when nothing matches."""
    if len(data) < 12:
        return ""
    if data.startswith(MAGIC["jpeg"]):
        return "jpeg"
    if data.startswith(MAGIC["png"]):
        return "png"
    if data.startswith(MAGIC["gif"]):
        return "gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):
        return "avif"
    if data.startswith(MAGIC["bmp"]):
        return "bmp"
    return ""


def looks_like_markup(data: bytes) -> bool:
    head = data[:256].lstrip()[:64].lower()
    return any(head.startswith(prefix) for prefix in MARKUP_PREFIXES)


def validate_image_bytes(data: bytes, *, min_width: int = MIN_WIDTH,
                         min_height: int = MIN_HEIGHT,
                         min_bytes: int = MIN_BYTES,
                         max_bytes: int | None = None) -> ValidatedImage:
    """Accept only bytes that really are a decodable image. Raises SourceError otherwise."""
    if not data:
        raise SourceError(INVALID_IMAGE_RESPONSE, "empty")
    if max_bytes is not None and len(data) > max_bytes:
        raise SourceError(INVALID_IMAGE_RESPONSE, "too_large")
    if len(data) < max(12, int(min_bytes)):
        raise SourceError(INVALID_IMAGE_RESPONSE, "too_small_bytes")
    if looks_like_markup(data):
        # HTML/JSON disguised as a JPEG — an error page or a challenge.
        raise SourceError(INVALID_IMAGE_RESPONSE, "markup_not_image")
    fmt = sniff_format(data)
    if not fmt:
        raise SourceError(INVALID_IMAGE_RESPONSE, "unknown_signature")

    try:
        from PIL import Image

        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                width, height = probe.size
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise SourceError(INVALID_IMAGE_RESPONSE, "too_many_pixels")
                probe.verify()                   # structural check without a full decode
            with Image.open(io.BytesIO(data)) as image:
                image.load()                     # full decode; catches truncation
    except SourceError:
        raise
    except Exception as exc:  # noqa: BLE001 - decoder errors are sanitized by class name
        raise SourceError(INVALID_IMAGE_RESPONSE, f"decode:{type(exc).__name__}") from exc

    if width < min_width or height < min_height:
        raise SourceError(INVALID_IMAGE_RESPONSE, f"dimensions:{width}x{height}")

    return ValidatedImage(data=data, width=width, height=height, fmt=fmt,
                          sha256=hashlib.sha256(data).hexdigest())


class DuplicateTracker:
    """Content-hash dedupe that preserves first-seen order."""

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}
        self.duplicates = 0

    def is_duplicate(self, image: ValidatedImage) -> bool:
        if image.sha256 in self._seen:
            self.duplicates += 1
            return True
        self._seen[image.sha256] = len(self._seen)
        return False

    def __len__(self) -> int:
        return len(self._seen)
