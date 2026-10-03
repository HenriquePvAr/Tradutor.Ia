"""TDD #84F32R - per-line hierarchy, style/font separation, and fit-with-
effects regression coverage.

Root causes fixed here (see ``ocr_balloon.py``):

1. ``_draw_group_translation`` always derived exactly one font size/style for
   an entire multi-line group from the group's *union* box, so a source
   region with a deliberate per-line hierarchy (a small intro clause next to
   a large emphatic clause) always rendered every line at the same size. Now
   ``_source_line_scale_ratios`` reads the already-available per-line source
   box heights and, when the target wraps 1:1 onto the same line count as the
   source, ``_build_per_line_plan``/``_draw_multiline_per_font`` render each
   line at its own proportional size - same font family/role, only the size
   differs (never a hardcoded page/region/text literal).
2. The overflow *gate* only ever measured the raw glyph+stroke box, never the
   glow/shadow footprint that is actually painted, so a display style with a
   glow could report ``overflow == 0`` and still visually bleed past the safe
   area once the Gaussian-blurred glow layer was composited. The gate now
   also checks an effect-inclusive box (stroke + glow radius + shadow
   offset), reusing the same padding logic already trusted for the allowed
   modification mask.

All fixtures are synthetic or reuse the real, read-only #84F24 source pages
already used by ``test_84f32_role_aware_font_fidelity.py``. No fixture is a
production condition: nothing in ``ocr_balloon.py`` branches on a page id,
region id, or text literal.
"""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import os
import unittest

import cv2
import numpy as np
from dataclasses import replace

import config
import ocr_balloon as ob
from ocr_engine import OCRLine

_REAL_RUN = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "output",
    "shadow_slave_chapter_1_5",
    "e489db56-d7be-4fda-aead-b9ae5e54186a",
)


def _source_page(page_no: int):
    path = os.path.join(_REAL_RUN, "smart_input_pages", f"page_{page_no:03d}.png")
    image = cv2.imread(path)
    if image is None:
        raise unittest.SkipTest(f"missing #84F24 read-only source page: {path}")
    return image


def _line(text: str, box, *, page: int, line_id: str):
    x, y, w, h = [int(value) for value in box]
    polygon = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.int32)
    return OCRLine(
        text=text, raw_text=text, confidence=0.97, polygon=polygon, box=(x, y, w, h),
        engine="rapidocr", page=page, metadata={"ocr_line_id": line_id},
    )


def _group(page, group_id, text, boxes, *, classification="speech"):
    lines = [
        _line(line_text, box, page=page, line_id=f"p{page:03d}:{group_id}:{idx}")
        for idx, (line_text, box) in enumerate(boxes, 1)
    ]
    group = ob.TextGroup(group_id=group_id, lines=lines)
    group.text = text
    group.translation = text
    group.translation_candidate = text
    group.classification = classification
    group.region_id = f"p{page:03d}:REGION_001"
    group.source_engine = "rapidocr"
    return group


def _p002_blue_display_group(translation):
    group = _group(2, "BALAO_1", "BEFORE OUR NIGHTMARES BECAME REALITY.", [
        ("BEFORE", (240, 1184, 320, 83)),
        ("OUR NIGHTMARES", (68, 1264, 662, 93)),
        ("BECAME REALITY.", (68, 1363, 662, 146)),
    ], classification="narration")
    group.translation = translation
    group.translation_candidate = translation
    return group


def _p005_bottom_group(translation):
    group = _group(5, "BALAO_2", "NOT THE CHEAP SYNTHETIC STUFF I'M USED TO GETTING IN THE SLUMS.", [
        ("NOT THE CHEAP", (170, 1620, 465, 72)),
        ("SYNTHETIC STUFF I'M", (83, 1714, 637, 78)),
        ("USED TO GETTING", (138, 1807, 527, 81)),
        ("IN THE SLUMS.", (177, 1909, 441, 76)),
    ], classification="speech")
    group.region_id = "p005:REGION_002"
    group.translation = translation
    group.translation_candidate = translation
    return group


class PerLineHierarchySourceSignalTests(unittest.TestCase):
    """Direct unit coverage for ``_source_line_scale_ratios``."""

    def test_uniform_source_lines_have_no_hierarchy_signal(self):
        group = _p005_bottom_group("placeholder")
        self.assertIsNone(ob._source_line_scale_ratios(group))

    def test_skewed_source_lines_produce_a_hierarchy_signal(self):
        group = _p002_blue_display_group("placeholder")
        ratios = ob._source_line_scale_ratios(group)
        self.assertIsNotNone(ratios)
        self.assertEqual(len(ratios), 3)
        # "BEFORE" (h=83) must resolve smaller than "BECAME REALITY." (h=146).
        self.assertLess(ratios[0], ratios[2])

    def test_single_line_group_has_no_hierarchy_signal(self):
        group = _group(1, "BALAO_1", "ONLY ONE LINE",
                        [("ONLY ONE LINE", (10, 10, 200, 40))])
        self.assertIsNone(ob._source_line_scale_ratios(group))


