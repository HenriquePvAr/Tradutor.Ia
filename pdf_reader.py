"""Read pages out of the chapter PDFs this application produces.

``pdf.generate_pdf`` writes every chapter with Pillow: one full-page image per PDF
page, ``/DCTDecode`` (baseline JPEG), no text operators, no encryption, a plain
cross-reference table. That narrow shape is the only thing this module reads, and it
is why the in-app reader needs no PDF rendering dependency at all: a page is served
by handing the browser the JPEG that already sits inside the artifact.

Anything outside that shape raises :class:`UnsupportedPdf` so the caller can fall
back to the browser's own PDF viewer instead of guessing at the bytes.

This module never writes. The artifact is opened read-only, byte ranges are read by
offset, and no path here is ever taken from a client.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# A page thumbnail is a navigation aid, not a reading surface: this width keeps the
# sidebar cheap to build for a hundred-page chapter.
THUMBNAIL_WIDTH = 150
# ponytail: bounded LRU keyed on (path, mtime, size). Sixteen parsed documents and
# 256 thumbnails is far more than one reader ever holds open; raise only if a real
# workflow keeps more chapters warm at once.
_DOCUMENT_CACHE_SIZE = 16
_THUMBNAIL_CACHE_SIZE = 256

_XREF_TAIL_BYTES = 2048
_WHITESPACE = re.compile(rb"\s*")
_XREF_SUBSECTION = re.compile(rb"(\d+)[ \t]+(\d+)[ \t]*\r?\n")
_OBJECT_HEADER = re.compile(rb"\s*(\d+)\s+(\d+)\s+obj")
_REFERENCE = re.compile(rb"(\d+)\s+0\s+R")


class UnsupportedPdf(RuntimeError):
    """The artifact is a PDF, but not one whose pages this module can extract."""


@dataclass(frozen=True)
class ReaderPage:
    """One page: where its JPEG lives in the file and how big the image is."""

    number: int  # user-facing, 1-based
    offset: int
    length: int
    width: int
    height: int


@dataclass(frozen=True)
class ReaderDocument:
    path: str
    pages: tuple[ReaderPage, ...]

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def page(self, number: int) -> ReaderPage:
        """Return the 1-based page, rejecting 0, negatives and past-the-end."""

        if not isinstance(number, int) or isinstance(number, bool):
            raise IndexError("page_number_invalid")
        if number < 1 or number > len(self.pages):
            raise IndexError("page_number_out_of_range")
        return self.pages[number - 1]

    def page_bytes(self, number: int) -> bytes:
        page = self.page(number)
        with open(self.path, "rb") as handle:
            handle.seek(page.offset)
            data = handle.read(page.length)
        if len(data) != page.length:
            raise UnsupportedPdf("page_stream_truncated")
        return data


def _skip_whitespace(data: bytes, pos: int) -> int:
    return _WHITESPACE.match(data, pos).end()


def _startxref(data: bytes) -> int:
    offsets = re.findall(rb"startxref\s+(\d+)", data[-_XREF_TAIL_BYTES:])
    if not offsets:
        raise UnsupportedPdf("startxref_missing")
    return int(offsets[-1])


def _parse_xref(data: bytes, start: int) -> tuple[dict[int, int], bytes]:
    """Parse a classic cross-reference table plus its trailer dictionary."""

    if not data.startswith(b"xref", start):
        raise UnsupportedPdf("xref_table_missing")
    pos = _skip_whitespace(data, start + 4)
    offsets: dict[int, int] = {}
    while not data.startswith(b"trailer", pos):
        header = _XREF_SUBSECTION.match(data, pos)
        if not header:
            raise UnsupportedPdf("xref_subsection_invalid")
        first, count = int(header.group(1)), int(header.group(2))
        pos = header.end()
        for index in range(count):
            entry = data[pos:pos + 20]
            if len(entry) < 18:
                raise UnsupportedPdf("xref_entry_truncated")
            if entry[17:18] == b"n":
                offsets[first + index] = int(entry[0:10])
            pos += 20
        pos = _skip_whitespace(data, pos)
    trailer_end = data.find(b"startxref", pos)
    trailer = data[pos:trailer_end if trailer_end != -1 else len(data)]
    return offsets, trailer


def _object_at(data: bytes, offset: int) -> tuple[bytes, int]:
    """Return an object's dictionary text and the position right after it."""

    header = _OBJECT_HEADER.match(data, offset)
    if not header:
        raise UnsupportedPdf("object_header_invalid")
    body = header.end()
    ends = [end for end in (data.find(b"stream", body), data.find(b"endobj", body))
            if end != -1]
    if not ends:
        raise UnsupportedPdf("object_end_missing")
    return data[body:min(ends)], body


def _resolve(data: bytes, offsets: dict[int, int], number: int) -> tuple[bytes, int]:
    if number not in offsets:
        raise UnsupportedPdf("object_not_in_xref")
    return _object_at(data, offsets[number])


def _reference(text: bytes, key: bytes) -> int | None:
    match = re.search(re.escape(key) + rb"\s+(\d+)\s+0\s+R", text)
    return int(match.group(1)) if match else None


