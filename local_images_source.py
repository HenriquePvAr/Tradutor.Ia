"""Safe local-image selection and deterministic staging primitives.

This module is intentionally UI/pipeline agnostic.  It owns selection semantics (natural
sort for the first batch, append for later batches, explicit reorder/remove/clear) and
copies validated images into generated ordinal names.  The existing local-folder pipeline
can consume the resulting directory without a second OCR/translation implementation.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image

SUPPORTED_LOCAL_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg")
_NATURAL_SPLIT = re.compile(r"(\d+)")


def natural_sort_key(path: str | Path) -> tuple:
    name = unicodedata.normalize("NFKC", Path(path).name).casefold()
    return tuple((0, int(part)) if part.isdigit() else (1, part)
                 for part in _NATURAL_SPLIT.split(name) if part)


@dataclass(frozen=True)
class LocalImagePage:
    identity: str
    path: Path
    display_name: str
    extension: str


class LocalImagesError(ValueError):
    pass


def _validate_path(path: Path) -> LocalImagePage:
    path = Path(path).expanduser()
    suffix = path.suffix.casefold()
    if suffix not in SUPPORTED_LOCAL_IMAGE_EXTENSIONS:
        raise LocalImagesError(f"unsupported_extension:{path.name}")
    if not path.is_file():
        raise LocalImagesError(f"file_not_found:{path.name}")
    if path.stat().st_size <= 0:
        raise LocalImagesError(f"empty_file:{path.name}")
    try:
        with Image.open(path) as image:
            image.verify()
            width, height = image.size
    except Exception as exc:  # PIL uses several decoder-specific exceptions.
        raise LocalImagesError(f"invalid_image:{path.name}") from exc
    if width <= 0 or height <= 0:
        raise LocalImagesError(f"invalid_dimensions:{path.name}")
    resolved = path.resolve(strict=True)
    identity = hashlib.sha256(str(resolved).casefold().encode("utf-8")).hexdigest()
    return LocalImagePage(identity, resolved, path.name, suffix)


class LocalImagesSelection:
    """Mutable selection state with explicit ordering semantics."""

    def __init__(self) -> None:
        self.pages: list[LocalImagePage] = []

    def add(self, paths: Iterable[str | Path], *, initial: bool = False) -> list[LocalImagePage]:
        incoming = [_validate_path(Path(path)) for path in paths]
        known = {page.identity for page in self.pages}
        unique: list[LocalImagePage] = []
        for page in incoming:
            if page.identity in known:
                continue
            known.add(page.identity)
            unique.append(page)
        incoming = unique
        # An explicit UI submit may already carry an intentional order (for example
        # the reordered opaque media ids from the page manager).  Only the native
        # picker's first batch requests natural sorting explicitly via ``initial``;
        # an empty selection with ``initial=False`` must preserve caller order.
        if initial:
            incoming.sort(key=lambda page: natural_sort_key(page.path))
        self.pages.extend(incoming)
        return list(self.pages)

    def move(self, index: int, target: int) -> None:
        if not 0 <= index < len(self.pages) or not 0 <= target < len(self.pages):
            raise IndexError("page_index")
        page = self.pages.pop(index)
        self.pages.insert(target, page)

    def remove(self, index: int) -> LocalImagePage:
        return self.pages.pop(index)

    def clear(self) -> None:
        self.pages.clear()

    def snapshot(self, root: str | Path, *, job_id: str) -> Path:
        if not self.pages:
            raise LocalImagesError("empty_selection")
        base = Path(root).resolve() / str(job_id)
        staging = base / "input"
        staging.mkdir(parents=True, exist_ok=False)
        try:
            width = max(4, len(str(len(self.pages))))
            for index, page in enumerate(self.pages, start=1):
                destination = staging / f"{index:0{width}d}{page.extension}"
                shutil.copy2(page.path, destination)
            return staging
        except Exception:
            shutil.rmtree(base, ignore_errors=True)
            raise