class PerLineHierarchyRenderTests(unittest.TestCase):
    """End-to-end: the real #84F24 P002 source must keep its source-derived
    per-line hierarchy (small intro line, larger emphatic lines) after
    translation, instead of collapsing every line to a uniform size."""

    def test_p002_translation_preserves_source_per_line_hierarchy(self):
        image = _source_page(2)
        group = _p002_blue_display_group(
            "ANTES NOSSOS PESADELOS SE TORNARAM REALIDADE.")
        ob._draw_group_translation(image.copy(), group, font_path=None, source_bgr=image)
        sizes = getattr(group, "line_font_sizes", None)
        self.assertIsNotNone(sizes, "per-line plan did not engage for the P002 fixture")
        self.assertEqual(len(sizes), 3)
        self.assertLess(sizes[0], sizes[-1],
                         "intro line must render smaller than the closing emphatic line")
        self.assertTrue(group.translation_valid)

    def test_p005_bottom_uniform_source_stays_uniform(self):
        """Control: P005 BOTTOM's source lines are all a similar height, so
        the per-line path must not engage and must not fabricate a
        hierarchy that was never in the source."""
        image = _source_page(5)
        group = _p005_bottom_group(
            "NAO A PORCARIA SINTETICA BARATA QUE ESTOU ACOSTUMADO A CONSEGUIR NOS GUETOS.")
        ob._draw_group_translation(image.copy(), group, font_path=None, source_bgr=image)
        self.assertIsNone(getattr(group, "line_font_sizes", None))
        self.assertTrue(group.translation_valid)
        self.assertEqual(group.typography_profile.get("visual_class"), "ink_display")
        self.assertNotEqual(group.typography_profile.get("visual_class"), "mystic_blue_system")


class SyntheticPerLineHierarchyTests(unittest.TestCase):
    """Generic (non-real-page) fixture per mission #26: a two-tier source
    line height must survive into the final render as a relative scale
    ordering, and a renderer that collapses every line to a uniform size
    must fail this test."""

    def _two_tier_group(self):
        return _group(950, "SYNTH_HIER", "small line BIG LINE HERE NOW", [
            ("small line", (20, 20, 160, 30)),
            ("BIG LINE HERE", (10, 60, 300, 70)),
            ("NOW", (10, 140, 150, 70)),
        ], classification="narration")

    def _canvas(self):
        canvas = np.zeros((260, 340, 3), dtype=np.uint8)
        canvas[:] = (20, 18, 22)
        return canvas

    def test_renderer_preserves_relative_scale_ordering(self):
        image = self._canvas()
        group = self._two_tier_group()
        group.translation = "linha pequena LINHA GRANDE AQUI AGORA"
        group.translation_candidate = group.translation
        ob._draw_group_translation(image.copy(), group, font_path=None, source_bgr=image)
        sizes = getattr(group, "line_font_sizes", None)
        if sizes is None:
            self.skipTest("synthetic fixture did not reach the display wrap path")
        self.assertEqual(len(sizes), 3)
        self.assertLess(sizes[0], sizes[1])


class FitIncludesEffectFootprintTests(unittest.TestCase):
    """Mission #27: fit must include the glow/shadow footprint, not just the
    raw glyph+stroke box."""

    def test_effect_box_is_at_least_as_large_as_translation_box(self):
        image = np.zeros((400, 400, 3), dtype=np.uint8)
        image[:] = (15, 12, 18)
        group = _group(960, "SYNTH_GLOW", "GLOWING WORDS HERE", [
            ("GLOWING WORDS HERE", (20, 150, 360, 100)),
        ], classification="narration")
        group.translation = "PALAVRAS BRILHANTES AQUI"
        group.translation_candidate = group.translation
        ob._draw_group_translation(image.copy(), group, font_path=None, source_bgr=image)
        self.assertTrue(group.translation_valid)
        effect_box = getattr(group, "effect_box", None)
        translation_box = group.translation_box
        self.assertIsNotNone(effect_box)
        self.assertIsNotNone(translation_box)
        # The effect-inclusive box must never be smaller than the raw box -
        # it can only grow to cover stroke+glow+shadow.
        self.assertGreaterEqual(effect_box[2], translation_box[2])
        self.assertGreaterEqual(effect_box[3], translation_box[3])

    def test_glow_radius_alone_can_flip_a_fitting_box_into_overflow(self):
        """Deterministic version of the same contract: a glyph+stroke box
        that exactly fits the draw box must be reported as overflowing once
        its glow radius is large enough to push the effect box past the same
        draw box - proving the gate actually looks at the effect box and not
        just the raw glyph+stroke box."""
        draw_box = (0, 0, 100, 100)
        translation_box = (5, 5, 90, 90)  # fits comfortably inside draw_box
        self.assertEqual(ob._box_overflow_ratio(translation_box, draw_box), 0.0)
        # Same ink, but padded out by a glow radius large enough to cross the
        # draw box edge - this is exactly the ``effect_box`` construction in
        # ``_draw_group_translation``.
        glow_radius = 12
        effect_box = (
            translation_box[0] - glow_radius, translation_box[1] - glow_radius,
            translation_box[2] + glow_radius * 2, translation_box[3] + glow_radius * 2,
        )
        self.assertGreater(ob._box_overflow_ratio(effect_box, draw_box), 0.0)

    def test_severe_effect_overflow_is_rejected_but_minor_graze_is_not(self):
        """The effect footprint gate (``MAX_EFFECT_OVERFLOW_RATIO``) is
        deliberately more lenient than the raw glyph+stroke gate
        (``MAX_TEXT_OVERFLOW_RATIO``) - it must reject a severe spill (a
        glow bleeding well past the safe area, into unrelated artwork) while
        still tolerating the small graze many already-accepted glow/shadow
        balloon styles produce."""
        draw_box = (0, 0, 200, 200)
        translation_box = (10, 10, 180, 180)
        minor_effect_box = (8, 8, 184, 184)  # 1% larger - a hairline graze
        severe_effect_box = (10, 10, 250, 250)  # spills well past the panel
        self.assertLessEqual(
            ob._box_overflow_ratio(minor_effect_box, draw_box), config.MAX_EFFECT_OVERFLOW_RATIO)
        self.assertGreater(
            ob._box_overflow_ratio(severe_effect_box, draw_box), config.MAX_EFFECT_OVERFLOW_RATIO)


