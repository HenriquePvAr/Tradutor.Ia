"""TDD #84F22 visual fidelity contracts.

These tests use synthetic local canvases only. They encode the reusable visual
rules from the user supplied NexusToons-style screenshots without copying a
page, phrase, or scan-specific decision into production code.
"""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest

import cv2
import numpy as np

import ocr_balloon as ob
from ocr_engine import OCRLine


def _line(text, box, *, page=1):
    x, y, w, h = box
    polygon = np.array(
        [[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.int32)
    return OCRLine(
        text=text,
        raw_text=text,
        confidence=0.96,
        polygon=polygon,
        box=(x, y, w, h),
        engine="synthetic",
        page=page,
    )


def _group(text, box, *, classification="narration"):
    group = ob.TextGroup(group_id="BALAO_1", lines=[_line(text, box)])
    group.text = text
    group.translation = text
    group.translation_candidate = text
    group.classification = classification
    group.region_id = "REGION_001"
    return group


def _dark_blue_canvas():
    img = np.zeros((520, 760, 3), dtype=np.uint8)
    img[:, :, 0] = 92
    img[:, :, 1] = 42
    img[:, :, 2] = 12
    cv2.circle(img, (380, 220), 170, (170, 95, 25), -1)
    return cv2.GaussianBlur(img, (0, 0), 7)


def _white_canvas():
    return np.full((520, 760, 3), 248, dtype=np.uint8)


def _draw_original_text(image, text, origin, *, scale, fill, stroke=None,
                        stroke_thickness=8, thickness=3):
    if stroke is not None:
        cv2.putText(
            image, text, origin, cv2.FONT_HERSHEY_SIMPLEX, scale,
            stroke, stroke_thickness, cv2.LINE_AA)
    cv2.putText(
        image, text, origin, cv2.FONT_HERSHEY_SIMPLEX, scale,
        fill, thickness, cv2.LINE_AA)
    return image


def _color_distance(a, b):
    return max(abs(int(a[i]) - int(b[i])) for i in range(3))


class TypographyProfileContracts(unittest.TestCase):
    def test_dark_saturated_open_caption_gets_mystic_blue_display_profile(self):
        img = _dark_blue_canvas()
        _draw_original_text(
            img,
            "ASPIRANTE!",
            (180, 225),
            scale=2.1,
            fill=(255, 245, 225),
            stroke=(210, 105, 28),
            stroke_thickness=9,
            thickness=3,
        )
        group = _group(
            "ASPIRANTE! BEM-VINDO AO FEITICO DO PESADELO.",
            (120, 150, 520, 140),
            classification="narration",
        )

        profile = ob.typography_profile_for_region(img, group, group.box)

        self.assertEqual(profile["visual_class"], "mystic_blue_system")
        self.assertEqual(profile["font_class"], "condensed_display")
        self.assertEqual(profile["case_style"], "uppercase")
        self.assertGreaterEqual(profile["stroke_width"], 2)
        self.assertGreater(profile["glow_strength"], 0)
        self.assertEqual(profile["fill_color"], (226, 245, 255))
        self.assertEqual(profile["style_source"], "original_pixels")

    def test_light_open_emphasis_gets_dramatic_red_display_profile(self):
        img = _white_canvas()
        _draw_original_text(
            img,
            "MARCAD0",
            (135, 245),
            scale=2.8,
            fill=(12, 8, 118),
            stroke=(250, 250, 255),
            stroke_thickness=13,
            thickness=5,
        )
        group = _group("EU FUI MARCADO...", (120, 145, 520, 150))

        profile = ob.typography_profile_for_region(img, group, group.box)

        self.assertEqual(profile["visual_class"], "dramatic_red_display")
        self.assertEqual(profile["font_class"], "tall_display")
        # Curated stylized class keeps its intentional same-family preset (dark red);
        # the raw measured colour does not overwrite deliberate art direction.
        self.assertLessEqual(_color_distance(profile["fill_color"], (118, 8, 12)), 25)
        self.assertGreaterEqual(profile["stroke_width"], 2)
        self.assertGreater(profile["glow_strength"], 0)
        self.assertEqual(profile["style_source"], "original_pixels")

    def test_dialogue_balloon_stays_readable_not_dramatic(self):
        img = _white_canvas()
        _draw_original_text(
            img,
            "HELLO",
            (190, 235),
            scale=1.8,
            fill=(18, 18, 18),
            stroke=None,
            thickness=4,
        )
        group = _group("ISSO NAO TEM NADA A VER COMIGO.", (90, 170, 580, 100),
                       classification="speech")

        profile = ob.typography_profile_for_region(img, group, group.box)

        self.assertEqual(profile["visual_class"], "balloon_dialogue")
        self.assertEqual(profile["font_class"], "comic_sans_style")
        # New contract: the measured near-black source colour (18, 18, 18) is
        # applied rather than the fixed comic preset (32, 28, 38); still dark/readable.
        self.assertTrue(profile["source_text_color_confident"])
        self.assertLessEqual(_color_distance(profile["fill_color"], (18, 18, 18)), 20)
        self.assertEqual(profile["glow_strength"], 0)
        self.assertEqual(profile["style_source"], "original_pixels")


class VisualTextFittingContracts(unittest.TestCase):
    def test_display_wrapping_prefers_balanced_lines(self):
        img = _dark_blue_canvas()
        group = _group(
            "ANTES DOS NOSSOS PESADELOS SE TORNAREM REALIDADE.",
            (95, 130, 570, 170),
            classification="narration",
        )
        profile = ob.typography_profile_for_region(img, group, group.box)

        lines = ob.typographic_wrap_lines(
            "ANTES DOS NOSSOS PESADELOS SE TORNAREM REALIDADE.",
            max_chars=24,
            profile=profile,
        )

        self.assertGreaterEqual(len(lines), 2)
        self.assertLessEqual(max(len(line) for line in lines), 28)
        self.assertLessEqual(max(len(line) for line in lines) -
                             min(len(line) for line in lines), 13)
        self.assertEqual(" ".join(lines),
                         "ANTES DOS NOSSOS PESADELOS SE TORNAREM REALIDADE.")

    def test_visual_quality_flags_weak_generic_overlay(self):
        weak = {
            "font_class": "unknown_fallback",
            "stroke_width": 0,
            "glow_strength": 0,
            "fill_color": (40, 40, 40),
            "background_brightness": 38.0,
            "occupancy_ratio": 0.12,
            "visual_class": "mystic_blue_system",
        }

        verdict = ob.validate_visual_text_profile(weak)

        self.assertFalse(verdict["passed"])
        self.assertIn("font_class_unknown", verdict["reasons"])
        self.assertIn("contrast_too_low", verdict["reasons"])
        self.assertIn("weak_visual_presence", verdict["reasons"])

    def test_visual_quality_accepts_integrated_benchmark_like_profile(self):
        strong = {
            "font_class": "condensed_display",
            "stroke_width": 2,
            "glow_strength": 5,
            "fill_color": (226, 245, 255),
            "background_brightness": 34.0,
            "occupancy_ratio": 0.34,
            "visual_class": "mystic_blue_system",
        }

        verdict = ob.validate_visual_text_profile(strong)

        self.assertTrue(verdict["passed"], verdict)
        self.assertEqual(verdict["reasons"], [])


if __name__ == "__main__":
    unittest.main()
