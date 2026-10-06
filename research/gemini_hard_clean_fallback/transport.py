"""Local transport contracts for the Gemini fallback.

This module deliberately contains no browser or credential handling.  It makes
the file boundary explicit so a live browser adapter can fail closed before
uploading, and so downloaded/blob bytes are validated before entering the
image pipeline.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PIL import Image


@dataclass(frozen=True)
class ImageDiagnostics:
    path: str
    exists: bool
    file_size_bytes: int
    sha256: str | None
    extension: str
    mime_expected: str
    format: str | None
    width: int | None
    height: int | None
    mode: str | None
    channels: int | None
    alpha_present: bool | None
    pil_decode: str
    secondary_decode: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


MIME_BY_SUFFIX = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _secondary_decode(path: Path) -> str:
    try:
        import cv2  # type: ignore

        image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        return "PASS" if image is not None and image.size else "FAIL"
    except Exception:
        try:
            import imageio.v3 as iio  # type: ignore

            image = iio.imread(path)
            return "PASS" if getattr(image, "size", 0) else "FAIL"
        except Exception:
            return "UNAVAILABLE"


def inspect_image(path: str | Path, secondary: bool = True) -> ImageDiagnostics:
    target = Path(path)
    suffix = target.suffix.lower()
    if not target.exists():
        return ImageDiagnostics(str(target), False, 0, None, suffix, MIME_BY_SUFFIX.get(suffix, "application/octet-stream"), None, None, None, None, None, None, "FAIL", "UNAVAILABLE", "file does not exist")
    size = target.stat().st_size
    digest = _sha256(target)
    try:
        with Image.open(target) as image:
            image.verify()
        with Image.open(target) as reopened:
            reopened.load()
            width, height = reopened.size
            mode = reopened.mode
            channels = len(reopened.getbands())
            alpha = "A" in reopened.getbands()
            fmt = reopened.format
        return ImageDiagnostics(str(target), True, size, digest, suffix, MIME_BY_SUFFIX.get(suffix, "application/octet-stream"), fmt, width, height, mode, channels, alpha, "PASS", _secondary_decode(target) if secondary else "SKIPPED")
    except Exception as exc:
        return ImageDiagnostics(str(target), True, size, digest, suffix, MIME_BY_SUFFIX.get(suffix, "application/octet-stream"), None, None, None, None, None, "FAIL", _secondary_decode(target) if secondary else "SKIPPED", repr(exc))


def normalize_image_for_gemini(source: str | Path, destination_dir: str | Path, *, output_format: str = "PNG") -> ImageDiagnostics:
    """Decode, strip metadata/alpha, encode RGB, fsync, and re-validate."""
    source_path = Path(source)
    dest_dir = Path(destination_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = source_path.stem or "image"
    suffix = ".jpg" if output_format.upper() in {"JPG", "JPEG"} else ".png"
    destination = dest_dir / f"{stem}.normalized{suffix}"
    with Image.open(source_path) as image:
        image.load()
        rgb = image.convert("RGB")
        with tempfile.NamedTemporaryFile(dir=dest_dir, prefix=f".{stem}.", suffix=suffix, delete=False) as handle:
            temp_path = Path(handle.name)
        try:
            if suffix == ".jpg":
                rgb.save(temp_path, format="JPEG", quality=95, optimize=True)
            else:
                rgb.save(temp_path, format="PNG", optimize=True)
            with temp_path.open("r+b") as check:
                os.fsync(check.fileno())
            os.replace(temp_path, destination)
        finally:
            if temp_path.exists():
                temp_path.unlink()
    diagnostics = inspect_image(destination)
    if not diagnostics.exists or diagnostics.file_size_bytes <= 0 or diagnostics.pil_decode != "PASS":
        raise ValueError(f"normalized image failed validation: {diagnostics.to_dict()}")
    return diagnostics


def copy_to_short_path(source: str | Path, root: str | Path, job_id: str) -> tuple[Path, ImageDiagnostics]:
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    destination = root_path / f"{job_id}.png"
    shutil.copyfile(source, destination)
    diagnostics = inspect_image(destination)
    if diagnostics.pil_decode != "PASS" or diagnostics.file_size_bytes <= 0:
        raise ValueError(f"short path copy failed validation: {diagnostics.to_dict()}")
    return destination, diagnostics


def decode_output_bytes(data: bytes, destination: str | Path, *, source: str) -> ImageDiagnostics:
    if not data:
        raise ValueError(f"empty output bytes from {source}")
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temp = destination_path.with_suffix(destination_path.suffix + ".tmp")
    temp.write_bytes(data)
    os.replace(temp, destination_path)
    diagnostics = inspect_image(destination_path)
    if diagnostics.pil_decode != "PASS":
        raise ValueError(f"invalid output bytes from {source}: {diagnostics.to_dict()}")
    return diagnostics


def decode_data_url(data_url: str, destination: str | Path) -> ImageDiagnostics:
    header, encoded = data_url.split(",", 1)
    if ";base64" not in header:
        raise ValueError("only base64 data URLs are supported")
    return decode_output_bytes(base64.b64decode(encoded, validate=True), destination, source="DATA_URL")


def diagnostics_json(paths: list[str | Path]) -> dict[str, Any]:
    return {"images": [inspect_image(path).to_dict() for path in paths]}