def _integer(text: bytes, key: bytes, data: bytes, offsets: dict[int, int]) -> int:
    direct = re.search(re.escape(key) + rb"\s+(\d+)(?![\s\d]*\bR\b)", text)
    if direct:
        return int(direct.group(1))
    indirect = _reference(text, key)
    if indirect is None:
        raise UnsupportedPdf(f"missing_{key.decode().strip('/').lower()}")
    body, _ = _resolve(data, offsets, indirect)
    value = re.match(rb"\s*(\d+)", body)
    if not value:
        raise UnsupportedPdf("indirect_integer_invalid")
    return int(value.group(1))


def _page_objects(data: bytes, offsets: dict[int, int], node: int,
                  seen: set[int]) -> list[int]:
    """Flatten the page tree in reading order, refusing to loop on itself."""

    if node in seen:
        raise UnsupportedPdf("page_tree_cycle")
    seen.add(node)
    text, _ = _resolve(data, offsets, node)
    if re.search(rb"/Type\s*/Page\b(?!s)", text):
        return [node]
    kids = re.search(rb"/Kids\s*\[(.*?)\]", text, re.S)
    if not kids:
        raise UnsupportedPdf("page_tree_invalid")
    pages: list[int] = []
    for reference in _REFERENCE.finditer(kids.group(1)):
        pages.extend(_page_objects(data, offsets, int(reference.group(1)), seen))
    if not pages:
        raise UnsupportedPdf("document_has_no_pages")
    return pages


def _page_image(data: bytes, offsets: dict[int, int], page_object: int,
                number: int) -> ReaderPage:
    text, _ = _resolve(data, offsets, page_object)
    resources = _reference(text, b"/Resources")
    resource_text = _resolve(data, offsets, resources)[0] if resources else text
    xobject = re.search(rb"/XObject\s*<<(.*?)>>", resource_text, re.S)
    if not xobject:
        raise UnsupportedPdf("page_has_no_image")
    references = _REFERENCE.findall(xobject.group(1))
    if len(references) != 1:
        raise UnsupportedPdf("page_is_not_a_single_image")

    image_text, body = _resolve(data, offsets, int(references[0]))
    if not re.search(rb"/Subtype\s*/Image\b", image_text):
        raise UnsupportedPdf("xobject_is_not_an_image")
    if not re.search(rb"/Filter\s*/DCTDecode\b", image_text):
        raise UnsupportedPdf("page_image_is_not_jpeg")
    length = _integer(image_text, b"/Length", data, offsets)
    width = _integer(image_text, b"/Width", data, offsets)
    height = _integer(image_text, b"/Height", data, offsets)

    stream = data.find(b"stream", body)
    if stream == -1:
        raise UnsupportedPdf("page_stream_missing")
    start = stream + len(b"stream")
    if data.startswith(b"\r\n", start):
        start += 2
    elif data[start:start + 1] in (b"\n", b"\r"):
        start += 1
    if start + length > len(data):
        raise UnsupportedPdf("page_stream_truncated")
    if data[start:start + 2] != b"\xff\xd8":
        raise UnsupportedPdf("page_image_is_not_jpeg")
    return ReaderPage(number=number, offset=start, length=length,
                      width=width, height=height)


def parse_document(path: Path | str) -> ReaderDocument:
    """Parse an image-only PDF into per-page JPEG byte ranges."""

    resolved = Path(path)
    data = resolved.read_bytes()
    if not data.startswith(b"%PDF-"):
        raise UnsupportedPdf("not_a_pdf")
    offsets, trailer = _parse_xref(data, _startxref(data))
    if re.search(rb"/Encrypt\b", trailer):
        raise UnsupportedPdf("encrypted_pdf")
    root = _reference(trailer, b"/Root")
    if root is None:
        raise UnsupportedPdf("catalog_missing")
    catalog, _ = _resolve(data, offsets, root)
    pages_root = _reference(catalog, b"/Pages")
    if pages_root is None:
        raise UnsupportedPdf("page_tree_missing")
    page_objects = _page_objects(data, offsets, pages_root, set())
    pages = tuple(
        _page_image(data, offsets, page_object, index)
        for index, page_object in enumerate(page_objects, start=1)
    )
    return ReaderDocument(path=str(resolved), pages=pages)


@lru_cache(maxsize=_DOCUMENT_CACHE_SIZE)
def _cached_document(path: str, mtime_ns: int, size: int) -> ReaderDocument:
    return parse_document(path)


def open_document(path: Path | str) -> ReaderDocument:
    """Parse (or reuse) the document for ``path``, re-parsing if the file changed."""

    resolved = Path(path)
    stat = resolved.stat()
    return _cached_document(str(resolved), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=_THUMBNAIL_CACHE_SIZE)
def _cached_thumbnail(path: str, mtime_ns: int, size: int, number: int,
                      width: int) -> bytes:
    from PIL import Image

    source = open_document(path).page_bytes(number)
    with Image.open(io.BytesIO(source)) as image:
        # draft() lets the JPEG decoder downscale while decoding, so a 4000px page
        # never becomes a full-size bitmap just to produce a 150px thumbnail.
        image.draft("RGB", (width, max(1, image.height * width // max(1, image.width))))
        image.thumbnail((width, 4 * width), Image.LANCZOS)
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, "JPEG", quality=70, optimize=True)
    return buffer.getvalue()


def page_thumbnail(path: Path | str, number: int, *,
                   width: int = THUMBNAIL_WIDTH) -> bytes:
    """Return a small JPEG preview of one page, cached and bounded."""

    resolved = Path(path)
    stat = resolved.stat()
    return _cached_thumbnail(str(resolved), stat.st_mtime_ns, stat.st_size,
                             int(number), int(width))
