"""Measured previews and the visual gate for a page draft.

Everything here is measurement, not judgement by inspection: a draft is only
approved by numbers a human can check. Nothing depends on a page, a region, a
phrase or a chapter — the same functions serve any draft.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np

GATE_VERSION = "2"

PASSED = "passed"
FAILED = "failed"
NEEDS_REVIEW = "needs_review"

# A pixel counts as changed when any channel moved by more than this. Encoders
# and resampling wobble by a level or two; real edits move far more.
CHANGE_TOLERANCE = 8
# How far outside the erase box to look for ink the cleanup could not reach.
RESIDUAL_RING_MARGIN = 12
# Ink is what stands out from the local background by at least this much.
INK_CONTRAST = 60
MIN_RESIDUAL_COMPONENT_AREA = 2
MAX_RESIDUAL_COMPONENT_AREA_RATIO = 0.08
MAX_SAFE_EXPANSION_FRACTION = 0.35
FONT_PROFILE_VERSION = "1"


def _read(path: str | Path) -> "np.ndarray | None":
    return cv2.imread(str(path))


def changed_mask(base: "np.ndarray", draft: "np.ndarray") -> "np.ndarray":
    return cv2.absdiff(base, draft).max(axis=2) > CHANGE_TOLERANCE


def box_mask(shape: tuple[int, int], boxes: list[tuple[int, int, int, int]]) -> "np.ndarray":
    """A boolean mask of the areas a draft is allowed to change (x, y, w, h)."""
    mask = np.zeros(shape[:2], dtype=bool)
    height, width = shape[:2]
    for x, y, w, h in boxes:
        left, top = max(0, int(x)), max(0, int(y))
        right, bottom = min(width, int(x) + int(w)), min(height, int(y) + int(h))
        if right > left and bottom > top:
            mask[top:bottom, left:right] = True
    return mask


def _clip_ltrb(rect: tuple[int, int, int, int], shape: tuple[int, ...]) -> tuple[int, int, int, int] | None:
    height, width = shape[:2]
    left, top, right, bottom = rect
    left, top = max(0, int(left)), max(0, int(top))
    right, bottom = min(width, int(right)), min(height, int(bottom))
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _xywh_to_ltrb(box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x, y, w, h = (int(v) for v in box[:4])
    return x, y, x + w, y + h


def _ltrb_to_xywh(rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    left, top, right, bottom = (int(v) for v in rect[:4])
    return left, top, max(1, right - left), max(1, bottom - top)


def validation_halo_mask(shape: tuple[int, ...], box: tuple[int, int, int, int], *,
                         margin: int = RESIDUAL_RING_MARGIN) -> "np.ndarray":
    """Mask the validation halo around a region without granting edit permission."""
    mask = np.zeros(shape[:2], dtype=bool)
    inner = _clip_ltrb(_xywh_to_ltrb(box), shape)
    if inner is None:
        return mask
    left, top, right, bottom = inner
    outer = _clip_ltrb((left - margin, top - margin, right + margin, bottom + margin), shape)
    if outer is None:
        return mask
    outer_left, outer_top, outer_right, outer_bottom = outer
    mask[outer_top:outer_bottom, outer_left:outer_right] = True
    mask[top:bottom, left:right] = False
    return mask


def _component_records(mask: "np.ndarray", *, origin: tuple[int, int] = (0, 0)) -> list[dict[str, Any]]:
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
    records: list[dict[str, Any]] = []
    ox, oy = origin
    for label in range(1, count):
        area = int(stats[label, cv2.CC_STAT_AREA])
        if area < MIN_RESIDUAL_COMPONENT_AREA:
            continue
        x = int(stats[label, cv2.CC_STAT_LEFT]) + ox
        y = int(stats[label, cv2.CC_STAT_TOP]) + oy
        w = int(stats[label, cv2.CC_STAT_WIDTH])
        h = int(stats[label, cv2.CC_STAT_HEIGHT])
        records.append({
            "bounds": [x, y, x + w, y + h],
            "area": area,
            "width": w,
            "height": h,
            "aspect_ratio": round(w / max(1, h), 3),
        })
    return records


def _rects_intersect(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])


def detect_residual_source_ink(image: "np.ndarray", box: tuple[int, int, int, int], *,
                               validation_margin: int = RESIDUAL_RING_MARGIN,
                               protected_boxes: list[tuple[int, int, int, int]] | None = None,
                               contrast: int = INK_CONTRAST) -> dict[str, Any]:
    """Find source glyph ink immediately outside a region box.

    The detector is conservative: it only treats small, high-contrast connected
    components in the validation halo as residual glyph evidence. Larger or
    colliding components are reported as unsafe/ambiguous rather than erased.
    """
    inner = _clip_ltrb(_xywh_to_ltrb(box), image.shape)
    if inner is None:
        return {"residual_detected": False, "confidence": 0.0, "residual_pixel_count": 0,
                "residual_component_count": 0, "residual_components": [],
                "residual_bounds": [], "suggested_expansion": {"left": 0, "right": 0, "top": 0, "bottom": 0},
                "blocked_directions": [], "safe_to_expand": False, "needs_human_review": True,
                "reason_codes": ["invalid_region_box"], "evidence": {}}
    left, top, right, bottom = inner
    outer = _clip_ltrb((left - validation_margin, top - validation_margin,
                        right + validation_margin, bottom + validation_margin), image.shape)
    if outer is None:
        outer = inner
    outer_left, outer_top, outer_right, outer_bottom = outer
    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    halo = validation_halo_mask(image.shape, _ltrb_to_xywh(inner), margin=validation_margin)
    if not halo.any():
        return {"residual_detected": False, "confidence": 1.0, "residual_pixel_count": 0,
                "residual_component_count": 0, "residual_components": [],
                "residual_bounds": [], "suggested_expansion": {"left": 0, "right": 0, "top": 0, "bottom": 0},
                "blocked_directions": [], "safe_to_expand": True, "needs_human_review": False,
                "reason_codes": ["no_validation_halo"], "evidence": {"validation_margin": validation_margin}}

    sample = grey[halo]
    background = int(np.median(sample))
    ink = (np.abs(grey.astype(int) - background) > int(contrast)) & halo
    components = _component_records(ink[outer_top:outer_bottom, outer_left:outer_right],
                                    origin=(outer_left, outer_top))
    candidate_components = []
    ambiguous_components = []
    for component in components:
        c_left, c_top, c_right, c_bottom = component["bounds"]
        area = int(component["area"])
        box_area = max(1, (right - left) * (bottom - top))
        near = c_right >= left and c_left <= right and c_bottom >= top and c_top <= bottom
        compact = area / box_area <= MAX_RESIDUAL_COMPONENT_AREA_RATIO
        touches_outer = (c_left <= outer_left or c_top <= outer_top
                         or c_right >= outer_right or c_bottom >= outer_bottom)
        if near and compact and not touches_outer:
            candidate_components.append(component)
        elif area:
            ambiguous_components.append(component)

    expansions = {"left": 0, "right": 0, "top": 0, "bottom": 0}
    reason_codes: set[str] = set()
    if candidate_components:
        c_left = min(c["bounds"][0] for c in candidate_components)
        c_top = min(c["bounds"][1] for c in candidate_components)
        c_right = max(c["bounds"][2] for c in candidate_components)
        c_bottom = max(c["bounds"][3] for c in candidate_components)
        expansions = {
            "left": max(0, left - c_left),
            "right": max(0, c_right - right),
            "top": max(0, top - c_top),
            "bottom": max(0, c_bottom - bottom),
        }
        if expansions["left"]:
            reason_codes.add("residual_ink_left")
        if expansions["right"]:
            reason_codes.add("residual_ink_right")
        if expansions["top"]:
            reason_codes.add("residual_ink_top")
        if expansions["bottom"]:
            reason_codes.add("residual_ink_bottom")
        if len(candidate_components) == 1 and max(candidate_components[0]["width"], candidate_components[0]["height"]) <= validation_margin:
            reason_codes.add("punctuation_outside_box")
        else:
            reason_codes.add("glyph_extends_beyond_box")

    expanded = (left - expansions["left"], top - expansions["top"],
                right + expansions["right"], bottom + expansions["bottom"])
    expanded = _clip_ltrb(expanded, image.shape) or inner
    blocked: set[str] = set()
    protected = [tuple(int(v) for v in p[:4]) for p in (protected_boxes or [])]
    for protected_rect in protected:
        if _rects_intersect(expanded, protected_rect) and not _rects_intersect(inner, protected_rect):
            blocked.add("neighbor_region_collision")
    max_expansion_x = max(1, int((right - left) * MAX_SAFE_EXPANSION_FRACTION))
    max_expansion_y = max(1, int((bottom - top) * MAX_SAFE_EXPANSION_FRACTION))
    if expansions["left"] > max_expansion_x or expansions["right"] > max_expansion_x:
        blocked.add("unsafe_art_overlap")
    if expansions["top"] > max_expansion_y or expansions["bottom"] > max_expansion_y:
        blocked.add("unsafe_art_overlap")
    if ambiguous_components and not candidate_components:
        reason_codes.add("ambiguous_residual_component")
        blocked.add("ambiguous_residual_component")
    if blocked:
        reason_codes.update(blocked)

    residual_pixels = int(sum(c["area"] for c in candidate_components))
    residual_bounds = ([min(c["bounds"][0] for c in candidate_components),
                        min(c["bounds"][1] for c in candidate_components),
                        max(c["bounds"][2] for c in candidate_components),
                        max(c["bounds"][3] for c in candidate_components)]
                       if candidate_components else [])
    safe = bool(candidate_components) and not blocked
    confidence = 0.85 if safe else (0.35 if components else 1.0)
    return {
        "residual_detected": bool(candidate_components),
        "confidence": confidence,
        "residual_pixel_count": residual_pixels,
        "residual_component_count": len(candidate_components),
        "residual_components": candidate_components,
        "residual_bounds": residual_bounds,
        "suggested_expansion": expansions,
        "expanded_box": list(expanded),
        "expanded_box_xywh": list(_ltrb_to_xywh(expanded)),
        "original_mask_bounds": [left, top, right, bottom],
        "validation_halo_bounds": [outer_left, outer_top, outer_right, outer_bottom],
        "blocked_directions": sorted(blocked),
        "safe_to_expand": safe or not components,
        "needs_human_review": bool(candidate_components or ambiguous_components or blocked),
        "reason_codes": sorted(reason_codes) or ["no_residual_ink"],
        "evidence": {
            "background_level": background,
            "validation_margin": validation_margin,
            "candidate_components": len(candidate_components),
            "ambiguous_components": len(ambiguous_components),
            "raw_component_count": len(components),
        },
    }


def expand_box_using_residual_evidence(
    image: "np.ndarray",
    box: tuple[int, int, int, int],
    *,
    protected_boxes: list[tuple[int, int, int, int]] | None = None,
    validation_margin: int = RESIDUAL_RING_MARGIN,
) -> dict[str, Any]:
    """Return a minimal evidence-driven box expansion proposal."""
    residual = detect_residual_source_ink(
        image, box, validation_margin=validation_margin, protected_boxes=protected_boxes)
    original = _clip_ltrb(_xywh_to_ltrb(box), image.shape)
    if original is None:
        state = "unsafe_expansion"
        expanded = (0, 0, 1, 1)
    elif residual["residual_detected"] and residual["safe_to_expand"]:
        state = "safe_expansion_available"
        expanded = tuple(residual["expanded_box"])
    elif residual["residual_detected"]:
        state = "ambiguous_expansion"
        expanded = original
    else:
        state = "no_expansion_needed"
        expanded = original
    return {
        "state": state,
        "original_box": list(original or _xywh_to_ltrb(box)),
        "expanded_box": list(expanded),
        "original_box_xywh": list(box),
        "expanded_box_xywh": list(_ltrb_to_xywh(expanded)),
        "expansion": residual["suggested_expansion"],
        "residual_detection": residual,
    }


def extract_font_profile(image: "np.ndarray", box: tuple[int, int, int, int]) -> dict[str, Any]:
    """Measure generic visual traits of text already present in a region."""
    rect = _clip_ltrb(_xywh_to_ltrb(box), image.shape)
    if rect is None:
        return {"font_profile_version": FONT_PROFILE_VERSION, "available": False,
                "reason_codes": ["invalid_region_box"]}
    left, top, right, bottom = rect
    region = image[top:bottom, left:right]
    grey = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY) if region.ndim == 3 else region
    background = int(np.median(grey))
    ink = np.abs(grey.astype(int) - background) > INK_CONTRAST
    components = _component_records(ink)
    if not components:
        return {"font_profile_version": FONT_PROFILE_VERSION, "available": False,
                "reason_codes": ["no_source_ink"], "background_level": background}
    widths = [int(c["width"]) for c in components]
    heights = [int(c["height"]) for c in components]
    areas = [int(c["area"]) for c in components]
    glyph_width = float(np.median(widths))
    glyph_height = float(np.median(heights))
    bounds = [min(c["bounds"][0] for c in components) + left,
              min(c["bounds"][1] for c in components) + top,
              max(c["bounds"][2] for c in components) + left,
              max(c["bounds"][3] for c in components) + top]
    condensation = round(glyph_width / max(1.0, glyph_height), 3)
    weight = round(float(np.mean(areas)) / max(1.0, glyph_width * glyph_height), 3)
    return {
        "font_profile_version": FONT_PROFILE_VERSION,
        "available": True,
        "background_level": background,
        "glyph_count": len(components),
        "glyph_width_median": round(glyph_width, 3),
        "glyph_height_median": round(glyph_height, 3),
        "condensation_ratio": condensation,
        "weight_ratio": weight,
        "uppercase_likely": glyph_height >= max(8, (bottom - top) * 0.35),
        "ink_bounds": bounds,
        "reason_codes": [],
    }


def select_font_candidate(font_profile: dict[str, Any],
                          candidates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Pick the closest authorized font profile without chapter-specific rules."""
    default_candidates = [
        {"name": "regular", "condensation_ratio": 0.58, "weight_ratio": 0.42},
        {"name": "shout", "condensation_ratio": 0.50, "weight_ratio": 0.48},
        {"name": "decorative", "condensation_ratio": 0.62, "weight_ratio": 0.36},
    ]
    pool = candidates or default_candidates
    if not font_profile.get("available"):
        return {"selected_font": pool[0]["name"], "font_match_score": 0.0,
                "font_reason_codes": ["font_profile_unavailable"], "fallback_used": True,
                "font_candidates": pool}
    scored = []
    target_c = float(font_profile.get("condensation_ratio") or 0.0)
    target_w = float(font_profile.get("weight_ratio") or 0.0)
    for item in pool:
        dist = abs(float(item.get("condensation_ratio") or 0.0) - target_c) \
            + abs(float(item.get("weight_ratio") or 0.0) - target_w)
        scored.append((max(0.0, 1.0 - dist), item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    score, best = scored[0]
    reason_codes = ["font_match_low_confidence"] if score < 0.55 else []
    return {"selected_font": best["name"], "font_match_score": round(score, 3),
            "font_reason_codes": reason_codes, "fallback_used": False,
            "font_candidates": [{**item, "score": round(max(0.0, 1.0 - (
                abs(float(item.get("condensation_ratio") or 0.0) - target_c)
                + abs(float(item.get("weight_ratio") or 0.0) - target_w))), 3)}
                for item in pool]}


def measure_pixel_isolation(base: "np.ndarray", draft: "np.ndarray",
                            boxes: list[tuple[int, int, int, int]]) -> dict[str, Any]:
    """How many pixels moved, and whether any moved where they may not."""
    if base.shape != draft.shape:
        return {"dimensions_match": False, "base_dimensions": list(base.shape[:2]),
                "draft_dimensions": list(draft.shape[:2]),
                "changed_pixels_total": None, "changed_pixels_inside_mask": None,
                "changed_pixels_outside_mask": None}
    changed = changed_mask(base, draft)
    allowed = box_mask(base.shape, boxes)
    outside = changed & ~allowed
    ys, xs = np.nonzero(outside)
    return {
        "dimensions_match": True,
        "image_dimensions": [int(base.shape[1]), int(base.shape[0])],
        "changed_pixels_total": int(changed.sum()),
        "changed_pixels_inside_mask": int((changed & allowed).sum()),
        "changed_pixels_outside_mask": int(outside.sum()),
        # Where the leak is, so a failure can be looked at rather than guessed at.
        "outside_bounds": ([int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
                           if xs.size else []),
        "allowed_area": int(allowed.sum()),
    }


def measure_residual_ink(base: "np.ndarray", draft: "np.ndarray",
                         box: tuple[int, int, int, int], *,
                         margin: int = RESIDUAL_RING_MARGIN) -> dict[str, Any]:
    """Ink still sitting just outside the erase box.

    A recorded region box can be tighter than the glyphs actually drawn. When it
    is, cleaning inside the box leaves a sliver of the previous text on the page
    and the new line reads with a fragment of the old one beside it. Measured on
    the *draft*, because that is what a reader would see.
    """
    height, width = draft.shape[:2]
    x, y, w, h = (int(v) for v in box[:4])
    left, top = max(0, x), max(0, y)
    right, bottom = min(width, x + w), min(height, y + h)
    outer_left, outer_top = max(0, left - margin), max(0, top - margin)
    outer_right, outer_bottom = min(width, right + margin), min(height, bottom + margin)

    grey = cv2.cvtColor(draft, cv2.COLOR_BGR2GRAY)
    ring = np.zeros(grey.shape, dtype=bool)
    ring[outer_top:outer_bottom, outer_left:outer_right] = True
    ring[top:bottom, left:right] = False
    if not ring.any():
        return {"ring_pixels": 0, "residual_ink_pixels": 0, "residual_ink_ratio": 0.0,
                "background_level": None, "margin": margin}

    # The local background is what most of the ring is; ink is what departs from it.
    values = grey[ring]
    background = int(np.median(values))
    residual = int((np.abs(values.astype(int) - background) > INK_CONTRAST).sum())
    return {
        "ring_pixels": int(ring.sum()),
        "residual_ink_pixels": residual,
        "residual_ink_ratio": round(residual / max(1, int(ring.sum())), 5),
        "background_level": background,
        "margin": margin,
    }


def measure_text_fit(draft: "np.ndarray", box: tuple[int, int, int, int]) -> dict[str, Any]:
    """Whether the drawn text stays inside its box and keeps a margin."""
    height, width = draft.shape[:2]
    x, y, w, h = (int(v) for v in box[:4])
    left, top = max(0, x), max(0, y)
    right, bottom = min(width, x + w), min(height, y + h)
    region = cv2.cvtColor(draft[top:bottom, left:right], cv2.COLOR_BGR2GRAY)
    if region.size == 0:
        return {"text_fit": False, "reason": "empty_region"}
    background = int(np.median(region))
    ink = np.abs(region.astype(int) - background) > INK_CONTRAST
    if not ink.any():
        return {"text_fit": False, "reason": "no_text_drawn", "ink_pixels": 0}
    ys, xs = np.nonzero(ink)
    # Touching the very edge means the glyphs were clipped by the box.
    touches = bool(xs.min() == 0 or ys.min() == 0
                   or xs.max() == region.shape[1] - 1 or ys.max() == region.shape[0] - 1)
    return {
        "text_fit": not touches,
        "reason": "clipped_at_region_edge" if touches else "",
        "ink_pixels": int(ink.sum()),
        "ink_bounds": [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())],
        "region_size": [int(region.shape[1]), int(region.shape[0])],
        "contrast": int(np.abs(region[ink].astype(int) - background).mean()),
    }


def evaluate_visual_gate(base_path: str | Path, draft_path: str | Path, *,
                         boxes: list[tuple[int, int, int, int]]) -> dict[str, Any]:
    """The whole visual verdict for one draft, with the numbers behind it."""
    reasons: list[str] = []
    base, draft = _read(base_path), _read(draft_path)
    if base is None or draft is None:
        return {"status": FAILED, "reason_codes": ["draft_image_unavailable"],
                "gate_version": GATE_VERSION}

    isolation = measure_pixel_isolation(base, draft, boxes)
    if not isolation["dimensions_match"]:
        return {"status": FAILED, "reason_codes": ["image_dimension_mismatch"],
                "isolation": isolation, "gate_version": GATE_VERSION}

    # The one hard rule: a draft may not change a pixel outside its own region.
    if isolation["changed_pixels_outside_mask"]:
        reasons.append("pixels_changed_outside_mask")
    if not isolation["changed_pixels_inside_mask"]:
        reasons.append("nothing_changed_inside_mask")

    fits, residuals = [], []
    for box in boxes:
        fit = measure_text_fit(draft, box)
        fits.append({"box": list(box), **fit})
        if not fit["text_fit"]:
            reasons.append("text_fit_failed" if fit.get("reason") != "no_text_drawn"
                           else "no_text_drawn")
        residual = measure_residual_ink(base, draft, box)
        residuals.append({"box": list(box), **residual})
        if residual["residual_ink_pixels"]:
            # Not fatal on its own: the neighbouring art can legitimately have
            # ink. It always needs a human to look.
            reasons.append("residual_ink_outside_mask")
        source_residual = detect_residual_source_ink(draft, box)
        residuals[-1]["source_ink_detector"] = source_residual
        residuals[-1]["residual_source_pixels"] = int(source_residual["residual_pixel_count"])
        residuals[-1]["residual_component_count"] = int(source_residual["residual_component_count"])
        residuals[-1]["validation_halo_bounds"] = source_residual.get("validation_halo_bounds", [])
        if source_residual["residual_pixel_count"]:
            reasons.append("residual_source_ink_inside_validation_halo")

    hard = {"pixels_changed_outside_mask", "image_dimension_mismatch",
            "nothing_changed_inside_mask", "no_text_drawn", "text_fit_failed"}
    if any(r in hard for r in reasons):
        status = FAILED
    elif reasons:
        status = NEEDS_REVIEW
    else:
        status = PASSED
    residual_source_pixels = int(sum(item.get("residual_source_pixels") or 0 for item in residuals))
    residual_component_count = int(sum(item.get("residual_component_count") or 0 for item in residuals))
    return {"status": status, "reason_codes": sorted(set(reasons)),
            "isolation": isolation, "text_fit": fits, "residual_ink": residuals,
            "change_mask": {"boxes": [list(b) for b in boxes],
                            "changed_pixels_outside_change_mask": isolation["changed_pixels_outside_mask"]},
            "validation_halo": {"boxes": [r.get("validation_halo_bounds", []) for r in residuals],
                                "residual_source_pixels": residual_source_pixels,
                                "residual_component_count": residual_component_count},
            "residual_source_pixels": residual_source_pixels,
            "residual_component_count": residual_component_count,
            "gate_version": GATE_VERSION}


def write_previews(base_path: str | Path, draft_path: str | Path, *,
                   box: tuple[int, int, int, int], out_dir: str | Path,
                   stem: str, margin: int = 24) -> dict[str, str]:
    """Full pages, before/after crops, a diff and an overlay for one draft."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    base, draft = _read(base_path), _read(draft_path)
    if base is None or draft is None:
        return {}
    height, width = base.shape[:2]
    x, y, w, h = (int(v) for v in box[:4])
    left, top = max(0, x - margin), max(0, y - margin)
    right, bottom = min(width, x + w + margin), min(height, y + h + margin)
    before, after = base[top:bottom, left:right], draft[top:bottom, left:right]

    diff = cv2.absdiff(base, draft)
    heat = cv2.applyColorMap(cv2.convertScaleAbs(diff.max(axis=2), alpha=6), cv2.COLORMAP_JET)
    overlay = cv2.addWeighted(draft, 0.6, heat, 0.4, 0)

    files = {
        f"{stem}_preview.png": draft,
        f"{stem}_base.png": base,
        f"{stem}_region_before.png": before,
        f"{stem}_region_after.png": after,
        f"{stem}_diff.png": heat[top:bottom, left:right],
        f"{stem}_overlay.png": overlay[top:bottom, left:right],
        # Side by side, so the two states are read in one glance.
        f"{stem}_side_by_side.png": np.hstack([before, after])
        if before.shape == after.shape else before,
    }
    written = {}
    for name, image in files.items():
        path = out / name
        cv2.imwrite(str(path), image)
        written[name] = str(path)
    return written
