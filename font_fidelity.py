"""Local font fidelity checks for human preview rendering.

This module never downloads fonts and never calls a provider.  It resolves only
the renderer's locally available font candidates, records the actual file that
Pillow opened, and scores rendered rasters from pixels rather than from a font
name.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_FIDELITY_VERSION = "1.0"

ROLE_FONT_FILES = {
    "decorative": ("georgia.ttf", "georgiab.ttf", "calibril.ttf"),
    "shout": ("arialbi.ttf", "ariali.ttf", "segoeuii.ttf"),
    "regular": ("segoeui.ttf", "calibri.ttf", "arial.ttf"),
}


def _fonts_dir() -> Path | None:
    root = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    if not root:
        return None
    candidate = Path(root) / "Fonts"
    return candidate if candidate.is_dir() else None


def role_font_paths(role: str) -> list[str]:
    fonts_dir = _fonts_dir()
    names = ROLE_FONT_FILES.get(str(role or "").lower(), ROLE_FONT_FILES["regular"])
    paths: list[str] = []
    for name in names:
        paths.append(str(fonts_dir / name) if fonts_dir else name)
    return paths


def candidate_font_paths(role: str, configured_font_path: str | None = None, *,
                         prefer_role: bool = False) -> list[str]:
    configured = [str(configured_font_path)] if configured_font_path else []
    role_paths = role_font_paths(role)
    return role_paths + configured if prefer_role else configured + role_paths


def file_sha256(path: str | Path) -> str:
    try:
        h = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _glyph_support(font: ImageFont.ImageFont, text: str) -> dict[str, Any]:
    missing: list[str] = []
    checked: list[str] = []
    for char in dict.fromkeys(str(text or "")):
        if char.isspace():
            continue
        checked.append(char)
        try:
            bbox = font.getmask(char).getbbox()
        except Exception:
            bbox = None
        if bbox is None:
            missing.append(char)
    return {
        "status": "complete" if not missing else "missing_glyphs",
        "checked_count": len(checked),
        "missing_count": len(missing),
        "missing_chars": missing,
    }


def resolve_font(role: str, size: int, *, configured_font_path: str | None = None,
                 prefer_role: bool = False, text: str = "") -> tuple[ImageFont.ImageFont, dict[str, Any]]:
    requested = str(role or "regular").lower()
    candidates = candidate_font_paths(requested, configured_font_path, prefer_role=prefer_role)
    errors: list[dict[str, str]] = []
    configured_missing = bool(configured_font_path and not Path(configured_font_path).is_file())
    for path in candidates:
        if not path:
            continue
        try:
            if Path(path).is_file():
                font = ImageFont.truetype(path, int(size))
                identity = Path(path).stem.lower()
                used_configured_fallback = bool(configured_missing and not prefer_role)
                runtime = {
                    "font_fidelity_version": FONT_FIDELITY_VERSION,
                    "requested_font": requested,
                    "resolved_font_path": str(Path(path).resolve()),
                    "font_file_hash": file_sha256(path),
                    "font_load_success": True,
                    "font_load_error": "",
                    "fallback_used": used_configured_fallback,
                    "fallback_reason": "configured_font_unavailable" if used_configured_fallback else "",
                    "actual_font_identity": identity,
                    "glyph_support": _glyph_support(font, text),
                    "prefer_role": bool(prefer_role),
                    "candidate_paths_checked": candidates,
                    "candidate_errors": ([{"path": str(configured_font_path),
                                           "error": "configured_font_unavailable"}]
                                         if configured_missing else []),
                }
                try:
                    setattr(font, "tradutor_font_runtime", runtime)
                except Exception:
                    pass
                return font, runtime
        except Exception as exc:  # pragma: no cover - depends on local font parser
            errors.append({"path": path, "error": type(exc).__name__})
    try:
        font = ImageFont.truetype("arial.ttf", int(size))
        runtime = {
            "font_fidelity_version": FONT_FIDELITY_VERSION,
            "requested_font": requested,
            "resolved_font_path": "arial.ttf",
            "font_file_hash": "",
            "font_load_success": True,
            "font_load_error": "",
            "fallback_used": True,
            "fallback_reason": "role_font_files_unavailable",
            "actual_font_identity": "arial",
            "glyph_support": _glyph_support(font, text),
            "prefer_role": bool(prefer_role),
            "candidate_paths_checked": candidates,
            "candidate_errors": errors,
        }
    except Exception as exc:
        font = ImageFont.load_default()
        runtime = {
            "font_fidelity_version": FONT_FIDELITY_VERSION,
            "requested_font": requested,
            "resolved_font_path": "",
            "font_file_hash": "",
            "font_load_success": False,
            "font_load_error": type(exc).__name__,
            "fallback_used": True,
            "fallback_reason": "default_pillow_font",
            "actual_font_identity": "pillow_default",
            "glyph_support": _glyph_support(font, text),
            "prefer_role": bool(prefer_role),
            "candidate_paths_checked": candidates,
            "candidate_errors": errors,
        }
    try:
        setattr(font, "tradutor_font_runtime", runtime)
    except Exception:
        pass
    return font, runtime


def render_text_raster(text: str, font: ImageFont.ImageFont, *, stroke_width: int = 0,
                       slant_degrees: float = 0.0) -> np.ndarray:
    probe = Image.new("L", (16, 16), 0)
    draw = ImageDraw.Draw(probe)
    bbox = draw.textbbox((0, 0), str(text or ""), font=font, stroke_width=stroke_width)
    width = max(8, bbox[2] - bbox[0] + stroke_width * 4 + 8)
    height = max(8, bbox[3] - bbox[1] + stroke_width * 4 + 8)
    img = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(img)
    draw.text((4 - bbox[0], 4 - bbox[1]), str(text or ""), font=font,
              fill=255, stroke_width=stroke_width, stroke_fill=255)
    if abs(float(slant_degrees or 0.0)) > 0.01:
        shear = float(slant_degrees) / 45.0 * 0.18
        xshift = abs(shear) * img.size[1]
        img = img.transform(
            (int(img.size[0] + xshift), img.size[1]),
            Image.AFFINE,
            (1, shear, -xshift if shear > 0 else 0, 0, 1, 0),
            resample=Image.BICUBIC,
        )
    return np.asarray(img)


def raster_fingerprint(raster: np.ndarray) -> dict[str, Any]:
    arr = np.asarray(raster)
    ink = arr > 16
    ys, xs = np.nonzero(ink)
    if xs.size == 0:
        return {"hash": hashlib.sha256(arr.tobytes()).hexdigest(), "ink_pixels": 0,
                "bbox": [], "density": 0.0, "width": int(arr.shape[1]), "height": int(arr.shape[0])}
    bbox = [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]
    crop = arr[bbox[1]:bbox[3], bbox[0]:bbox[2]]
    return {
        "hash": hashlib.sha256(crop.tobytes()).hexdigest(),
        "ink_pixels": int(ink.sum()),
        "bbox": bbox,
        "density": round(float(ink.sum()) / max(1, crop.size), 4),
        "width": int(crop.shape[1]),
        "height": int(crop.shape[0]),
        "horizontal_profile_hash": hashlib.sha256(crop.sum(axis=0).astype(np.int64).tobytes()).hexdigest(),
        "vertical_profile_hash": hashlib.sha256(crop.sum(axis=1).astype(np.int64).tobytes()).hexdigest(),
    }


def style_profile_from_raster(raster: np.ndarray) -> dict[str, Any]:
    arr = np.asarray(raster)
    if arr.ndim == 3:
        arr = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
    ink = arr > 16
    ys, xs = np.nonzero(ink)
    if xs.size == 0:
        return {"available": False, "reason_codes": ["no_ink"]}
    width = int(xs.max() - xs.min() + 1)
    height = int(ys.max() - ys.min() + 1)
    density = float(ink.sum()) / max(1, width * height)
    dist = cv2.distanceTransform(ink.astype(np.uint8), cv2.DIST_L2, 3)
    stroke = float(np.percentile(dist[dist > 0], 70) * 2.0) if np.any(dist > 0) else 0.0
    centered_x = xs.astype(np.float32) - float(xs.mean())
    centered_y = ys.astype(np.float32) - float(ys.mean())
    denom = float((centered_y ** 2).sum()) or 1.0
    slant = float((centered_x * centered_y).sum() / denom)
    return {
        "available": True,
        "width": width,
        "height": height,
        "density": round(density, 4),
        "stroke_width": round(stroke, 3),
        "condensation_ratio": round(width / max(1, height), 3),
        "slant": round(slant, 3),
        "ink_pixels": int(ink.sum()),
    }


def compare_style_profiles(reference: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    if not reference.get("available") or not candidate.get("available"):
        return {"style_similarity": 0.0, "reason_codes": ["style_profile_unavailable"]}

    def sim(key: str, scale: float) -> float:
        return max(0.0, 1.0 - abs(float(reference.get(key) or 0.0)
                                  - float(candidate.get(key) or 0.0)) / scale)

    stroke = sim("stroke_width", 8.0)
    slant = sim("slant", 1.2)
    condensation = sim("condensation_ratio", 6.0)
    density = sim("density", 0.8)
    style = stroke * 0.35 + slant * 0.2 + condensation * 0.25 + density * 0.2
    return {
        "style_similarity": round(style, 3),
        "stroke_similarity": round(stroke, 3),
        "slant_similarity": round(slant, 3),
        "condensation_similarity": round(condensation, 3),
        "spacing_similarity": round(condensation, 3),
        "alignment_similarity": 1.0,
        "reason_codes": [],
    }


def score_font_candidates(reference_raster: np.ndarray, text: str, *,
                          configured_font_path: str | None = None,
                          roles: tuple[str, ...] = ("regular", "shout", "decorative"),
                          sizes: tuple[int, ...] = (18, 22, 26, 30, 34, 38),
                          slants: tuple[float, ...] = (0.0, -8.0, 8.0)) -> list[dict[str, Any]]:
    reference_profile = style_profile_from_raster(reference_raster)
    scored: list[dict[str, Any]] = []
    for role in roles:
        best: dict[str, Any] | None = None
        for size in sizes:
            font, runtime = resolve_font(role, size, configured_font_path=configured_font_path,
                                         prefer_role=True, text=text)
            for slant in slants:
                raster = render_text_raster(text, font, slant_degrees=slant)
                fp = raster_fingerprint(raster)
                profile = style_profile_from_raster(raster)
                score = compare_style_profiles(reference_profile, profile)
                record = {
                    "font_identity": runtime.get("actual_font_identity"),
                    "requested_font": role,
                    "resolved_font_path": runtime.get("resolved_font_path"),
                    "file_hash": runtime.get("font_file_hash"),
                    "glyph_support": runtime.get("glyph_support"),
                    "size": size,
                    "slant_degrees": slant,
                    "raster_fingerprint": fp,
                    **score,
                    "fit_score": 1.0,
                    "overall_score": round(float(score.get("style_similarity") or 0.0), 3),
                    "fallback_used": bool(runtime.get("fallback_used")),
                    "reason_codes": list(score.get("reason_codes") or [])
                    + (["font_fallback_detected"] if runtime.get("fallback_used") else []),
                }
                if best is None or record["overall_score"] > float(best.get("overall_score") or 0.0):
                    best = record
        if best:
            scored.append(best)
    scored.sort(key=lambda item: float(item.get("overall_score") or 0.0), reverse=True)
    return scored


def typography_gate(font_runtime: dict[str, Any], style_score: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    if not font_runtime.get("font_load_success"):
        reasons.append("font_load_failed")
    if font_runtime.get("fallback_used"):
        reasons.append("font_fallback_detected")
    if (font_runtime.get("glyph_support") or {}).get("status") != "complete":
        reasons.append("glyph_support_incomplete")
    if float(style_score.get("style_similarity") or 0.0) < 0.62:
        reasons.append("font_style_similarity_low")
    status = "passed" if not reasons else "needs_review"
    return {
        "gate_version": FONT_FIDELITY_VERSION,
        "status": status,
        "reason_codes": reasons,
        **{k: style_score.get(k) for k in (
            "style_similarity", "stroke_similarity", "slant_similarity",
            "condensation_similarity", "spacing_similarity", "alignment_similarity")},
        "font_load_success": bool(font_runtime.get("font_load_success")),
        "fallback_used": bool(font_runtime.get("fallback_used")),
        "glyph_support": (font_runtime.get("glyph_support") or {}).get("status", ""),
    }
