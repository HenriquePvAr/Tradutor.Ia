"""Measured previews and the visual gate for a page draft (BLOCO 6C).

Everything here is measurement, not judgement by inspection: a draft is only
approved by numbers a human can check. Nothing depends on a page, a region, a
phrase or a chapter — the same functions serve any draft.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np

GATE_VERSION = "1"

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

    hard = {"pixels_changed_outside_mask", "image_dimension_mismatch",
            "nothing_changed_inside_mask", "no_text_drawn", "text_fit_failed"}
    if any(r in hard for r in reasons):
        status = FAILED
    elif reasons:
        status = NEEDS_REVIEW
    else:
        status = PASSED
    return {"status": status, "reason_codes": sorted(set(reasons)),
            "isolation": isolation, "text_fit": fits, "residual_ink": residuals,
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
