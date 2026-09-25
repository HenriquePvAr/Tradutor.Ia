"""Atomic export of final rendered page images.

The exporter deliberately accepts only the pipeline's *final* page paths.  It never
re-reads source pages and it promotes a complete directory only after every image has
been decoded and validated.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image


class PngExportError(RuntimeError):
    pass


def _safe_destination(root: Path, name: str) -> Path:
    candidate = root / name
    if not candidate.exists():
        return candidate
    index = 2
    while True:
        candidate = root / f"{name}_{index}"
        if not candidate.exists():
            return candidate
        index += 1


def export_final_pages_png(
    page_paths: Iterable[str | Path],
    output_root: str | Path,
    chapter_name: str,
    *,
    cancel: Callable[[], bool] | None = None,
) -> Path:
    """Atomically publish final rendered pages as ``001.png``, ``002.png``, ... ."""

    pages = [Path(path).resolve(strict=True) for path in page_paths]
    if not pages:
        raise PngExportError("no_final_pages")
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(chapter_name or "chapter"))
    safe_name = safe_name.strip("._") or "chapter"
    destination = _safe_destination(root, safe_name)
    temp = Path(tempfile.mkdtemp(prefix=f".{safe_name}.", dir=str(root)))
    try:
        width = max(3, len(str(len(pages))))
        for index, source in enumerate(pages, 1):
            if cancel and cancel():
                raise PngExportError("cancelled")
            target = temp / f"{index:0{width}d}.png"
            try:
                with Image.open(source) as image:
                    image.load()
                    if image.width <= 0 or image.height <= 0:
                        raise ValueError("invalid_dimensions")
                    image.convert("RGBA").save(target, format="PNG")
                with Image.open(target) as check:
                    check.load()
                    if target.stat().st_size <= 0 or check.width <= 0 or check.height <= 0:
                        raise ValueError("invalid_export")
            except Exception as exc:
                raise PngExportError(f"page_{index}_invalid") from exc
        if cancel and cancel():
            raise PngExportError("cancelled")
        os.replace(str(temp), str(destination))
        return destination
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise

