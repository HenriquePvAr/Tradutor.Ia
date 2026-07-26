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

FONT_FIDELITY_VERSION = "1.1"

ROLE_FONT_FILES = {
    "bold": ("arialbd.ttf", "calibrib.ttf", "seguisb.ttf", "trebucbd.ttf"),
    "decorative": ("georgia.ttf", "georgiab.ttf", "calibril.ttf"),
    "shout": ("arialbi.ttf", "ariali.ttf", "segoeuii.ttf"),
    "regular": ("segoeui.ttf", "calibri.ttf", "arial.ttf"),
}

# A generic allow-list of local fonts the renderer may consider for human visual
# previews.  It is intentionally not keyed by chapter/page/region/text.  Missing
# fonts are reported, not downloaded or copied.
AUTHORIZED_FONT_FILES = {
    "regular": ("segoeui.ttf", "calibri.ttf", "arial.ttf", "trebuc.ttf"),
    "bold": ("arialbd.ttf", "calibrib.ttf", "seguisb.ttf", "trebucbd.ttf"),
    "shout": ("arialbi.ttf", "ariali.ttf", "impact.ttf", "bahnschrift.ttf"),
    "decorative": ("georgia.ttf", "georgiab.ttf", "georgiai.ttf", "calibril.ttf"),
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


def authorized_font_inventory(*, text: str = "") -> list[dict[str, Any]]:
    """Return every available local font from the renderer allow-list.

    The result proves the actual file hash and glyph support.  It deliberately
    avoids exposing font bytes or relying on any operation-specific identifier.
    """
    fonts_dir = _fonts_dir()
    records: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for role, names in AUTHORIZED_FONT_FILES.items():
        for name in names:
            path = Path(fonts_dir / name) if fonts_dir else Path(name)
            resolved = str(path.resolve()) if path.is_file() else str(path)
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            record: dict[str, Any] = {
                "role": role,
                "font_name": Path(name).stem.lower(),
                "requested_file": name,
                "configured": bool(path.is_file()),
                "resolved_font_path": resolved if path.is_file() else "",
                "font_file_hash": file_sha256(path) if path.is_file() else "",
                "license_metadata": "local_system_font_allowlist",
                "fallback_possible": False,
                "font_load_success": False,
                "font_load_error": "",
                "glyph_support": {"status": "unchecked"},
            }
            if path.is_file():
                try:
                    font = ImageFont.truetype(str(path), 24)
                    record["font_load_success"] = True
                    record["glyph_support"] = _glyph_support(font, text)
                    try:
                        names_tuple = font.getname()
                        record["actual_font_identity"] = " ".join(
                            str(part) for part in names_tuple if part)
                    except Exception:
                        record["actual_font_identity"] = record["font_name"]
                    profile = style_profile_from_raster(render_text_raster(text or "ABC", font))
                    record["style"] = profile
                    record["weight"] = profile.get("stroke_width")
                    record["slant"] = profile.get("slant")
                    record["condensation"] = profile.get("condensation_ratio")
                except Exception as exc:  # pragma: no cover - parser depends on local files
                    record["font_load_error"] = type(exc).__name__
            records.append(record)
    return records


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
    stroke = float(np.percentile(dist[dist > 0].astype(np.float32), 70)) * 2.0 if np.any(dist > 0) else 0.0
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


def _fit_score(reference: dict[str, Any], candidate_fp: dict[str, Any], target_box: tuple[int, int] | None) -> float:
    if target_box:
        max_w, max_h = max(1, int(target_box[0])), max(1, int(target_box[1]))
    else:
        max_w = max(1, int(reference.get("width") or candidate_fp.get("width") or 1))
        max_h = max(1, int(reference.get("height") or candidate_fp.get("height") or 1))
    width = int(candidate_fp.get("width") or 0)
    height = int(candidate_fp.get("height") or 0)
    if width <= 0 or height <= 0:
        return 0.0
    width_fit = min(1.0, max_w / max(1, width))
    height_fit = min(1.0, max_h / max(1, height))
    overflow_penalty = 0.0 if width <= max_w and height <= max_h else 0.35
    return round(max(0.0, min(width_fit, height_fit) - overflow_penalty), 3)


def generate_typography_candidates(
    reference_raster: np.ndarray,
    text: str,
    *,
    max_candidates: int = 5,
    min_candidates: int = 3,
    target_box: tuple[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Rank real local font renderings for a human to choose from.

    The function only creates candidate metadata.  It does not persist a human
    choice and does not create a page revision.
    """
    reference_profile = style_profile_from_raster(reference_raster)
    if not reference_profile.get("available"):
        return []
    base_height = max(12, int(reference_profile.get("height") or 24))
    sizes = sorted({max(8, int(base_height * scale)) for scale in (0.42, 0.5, 0.6, 0.7, 0.78, 0.9, 1.0, 1.12)})
    slants = (-10.0, -5.0, 0.0, 5.0, 10.0)
    scored: list[dict[str, Any]] = []
    for font_record in authorized_font_inventory(text=text):
        if not font_record.get("font_load_success"):
            continue
        if (font_record.get("glyph_support") or {}).get("status") != "complete":
            continue
        path = str(font_record.get("resolved_font_path") or "")
        if not path:
            continue
        best_for_file: dict[str, Any] | None = None
        for size in sizes:
            try:
                font = ImageFont.truetype(path, size)
            except Exception:
                continue
            for slant in slants:
                raster = render_text_raster(text, font, slant_degrees=slant)
                fp = raster_fingerprint(raster)
                profile = style_profile_from_raster(raster)
                style = compare_style_profiles(reference_profile, profile)
                fit = _fit_score(reference_profile, fp, target_box)
                overall = round(
                    float(style.get("style_similarity") or 0.0) * 0.55
                    + fit * 0.45,
                    3,
                )
                reason_codes: list[str] = []
                if fit < 1.0:
                    reason_codes.append("candidate_fit_margin_low")
                if overall < 0.55:
                    reason_codes.append("font_style_similarity_low")
                record = {
                    "candidate_id": hashlib.sha256(
                        f"{font_record['font_file_hash']}:{size}:{slant}:{text}".encode("utf-8")
                    ).hexdigest()[:16],
                    "requested_font": font_record.get("role"),
                    "actual_font": font_record.get("actual_font_identity") or font_record.get("font_name"),
                    "font_path_hash": font_record.get("font_file_hash"),
                    "font_load_success": True,
                    "fallback_used": False,
                    "glyph_support": font_record.get("glyph_support"),
                    "font_size": size,
                    "tracking": 0,
                    "slant": slant,
                    "stroke_score": style.get("stroke_similarity"),
                    "condensation_score": style.get("condensation_similarity"),
                    "spacing_score": style.get("spacing_similarity"),
                    "fit_score": fit,
                    "style_score": style.get("style_similarity"),
                    "raster_similarity": style.get("style_similarity"),
                    "overall_score": overall,
                    "reason_codes": reason_codes,
                    "resolved_font_path": path,
                    "font_identity": font_record.get("font_name"),
                    "raster_fingerprint": fp,
                }
                if best_for_file is None or overall > float(best_for_file.get("overall_score") or 0.0):
                    best_for_file = record
        if best_for_file:
            scored.append(best_for_file)
    scored.sort(key=lambda item: float(item.get("overall_score") or 0.0), reverse=True)
    unique: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    for item in scored:
        key = str(item.get("font_path_hash") or item.get("actual_font") or item.get("candidate_id"))
        if key in seen_hashes:
            continue
        seen_hashes.add(key)
        item = dict(item)
        item["option_label"] = f"OPÇÃO {len(unique) + 1}"
        unique.append(item)
        if len(unique) >= max(1, int(max_candidates)):
            break
    if len(unique) < int(min_candidates):
        for item in unique:
            item.setdefault("reason_codes", []).append("font_candidate_count_below_requested_minimum")
    return unique


def select_delegated_typography_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Select a preview font only after explicit user delegation.

    The ranking is based on measured candidate properties.  It intentionally
    does not inspect page ids, region ids, chapter names, source text, or font
    names as special cases.
    """
    acceptable: list[dict[str, Any]] = []
    for candidate in candidates:
        glyph_status = (candidate.get("glyph_support") or {}).get("status")
        if not candidate.get("font_load_success"):
            continue
        if candidate.get("fallback_used"):
            continue
        if glyph_status != "complete":
            continue
        if float(candidate.get("fit_score") or 0.0) < 1.0:
            continue
        visual_score = (
            0.30 * float(candidate.get("style_score") or 0.0)
            + 0.22 * float(candidate.get("stroke_score") or 0.0)
            + 0.18 * float(candidate.get("condensation_score") or 0.0)
            + 0.12 * float(candidate.get("spacing_score") or 0.0)
            + 0.10 * float(candidate.get("raster_similarity") or 0.0)
            + 0.08 * float(candidate.get("fit_score") or 0.0)
        )
        item = dict(candidate)
        item["delegated_visual_score"] = round(visual_score, 4)
        acceptable.append(item)
    if not acceptable:
        return {
            "status": "no_acceptable_typography_candidate",
            "selected": {},
            "runner_up": {},
            "confidence": 0.0,
            "reason_codes": ["no_candidate_passed_safety_preconditions"],
        }
    acceptable.sort(
        key=lambda item: (
            float(item.get("delegated_visual_score") or 0.0),
            float(item.get("style_score") or 0.0),
            float(item.get("stroke_score") or 0.0),
            float(item.get("condensation_score") or 0.0),
            float(item.get("overall_score") or 0.0),
        ),
        reverse=True,
    )
    selected = acceptable[0]
    runner_up = acceptable[1] if len(acceptable) > 1 else {}
    delta = (
        float(selected.get("delegated_visual_score") or 0.0)
        - float(runner_up.get("delegated_visual_score") or 0.0)
        if runner_up else float(selected.get("delegated_visual_score") or 0.0)
    )
    confidence = max(0.0, min(1.0, 0.65 + delta))
    return {
        "status": "selected_for_preview_generation",
        "selected": selected,
        "runner_up": runner_up,
        "confidence": round(confidence, 3),
        "reason_codes": [
            "delegated_by_user",
            "font_load_success",
            "no_fallback",
            "complete_glyph_support",
            "text_fits_target",
            "best_measured_visual_match",
        ],
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
