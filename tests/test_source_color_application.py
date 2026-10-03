"""Source colour is applied to the render fill when measured confidently, and the
family preset is kept when it is not (mission: typography colour fidelity #2/#13).

Colour and glyph shape stay independent: the same colour with different shapes may
pick different fonts, and different colours with the same shape keep the shape.
"""
from __future__ import annotations

import _test_bootstrap  # noqa: F401

import unittest

import cv2
import numpy as np

import ocr_balloon as ob
from ocr_balloon import OCRLine, TextGroup


def _line(text, box):
    x, y, w, h = box
    poly = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.int32)
    return OCRLine(text=text, confidence=0.95, polygon=poly, box=(x, y, w, h),
                   raw_text=text, engine="rapidocr", page=1)


def _group(text, box, classification="narration"):
    g = TextGroup(group_id="R1", lines=[_line(text, box)], text=text,
                  classification=classification, source_engine="rapidocr")
    g.translation = "OI."  # short: keeps a snug speech box out of display geometry
    return g


def _canvas(bg=(250, 250, 250), size=(240, 300)):
    return np.full((size[0], size[1], 3), bg, dtype=np.uint8)


def _draw(img, text, *, fill_bgr, org=(30, 130), scale=2.2, thick=5, stroke=None):
    if stroke is not None:
        cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, stroke, thick + 8, cv2.LINE_AA)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, fill_bgr, thick, cv2.LINE_AA)


def _dist(a, b):
    return max(abs(int(a[i]) - int(b[i])) for i in range(3))


class SourceColourApplied(unittest.TestCase):
    def _profile_for(self, fill_bgr, bg=(250, 250, 250), classification="speech"):
        # A snug speech box (aspect < 1.6, short text) stays out of the curated
        # open-caption stylized classes, exercising the ordinary measured-colour path.
        img = _canvas(bg=bg)
        _draw(img, "HI", fill_bgr=fill_bgr)
        box = (20, 70, 240, 90)  # h < 120, aspect < 3.2, short text -> ordinary speech
        return ob.typography_profile_for_region(img, _group("HI", box, classification), box)

    def test_red_source_gives_red_fill(self):
        p = self._profile_for((10, 8, 200))  # BGR red
        self.assertTrue(p["source_text_color_confident"])
        r, g, b = p["fill_color"]
        self.assertGreater(r, 120)
        self.assertLess(max(g, b), 90)

    def test_blue_source_gives_blue_fill(self):
        p = self._profile_for((200, 40, 10), bg=(20, 20, 20))  # BGR blue on dark, speech
        self.assertTrue(p["source_text_color_confident"])
        r, g, b = p["fill_color"]
        self.assertGreater(b, 120)
        self.assertLess(r, 110)

    def test_neutral_dark_source_gives_dark_fill(self):
        p = self._profile_for((20, 20, 20))
        self.assertTrue(p["source_text_color_confident"])
        self.assertLessEqual(_dist(p["fill_color"], (20, 20, 20)), 40)

    def test_same_colour_different_shape_can_pick_different_font(self):
        # Both black; only the glyph shape differs -> role may differ (decoupling).
        r1, _ = ob.select_font_role_with_glyph_style(
            "narration_box", "narration",
            {"family_class": "serif", "slant": "normal", "width": "normal", "confidence": 0.9})
        r2, _ = ob.select_font_role_with_glyph_style(
            "narration_box", "narration",
            {"family_class": "sans", "slant": "normal", "width": "normal", "confidence": 0.9})
        self.assertNotEqual(r1, r2)


class LowConfidenceKeepsPreset(unittest.TestCase):
    def test_blank_region_keeps_preset_not_source(self):
        img = _canvas()  # no glyphs
        box = (20, 40, 660, 150)
        p = ob.typography_profile_for_region(img, _group("HELLO", box), box)
        self.assertFalse(p["source_text_color_confident"])
        # A preset/style fill remains (never a crash, never an empty fill).
        self.assertEqual(len(p["fill_color"]), 3)


if __name__ == "__main__":
    unittest.main()
