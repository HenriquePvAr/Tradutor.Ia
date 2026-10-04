"""Professional layered PSD export (P1 Cleaned + P2 per-region raster layers).

Layer stack per page, bottom -> top:

    Original            full page, untouched source
    Cleaned             page after text removal/inpaint, BEFORE translated glyphs
    Text/Region NNN     one transparent raster layer per rendered region (bbox-sized)
    Translated Preview  flattened final translated page (reference, top)

The region layers are RASTER, not editable TypeTool text (``PSD_TEXT_MODE=RASTER_PER_REGION``,
``EDITABLE_TEXT_SUPPORTED=NO``). They carry only the delta a region's translation added over
the Cleaned page, isolated to that region's effective bounding box, so ``Cleaned`` plus the
region layers recompose the translated page while each region can be toggled independently.

Memory: every region layer is bbox-sized and offset on the canvas (never a full-page RGBA
buffer), and pages are written and released one at a time. Only three full-page buffers
(Original, Cleaned, Translated Preview) exist per page.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import numpy as np
from PIL import Image

# Any channel delta above this (out of 255) counts as "the translation drew here". Small,
# to tolerate RLE/anti-alias noise without swallowing faint glyph edges.
_REGION_ALPHA_THRESHOLD = 6


class ProfessionalPsdError(RuntimeError):
    pass


def _safe_destination(root: Path, name: str) -> Path:
    candidate = root / name
    index = 2
    while candidate.exists():
        candidate = root / f"{name}_{index}"
        index += 1
    return candidate


def _safe_name(value: str) -> str:
    name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(value or "chapter"))
    return name.strip("._") or "chapter"


def _bgr_to_rgba_pil(bgr: np.ndarray) -> Image.Image:
    rgb = bgr[:, :, ::-1]  # BGR -> RGB view
    rgba = np.dstack([rgb, np.full(rgb.shape[:2], 255, dtype=np.uint8)])
    return Image.fromarray(np.ascontiguousarray(rgba), "RGBA")


def _sorted_regions(regions: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(region: dict[str, Any]) -> tuple[int, int, str]:
        box = region.get("bbox") or [0, 0, 0, 0]
        try:
            y, x = int(box[1]), int(box[0])
        except (TypeError, ValueError, IndexError):
            y, x = 0, 0
        return (y, x, str(region.get("region_id") or ""))
    return sorted(regions, key=key)


def _clamp_box(box: Sequence[float], width: int, height: int) -> tuple[int, int, int, int] | None:
    try:
        x, y, w, h = (int(round(float(box[0]))), int(round(float(box[1]))),
                      int(round(float(box[2]))), int(round(float(box[3]))))
    except (TypeError, ValueError, IndexError):
        return None
    if w <= 0 or h <= 0:
        return None
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(width, x + w), min(height, y + h)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1 - x0, y1 - y0


def _region_layer_rgba(translated_bgr: np.ndarray, cleaned_bgr: np.ndarray,
                       box: tuple[int, int, int, int]) -> Image.Image | None:
    x, y, w, h = box
    tb = translated_bgr[y:y + h, x:x + w].astype(np.int16)
    cb = cleaned_bgr[y:y + h, x:x + w].astype(np.int16)
    diff = np.abs(tb - cb).max(axis=2)
    alpha = np.where(diff > _REGION_ALPHA_THRESHOLD, 255, 0).astype(np.uint8)
    if not alpha.any():
        return None  # translation drew nothing distinguishable in this region
    rgb = translated_bgr[y:y + h, x:x + w][:, :, ::-1]
    rgba = np.dstack([rgb, alpha])
    return Image.fromarray(np.ascontiguousarray(rgba), "RGBA")


def _would_render(region: dict[str, Any]) -> bool:
    """A region gets a translated raster layer only in ON, when active, effectively
    translate, and carrying a non-empty target. IGNORE/empty/inactive never render."""
    if not region.get("active", True):
        return False
    if str(region.get("translate_mode") or "translate") != "translate":
        return False
    return bool(str(region.get("target") or "").strip())


def _region_metadata(region: dict[str, Any], page_index: int) -> dict[str, Any]:
    return {
        "region_id": str(region.get("region_id") or ""),
        "page_index": page_index,
        "bbox": list(region.get("bbox") or []),
        "source_text_effective": str(region.get("source_text") or ""),
        "target_text_effective": str(region.get("target") or ""),
        "region_type_effective": str(region.get("region_type") or ""),
        "translate_mode_effective": str(region.get("translate_mode") or "translate"),
        "manual": bool(region.get("manual")),
        "active": bool(region.get("active", True)),
        "would_render_in_typesetting_on": _would_render(region),
    }


def write_professional_page_psd(
    dest_path: Path, *, original_bgr: np.ndarray, cleaned_bgr: np.ndarray,
    translated_bgr: np.ndarray, regions: Sequence[dict[str, Any]], page_index: int,
    typesetting_mode: str = "on",
) -> dict[str, Any]:
    """Write one layered PSD and return its manifest. Arrays are BGR (cv2).

    ``typesetting_mode``:
      * ``on``  -> Original(hidden), Cleaned(visible), Text/Region NNN(visible),
                   Translated Preview(hidden, reference). Opening the PSD shows the
                   translated result as independent, per-region editable raster layers.
      * ``off`` -> Original(hidden), Cleaned(visible) only. No translated glyphs are
                   rasterized onto the page; the reviewer typesets manually. Region
                   data is still fully preserved in the manifest.
    """
    try:
        from psd_tools import PSDImage
        from psd_tools.api.layers import PixelLayer
    except ImportError as exc:
        raise ProfessionalPsdError("psd_library_unavailable") from exc

    mode = str(typesetting_mode or "on").casefold()
    if mode not in {"on", "off"}:
        raise ProfessionalPsdError("invalid_typesetting_mode")

    height, width = translated_bgr.shape[:2]
    if (original_bgr.shape[:2] != (height, width)
            or cleaned_bgr.shape[:2] != (height, width)):
        raise ProfessionalPsdError("psd_dimension_mismatch")

    psd = PSDImage.new("RGBA", (width, height))
    layers_manifest: list[dict[str, Any]] = []

    def add(layer, name, role, **extra):
        psd.append(layer)
        layers_manifest.append({"name": name, "type": "pixel", "role": role, **extra})

    # Bottom -> top. psd-tools appends the first layer at the bottom.
    original_pil = _bgr_to_rgba_pil(original_bgr)
    original_layer = PixelLayer.frompil(original_pil, psd, layer_name="Original")
    original_layer.visible = False  # reference only, hidden by default
    add(original_layer, "Original", "original")
    original_pil.close()

    cleaned_pil = _bgr_to_rgba_pil(cleaned_bgr)
    cleaned_layer = PixelLayer.frompil(cleaned_pil, psd, layer_name="Cleaned")
    cleaned_layer.visible = True
    add(cleaned_layer, "Cleaned", "cleaned")
    cleaned_pil.close()

    ordered = _sorted_regions(regions)
    layer_number = 0
    if mode == "on":
        for region in ordered:
            if not _would_render(region):
                continue  # IGNORE / inactive / no target -> no layer (P2 §10)
            box = _clamp_box(region.get("bbox") or [], width, height)
            if box is None:
                continue
            layer_img = _region_layer_rgba(translated_bgr, cleaned_bgr, box)
            if layer_img is None:
                continue
            layer_number += 1
            name = f"Text/Region {layer_number:03d}"
            x, y, _, _ = box
            region_layer = PixelLayer.frompil(layer_img, psd, layer_name=name, top=y, left=x)
            region_layer.visible = True
            add(region_layer, name, "translated_region",
                region_id=str(region.get("region_id") or ""), page_index=page_index,
                bbox=list(box), manual=bool(region.get("manual")),
                region_type=str(region.get("region_type") or ""),
                effective_target=str(region.get("target") or ""))
            layer_img.close()

    include_preview = mode == "on"
    if include_preview:
        preview_pil = _bgr_to_rgba_pil(translated_bgr)
        preview_layer = PixelLayer.frompil(preview_pil, psd, layer_name="Translated Preview")
        preview_layer.visible = False  # hidden reference; avoids double-compositing region text
        add(preview_layer, "Translated Preview", "translated_preview")
        preview_pil.close()

    temp_file = dest_path.with_name(f".{dest_path.name}.{os.getpid()}.tmp")
    try:
        _save_without_forced_composite(psd, temp_file, translated_bgr if mode == "on" else cleaned_bgr)
        _validate_layered_psd(temp_file, width, height, [m["name"] for m in layers_manifest])
        os.replace(temp_file, dest_path)
    except ProfessionalPsdError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ProfessionalPsdError("psd_save_failed") from exc
    finally:
        try:
            temp_file.unlink()
        except OSError:
            pass
    return {
        "page_index": page_index, "width": width, "height": height,
        "layers": layers_manifest,
        "regions": [_region_metadata(r, page_index) for r in ordered],
        "region_layer_count": layer_number,
        "editable_text_supported": False,
        "typesetting_mode": mode,
        "psd_text_mode": "RASTER_PER_REGION",
        "translated_preview_included": include_preview,
        "translated_region_layers_included": mode == "on",
        "cleaned_included": True,
        "original_included": True,
    }


def _save_without_forced_composite(psd, dest_path: Path, merged_bgr: np.ndarray) -> None:
    """Save the PSD without psd-tools' save-time full-canvas re-composite.

    ``PSDImage.save`` calls ``composite(force=True)`` over every full-page layer to
    build the merged/preview image_data — the single dominant memory spike on tall
    pages (measured ~3.7 GB at 30k). We already hold the flattened page, so we build
    the layer records once, set the merged image_data from that page directly, and
    clear the updated flag so ``save`` serializes the layers without re-compositing.

    This uses psd-tools record internals (``_update_record`` / ``_record.image_data`` /
    ``_updated_layers``), verified against the pinned ``psd-tools==1.10.9`` in
    requirements-beta.txt. It is guarded: if those attributes are ever absent (a version
    change), it falls back to the library's own ``save`` with no loss of correctness.
    """
    try:
        update_record = psd._update_record
        record = psd._record
        image_data = record.image_data
        header = record.header
        set_data = image_data.set_data
    except AttributeError:
        psd.save(dest_path)
        return
    update_record()  # build layer records/channels once (per-layer, no cross-layer composite)
    merged = _bgr_to_rgba_pil(merged_bgr)
    try:
        set_data([channel.tobytes() for channel in merged.split()], header)
    finally:
        merged.close()
    psd._updated_layers = False  # save() now skips its composite(force=True)
    psd.save(dest_path)


def _validate_layered_psd(path: Path, width: int, height: int, expected_names: list[str]) -> None:
    """Lightweight structural validation (no full-page tobytes compares — memory-bounded)."""
    try:
        from psd_tools import PSDImage
    except ImportError as exc:
        raise ProfessionalPsdError("psd_library_unavailable") from exc
    try:
        reopened = PSDImage.open(path)
    except Exception as exc:  # noqa: BLE001
        raise ProfessionalPsdError("psd_reopen_failed") from exc
    if tuple(reopened.size) != (width, height):
        raise ProfessionalPsdError("psd_canvas_dimension_mismatch")
    names = [layer.name for layer in reopened]
    if names != expected_names:
        raise ProfessionalPsdError("psd_layer_structure_invalid")
    if not all(layer.kind == "pixel" for layer in reopened):
        raise ProfessionalPsdError("psd_non_pixel_layer")


def export_professional_psd(
    pages: Iterable[dict[str, Any]], output_root: str | Path, chapter_name: str,
    *, cancel: Callable[[], bool] | None = None, typesetting_mode: str = "on",
) -> Path:
    """Atomically publish one layered PSD per page + a chapter manifest.

    Each page dict: {page_index, original_bgr, cleaned_bgr, translated_bgr, regions}.
    Pages are written and released one at a time; no multi-page PSD is held in RAM.
    """
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_name(chapter_name)
    destination = _safe_destination(root, safe_name)
    temp = Path(tempfile.mkdtemp(prefix=f".{safe_name}.", dir=str(root)))
    manifests: list[dict[str, Any]] = []
    try:
        # Iterate lazily so a multi-page chapter never holds all page arrays in RAM:
        # each page dict (its original/cleaned/translated buffers) is produced, written
        # and released one at a time (P3 §21). Padding is fixed (supports 9999 pages).
        pad = 4
        for index, page in enumerate(pages, 1):
            if cancel and cancel():
                raise ProfessionalPsdError("cancelled")
            target = temp / f"{index:0{pad}d}.psd"
            manifest = write_professional_page_psd(
                target,
                original_bgr=page["original_bgr"], cleaned_bgr=page["cleaned_bgr"],
                translated_bgr=page["translated_bgr"], regions=page.get("regions") or [],
                page_index=int(page.get("page_index") or index),
                typesetting_mode=typesetting_mode,
            )
            manifest["file"] = target.name
            manifests.append(manifest)
        if not manifests:
            raise ProfessionalPsdError("no_final_pages")
        mode = str(typesetting_mode or "on").casefold()
        (temp / "psd_manifest.json").write_text(
            json.dumps({"chapter": safe_name, "editable_text_supported": False,
                        "typesetting_mode": mode, "psd_text_mode": "RASTER_PER_REGION",
                        "translated_region_layers_included": mode == "on",
                        "translated_preview_included": mode == "on",
                        "cleaned_included": True, "original_included": True,
                        "pages": manifests}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        if cancel and cancel():
            raise ProfessionalPsdError("cancelled")
        os.replace(temp, destination)
        return destination
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