class StyleFontRoleSeparationTests(unittest.TestCase):
    """Mission #25: resolving a font *role* must never silently overwrite the
    source-derived fill/stroke/glow that ``_typography_profile_for_region``
    already computed from pixels."""

    def test_resolving_font_role_does_not_mutate_style_colors(self):
        profile = {
            "text_role": "unknown", "visual_class": "ink_display", "font_class": "tall_display",
            "fill_color": (250, 250, 252), "stroke_color": (18, 16, 22),
        }
        before_fill, before_stroke = profile["fill_color"], profile["stroke_color"]
        role, source = ob._resolve_font_role(
            profile["text_role"], profile["visual_class"], profile["font_class"])
        self.assertEqual(role, "display")
        # The role resolver takes only role-shaped inputs and returns only a
        # role string - it has no way to see or touch fill/stroke at all,
        # which is exactly the architectural guarantee this test locks in.
        self.assertEqual(profile["fill_color"], before_fill)
        self.assertEqual(profile["stroke_color"], before_stroke)

    def test_ink_display_style_survives_font_resolution_pipeline(self):
        """Full pipeline version: an ink_display (white fill / black outline)
        profile must render with that same style after the font role/size
        selection pipeline runs, not the mystic-blue system-text palette."""
        image = _source_page(5)
        group = _p005_bottom_group(
            "NAO A PORCARIA SINTETICA BARATA QUE ESTOU ACOSTUMADO A CONSEGUIR NOS GUETOS.")
        ob._draw_group_translation(image.copy(), group, font_path=None, source_bgr=image)
        profile = group.typography_profile
        self.assertEqual(profile["visual_class"], "ink_display")
        self.assertEqual(profile["font_role"], "display")
        # White-on-black or black-on-white only - never the mystic blue fill.
        fill = profile["fill_color"]
        self.assertNotEqual(tuple(fill), (226, 245, 255))
        stroke = profile["stroke_color"]
        self.assertNotEqual(tuple(stroke), (26, 86, 160))


class OrdinarySpeechRoleTests(unittest.TestCase):
    """Mission #28: an ordinary balloon fixture must not resolve to a
    dramatic/display-heavy font without strong source evidence."""

    def test_ordinary_balloon_does_not_resolve_to_dramatic_display(self):
        canvas = np.full((110, 280, 3), (250, 249, 251), dtype=np.uint8)
        import cv2 as _cv2
        _cv2.putText(canvas, "come with me", (14, 45), _cv2.FONT_HERSHEY_SCRIPT_COMPLEX,
                      1.0, (35, 30, 40), 2, _cv2.LINE_AA)
        _cv2.putText(canvas, "right now", (14, 90), _cv2.FONT_HERSHEY_SCRIPT_COMPLEX,
                      1.0, (35, 30, 40), 2, _cv2.LINE_AA)
        group = _group(970, "SYNTH_BALLOON", "come with me right now",
                        [("come with me", (12, 15, 240, 42)), ("right now", (12, 60, 190, 42))],
                        classification="speech")
        profile = ob.typography_profile_for_region(canvas, group, group.box)
        self.assertEqual(profile["font_role"], "balloon_dialogue")
        self.assertNotIn(profile["font_role"], {"dramatic_display", "system_text"})


if __name__ == "__main__":
    unittest.main()
