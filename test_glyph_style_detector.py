"""Glyph-shape style detector: serif/sans/display + italic/weight, from the
source lettering pixels and independent of colour (mission #6/#7).

Each case renders real text in a known local font and asserts the detector reads
its *shape*, not its colour. A serif narration and a comic shout that happen to be
the same colour must classify differently; the same font in different colours must
classify the same. Low-confidence input falls back (family "unknown").
"""
from __future__ import annotations

import _test_bootstrap  # noqa: F401

import unittest

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ocr_balloon import extract_glyph_style_profile, select_font_role_with_glyph_style


def _render_bgr(font_file, text="Perfectly Fine Hamburg", size=72, fill=(20, 20, 20),
                bg=(250, 250, 250)):
    try:
        font = ImageFont.truetype(font_file, size)
    except OSError:
        return None
    img = Image.new("RGB", (1200, 200), bg)
    ImageDraw.Draw(img).text((24, 60), text, font=font, fill=fill)
    rgb = np.array(img)
    return rgb[:, :, ::-1].copy()  # RGB -> BGR


def _profile(font_file, **kw):
    bgr = _render_bgr(font_file, **kw)
    if bgr is None:
        return None
    h, w = bgr.shape[:2]
    return extract_glyph_style_profile(bgr, None, (0, 0, w, h))


class FamilyShapeClassification(unittest.TestCase):
    def test_serif_fonts_read_as_serif(self):
        for f in ("georgia.ttf", "times.ttf"):
            p = _profile(f)
            if p is None:
                continue
            self.assertEqual(p["family_class"], "serif", f)

    def test_sans_fonts_read_as_sans(self):
        for f in ("arial.ttf", "calibri.ttf", "segoeui.ttf"):
            p = _profile(f)
            if p is None:
                continue
            self.assertEqual(p["family_class"], "sans", f)

    def test_impact_reads_as_display(self):
        p = _profile("impact.ttf")
        if p is not None:
            self.assertEqual(p["family_class"], "display")


class SlantAndWeight(unittest.TestCase):
    def test_italic_fonts_detected(self):
        for f in ("ariali.ttf", "arialbi.ttf", "georgiai.ttf", "calibrii.ttf"):
            p = _profile(f)
            if p is None:
                continue
            self.assertEqual(p["slant"], "italic", f)

    def test_upright_fonts_are_normal_slant(self):
        for f in ("arial.ttf", "georgia.ttf", "impact.ttf"):
            p = _profile(f)
            if p is None:
                continue
            self.assertEqual(p["slant"], "normal", f)

    def test_bold_is_heavier_than_regular(self):
        reg = _profile("arial.ttf")
        bold = _profile("arialbd.ttf")
        if reg and bold:
            order = ["thin", "regular", "medium", "bold", "extra_bold"]
            self.assertGreater(order.index(bold["weight"]), order.index(reg["weight"]))


class ColourIsDecoupledFromShape(unittest.TestCase):
    def test_same_font_different_colours_same_family(self):
        black = _profile("georgia.ttf", fill=(15, 15, 15))
        red = _profile("georgia.ttf", fill=(30, 30, 200))     # BGR red is (b,g,r) in image
        white = _profile("georgia.ttf", fill=(245, 245, 245), bg=(20, 20, 20))
        for p in (black, red, white):
            if p is not None:
                self.assertEqual(p["family_class"], "serif")

    def test_same_colour_different_fonts_different_family(self):
        serif = _profile("georgia.ttf", fill=(15, 15, 15))
        sans = _profile("arial.ttf", fill=(15, 15, 15))
        if serif and sans:
            self.assertNotEqual(serif["family_class"], sans["family_class"])


class LowConfidenceFallsBack(unittest.TestCase):
    def test_blank_region_is_unknown_low_confidence(self):
        blank = np.full((80, 200, 3), 250, dtype=np.uint8)
        p = extract_glyph_style_profile(blank, None, (0, 0, 200, 80))
        self.assertEqual(p["family_class"], "unknown")
        self.assertEqual(p["confidence"], 0.0)


class GlyphAwareRoleSelectionIsColourIndependent(unittest.TestCase):
    def _style(self, **kw):
        base = {"family_class": "sans", "slant": "normal", "width": "normal", "confidence": 0.9}
        base.update(kw)
        return base

    def test_serif_narration_gets_serif_role_not_display(self):
        role, source = select_font_role_with_glyph_style(
            "narration_box", "narration", self._style(family_class="serif"))
        self.assertEqual(role, "serif_narration")
        self.assertTrue(source.startswith("glyph_style_serif"))

    def test_italic_shape_gets_italic_role(self):
        role, _ = select_font_role_with_glyph_style(
            "balloon_dialogue", "speech", self._style(slant="italic"))
        self.assertEqual(role, "italic_dialogue")

    def test_serif_italic_combines(self):
        role, _ = select_font_role_with_glyph_style(
            "story_caption", "caption", self._style(family_class="serif", slant="italic"))
        self.assertEqual(role, "serif_italic")

    def test_low_confidence_keeps_semantic_role(self):
        role, source = select_font_role_with_glyph_style(
            "narration_box", "narration", self._style(family_class="serif", confidence=0.3))
        self.assertEqual(role, "narration_box")
        self.assertEqual(source, "semantic_role_fallback")

    def test_same_shape_different_semantic_role_still_serif(self):
        # Shape drives the family; colour is never consulted here.
        r1, _ = select_font_role_with_glyph_style("balloon_dialogue", "speech",
                                                  self._style(family_class="serif"))
        r2, _ = select_font_role_with_glyph_style("narration_box", "narration",
                                                  self._style(family_class="serif"))
        self.assertTrue(r1.startswith("serif") and r2.startswith("serif"))

    def test_same_semantic_role_different_shape_diverges(self):
        serif_role, _ = select_font_role_with_glyph_style("narration_box", "narration",
                                                          self._style(family_class="serif"))
        sans_role, _ = select_font_role_with_glyph_style("narration_box", "narration",
                                                         self._style(family_class="sans"))
        self.assertNotEqual(serif_role, sans_role)


if __name__ == "__main__":
    unittest.main()
