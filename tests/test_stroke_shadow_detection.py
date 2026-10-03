"""Conservative outline and drop-shadow detection from source lettering.

Shadow: an offset, semi-transparent copy of the glyph is detected; plain ink on a
clean balloon is not (negative control). Outline: the detector must never invent a
stroke on plain text (safety); it is intentionally low-recall rather than risk a
false outline on the page.
"""
from __future__ import annotations

import _test_bootstrap  # noqa: F401

import unittest

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

import ocr_balloon as ob


def _canvas(bg=(250, 250, 250), size=(220, 460)):
    return np.full((size[0], size[1], 3), bg, dtype=np.uint8)


def _outlined(fill_rgb, stroke_rgb, bg_rgb, *, sw=4, size=90):
    """A real outlined glyph via PIL stroke_width/stroke_fill; returns BGR."""
    img = Image.new("RGB", (460, 180), bg_rgb)
    try:
        font = ImageFont.truetype("arialbd.ttf", size)
    except OSError:
        font = ImageFont.load_default()
    ImageDraw.Draw(img).text((30, 40), "HELLO", font=font, fill=fill_rgb,
                             stroke_width=sw, stroke_fill=stroke_rgb)
    return np.array(img)[:, :, ::-1].copy()


def _dist(a, b):
    return max(abs(int(a[i]) - int(b[i])) for i in range(3))


BOX = (10, 40, 440, 170)
OUTLINE_BOX = (10, 20, 440, 150)


class DropShadowDetection(unittest.TestCase):
    def test_offset_shadow_is_detected(self):
        img = _canvas()
        # gray offset shadow first, then black glyph on top
        cv2.putText(img, "HI", (36, 146), cv2.FONT_HERSHEY_SIMPLEX, 3.0, (150, 150, 150), 6, cv2.LINE_AA)
        cv2.putText(img, "HI", (30, 140), cv2.FONT_HERSHEY_SIMPLEX, 3.0, (15, 15, 15), 6, cv2.LINE_AA)
        s = ob.extract_drop_shadow_profile(img, BOX)
        self.assertTrue(s["shadow_present"])
        self.assertGreaterEqual(s["confidence"], 0.5)
        dx, dy = s["shadow_offset"]
        self.assertGreater(dx, 0)
        self.assertGreater(dy, 0)

    def test_plain_text_has_no_shadow(self):
        img = _canvas()
        cv2.putText(img, "HI", (30, 140), cv2.FONT_HERSHEY_SIMPLEX, 3.0, (15, 15, 15), 6, cv2.LINE_AA)
        s = ob.extract_drop_shadow_profile(img, BOX)
        self.assertFalse(s["shadow_present"])

    def test_blank_region_has_no_shadow(self):
        img = _canvas()
        s = ob.extract_drop_shadow_profile(img, BOX)
        self.assertFalse(s["shadow_present"])


class OutlineDetection(unittest.TestCase):
    def test_white_fill_black_outline(self):
        prof = ob.extract_original_lettering_profile(
            _outlined((255, 255, 255), (0, 0, 0), (150, 150, 150)), None, OUTLINE_BOX)
        self.assertTrue(prof["stroke_present"])
        # fill ~ white, stroke ~ black (BGR)
        self.assertLessEqual(_dist(prof["dominant_bgr"], (255, 255, 255)), 30)
        self.assertLessEqual(_dist(prof["stroke_color"], (0, 0, 0)), 30)

    def test_black_fill_white_outline(self):
        prof = ob.extract_original_lettering_profile(
            _outlined((0, 0, 0), (255, 255, 255), (120, 120, 120)), None, OUTLINE_BOX)
        self.assertTrue(prof["stroke_present"])
        self.assertLessEqual(_dist(prof["dominant_bgr"], (0, 0, 0)), 30)
        self.assertLessEqual(_dist(prof["stroke_color"], (255, 255, 255)), 30)

    def test_red_fill_white_outline(self):
        prof = ob.extract_original_lettering_profile(
            _outlined((220, 20, 20), (255, 255, 255), (60, 60, 60)), None, OUTLINE_BOX)
        self.assertTrue(prof["stroke_present"])
        fb, sc = prof["dominant_bgr"], prof["stroke_color"]
        self.assertGreater(fb[2], 150)                 # red fill: high R
        self.assertLessEqual(_dist(sc, (255, 255, 255)), 40)  # white outline

    def test_plain_black_on_white_reports_no_stroke(self):
        img = _canvas()
        cv2.putText(img, "HI", (30, 140), cv2.FONT_HERSHEY_SIMPLEX, 3.0, (15, 15, 15), 6, cv2.LINE_AA)
        self.assertFalse(ob.extract_original_lettering_profile(img, None, BOX)["stroke_present"])

    def test_plain_white_on_dark_reports_no_stroke(self):
        img = _canvas(bg=(30, 30, 30))
        cv2.putText(img, "HI", (30, 140), cv2.FONT_HERSHEY_SIMPLEX, 3.0, (245, 245, 245), 6, cv2.LINE_AA)
        self.assertFalse(ob.extract_original_lettering_profile(img, None, BOX)["stroke_present"])


if __name__ == "__main__":
    unittest.main()
