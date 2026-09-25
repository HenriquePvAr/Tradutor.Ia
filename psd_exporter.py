"""Atomic PSD export from already-rendered pages.

The exporter deliberately has no OCR, translation, provider, or rendering entry points.
It receives the source pixels and the pipeline's final rendered pixels and writes one real
two-layer PSD per page: ``Translated`` above ``Original``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image


class PsdExportError(RuntimeError):
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


def _canonical(path: str | Path) -> Image.Image:
    try:
        with Image.open(path) as image:
            image.load()
            if image.width <= 0 or image.height <= 0:
                raise ValueError("invalid_dimensions")
            return image.convert("RGBA")
    except Exception as exc:  # noqa: BLE001 - expose one controlled exporter error
        raise PsdExportError("invalid_input_image") from exc


def _validate_psd(path: Path, original: Image.Image, translated: Image.Image) -> None:
    try:
        from psd_tools import PSDImage
    except ImportError as exc:
        raise PsdExportError("psd_library_unavailable") from exc
    try:
        reopened = PSDImage.open(path)
        if tuple(reopened.size) != tuple(translated.size):
            raise PsdExportError("psd_canvas_dimension_mismatch")
        layers = list(reopened)
        if len(layers) != 2 or [layer.name for layer in layers] != ["Original", "Translated"]:
            raise PsdExportError("psd_layer_structure_invalid")
        if not all(layer.kind == "pixel" for layer in layers):
            raise PsdExportError("psd_non_pixel_layer")
        original_layer = layers[0].topil().convert("RGBA")
        translated_layer = layers[1].topil().convert("RGBA")
        if original_layer.tobytes() != original.tobytes():
            raise PsdExportError("original_layer_pixel_identity_mismatch")
        if translated_layer.tobytes() != translated.tobytes():
            raise PsdExportError("translated_layer_pixel_identity_mismatch")
        composite = reopened.composite().convert("RGBA")
        expected_composite = Image.alpha_composite(original, translated)
        if composite.tobytes() != expected_composite.tobytes():
            raise PsdExportError("psd_composite_mismatch")
        # Independent parser check: Pillow must open the written PSD and expose the same
        # canvas dimensions. Pillow does not provide a trustworthy layer API here.
        with Image.open(path) as independent:
            independent.load()
            if tuple(independent.size) != tuple(translated.size):
                raise PsdExportError("independent_psd_parse_dimension_mismatch")
    except PsdExportError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PsdExportError("psd_reopen_validation_failed") from exc


def _write_one(original_path: Path, translated_path: Path, destination: Path) -> None:
    try:
        from psd_tools import PSDImage
        from psd_tools.api.layers import PixelLayer
    except ImportError as exc:
        raise PsdExportError("psd_library_unavailable") from exc
    original = _canonical(original_path)
    translated = _canonical(translated_path)
    if original.size != translated.size:
        raise PsdExportError("psd_dimension_mismatch")
    psd = PSDImage.new("RGBA", translated.size)
    # psd-tools stores the first appended layer at the bottom; appending in this order
    # gives the required visual stack Original (bottom) -> Translated (top).
    psd.append(PixelLayer.frompil(original, psd, layer_name="Original"))
    psd.append(PixelLayer.frompil(translated, psd, layer_name="Translated"))
    temp_file = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        psd.save(temp_file)
        _validate_psd(temp_file, original, translated)
        os.replace(temp_file, destination)
    except PsdExportError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PsdExportError("psd_save_failed") from exc
    finally:
        try:
            temp_file.unlink()
        except OSError:
            pass


def export_final_pages_psd(
    page_pairs: Iterable[tuple[str | Path, str | Path]],
    output_root: str | Path,
    chapter_name: str,
    *,
    cancel: Callable[[], bool] | None = None,
) -> Path:
    """Atomically publish one validated two-layer PSD per page."""

    pairs = [(Path(original).resolve(strict=True), Path(translated).resolve(strict=True))
             for original, translated in page_pairs]
    if not pairs:
        raise PsdExportError("no_final_pages")
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(chapter_name or "chapter"))
    safe_name = safe_name.strip("._") or "chapter"
    destination = _safe_destination(root, safe_name)
    temp = Path(tempfile.mkdtemp(prefix=f".{safe_name}.", dir=str(root)))
    try:
        width = max(3, len(str(len(pairs))))
        for index, (original, translated) in enumerate(pairs, 1):
            if cancel and cancel():
                raise PsdExportError("cancelled")
            target = temp / f"{index:0{width}d}.psd"
            _write_one(original, translated, target)
        if cancel and cancel():
            raise PsdExportError("cancelled")
        os.replace(temp, destination)
        return destination
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
