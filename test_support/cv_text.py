"""OpenCV-5-compatible synthetic text drawing for test fixtures.

OpenCV 5 rewrote the Hershey text renderer and ``cv2.putText`` no longer
honours ``thickness`` the way the fixtures in this repo were written against.

OpenCV 4 stroked the glyph skeletons as polylines ``thickness`` pixels wide,
exactly like ``cv2.line`` and independent of ``fontScale``.  OpenCV 5 makes the
stroke a function of ``fontScale`` and lets ``thickness`` act only as a weak
multiplier that **saturates at 2** -- every thickness >= 2 renders
byte-identical output.  Measured stroke width in pixels, FONT_HERSHEY_SIMPLEX:

    fontScale         0.6  0.72  1.0  2.0  2.6  9.0
    OpenCV 4, any t     thickness, at every fontScale
    OpenCV 5, t=1       2     2    3    6    8    26
    OpenCV 5, t>=2      3     3    5    9   12    41

Fixtures here use ``thickness`` to model real lettering weight -- a heavy
outline drawn under a lighter glyph body, hairline display lettering, or body
text of a given weight inside a balloon.  Under OpenCV 5 those intents break in
both directions: thick outlines collapse to the body's width and vanish
entirely, while text meant to be fine renders far heavier than asked and pushes
the region classifier's texture and ink-ratio metrics across thresholds
calibrated against real lettering.

This helper restores the OpenCV 4 contract by doing what OpenCV 4 did: recover
the glyph skeleton, then stroke it to the requested width.  The skeleton comes
from the ridge of the distance transform, which is stable regardless of how
thick the installed renderer chose to draw.  (Eroding the thick render down to
the target width instead is not stable: at large fontScale the erosion radius
approaches the stroke half-width, so strokes drop out entirely wherever a curve
or a joint is a pixel narrower than the stems.)

On OpenCV 4 the native call is already correct, so we delegate to it untouched.

Only stroke weight is normalised.  Glyph shapes, spacing and origin remain
whatever the installed OpenCV produces.
"""

import cv2
import numpy as np

_NEEDS_STROKE_NORMALISATION = int(cv2.__version__.split(".")[0]) >= 5


def _stroke_radius(thickness):
    """Dilation radius reproducing OpenCV 4's stroke width for ``thickness``.

    Measured OpenCV 4 widths are 1, 3, 5, 7, 9, 11 and 21 pixels for
    thicknesses 1, 2, 3, 5, 7, 10 and 20, i.e. ``2 * ceil(t / 2) + 1`` for
    ``t >= 2`` and a single pixel for ``t == 1``.
    """
    return 0 if thickness <= 1 else (thickness + 1) // 2


_NEIGHBOURS = ((-1, 0), (-1, -1), (0, -1), (1, -1),
               (1, 0), (1, 1), (0, 1), (-1, 1))


def _skeleton(mask):
    """The glyph centreline, as a connected one-pixel-wide skeleton.

    Zhang-Suen thinning.  The cheaper alternative -- keeping the ridge of the
    distance transform -- is not usable here: on a wide stroke the ridge is a
    sparse set of isolated maxima rather than a line, so "HUGE" at fontScale 9
    comes out as 60 fragments instead of 4 glyphs, and a cleanup mask built
    from those fragments covers the whole region instead of the lettering.
    """
    image = (mask > 127).astype(np.uint8)
    while True:
        changed = False
        for step in (0, 1):
            p2, p3, p4, p5, p6, p7, p8, p9 = [
                np.roll(np.roll(image, dy, 0), dx, 1) for dy, dx in _NEIGHBOURS]
            filled = p2 + p3 + p4 + p5 + p6 + p7 + p8 + p9
            ring = (p2, p3, p4, p5, p6, p7, p8, p9, p2)
            transitions = sum(
                ((ring[i] == 0) & (ring[i + 1] == 1)).astype(np.uint8)
                for i in range(8))
            if step == 0:
                corners = (p2 * p4 * p6 == 0) & (p4 * p6 * p8 == 0)
            else:
                corners = (p2 * p4 * p8 == 0) & (p2 * p6 * p8 == 0)
            removable = ((image == 1) & (filled >= 2) & (filled <= 6)
                         & (transitions == 1) & corners)
            if removable.any():
                image[removable] = 0
                changed = True
        if not changed:
            return image * 255


def putText(image, text, org, fontFace, fontScale, color, thickness=1,
            lineType=cv2.LINE_8, bottomLeftOrigin=False):
    """Drop-in ``cv2.putText`` that honours ``thickness`` as OpenCV 4 did.

    Draws in place and returns ``image``, like ``cv2.putText``.
    """
    thickness = int(thickness)
    if not _NEEDS_STROKE_NORMALISATION:
        cv2.putText(image, text, org, fontFace, fontScale, color, thickness,
                    lineType, bottomLeftOrigin)
        return image

    drawn = np.zeros(image.shape[:2], dtype=np.uint8)
    cv2.putText(drawn, text, org, fontFace, fontScale, 255, 1, lineType,
                bottomLeftOrigin)
    alpha = _skeleton(np.where(drawn > 127, np.uint8(255), np.uint8(0)))

    radius = _stroke_radius(thickness)
    if radius:
        alpha = cv2.dilate(alpha, cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1)))

    weight = alpha.astype(np.float32) / 255.0
    values = np.asarray(color, dtype=np.float32).ravel()
    if image.ndim == 3:
        weight = weight[..., None]
        values = np.resize(values, image.shape[2])
    else:
        values = values[:1]
    blended = image.astype(np.float32) * (1.0 - weight) + values * weight
    image[...] = np.clip(blended, 0, 255).astype(image.dtype)
    return image
