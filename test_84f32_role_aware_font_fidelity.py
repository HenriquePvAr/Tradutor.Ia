"""TDD #84F32 - role-aware, source-derived font fidelity.

Root cause fixed here: ``typography_profile_for_region`` already derived a
rich, source-pixel-based ``visual_class`` (dramatic_red_display,
mystic_blue_system, ink_display, balloon_dialogue, ...) for the color/style
fix in #84F31, but every one of those *display*-shaped classes collapsed onto
the exact same font-file bucket ("shout" -> impact/bahnschrift/arialbd/...)
and every non-display class collapsed onto "regular"
(segoeui/calibri/arial) - a single font family standing in for speech
balloons, thought bubbles, narration boxes, story captions, ordinary display,
dramatic display, system/magical text and location labels alike.

This module adds a semantic ``font_role`` (see
``ocr_balloon._resolve_font_role``) computed generically from the classifier
output only (visual_class / text_role / font_class - never page, region or
text literal) and gives each role its own local-font candidate chain in
``font_fidelity.ROLE_FONT_FILES``. All fixtures are offline; the six real
source pages are read-only #84F24 sentinels reused from test_84f26/test_84f27
purely to prove no regression - production code never branches on their ids.
"""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import os
import unittest

import cv2
import numpy as np

import font_fidelity
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


# ---------------------------------------------------------------------------
# Real-page regression sentinels (read-only #84F24 pages). These prove the
# new role taxonomy does not regress the fixture classes named in the mission.
# ---------------------------------------------------------------------------

def _p001_control_group():
    return _group(1, "BALAO_5", "THAT'S WHAT THEY USED TO SAY",
                  [("THAT'S WHAT THEY USED TO SAY", (44, 2957, 714, 212))],
                  classification="unknown")


def _p002_blue_display_group():
    return _group(2, "BALAO_1", "BEFORE OUR NIGHTMARES BECAME REALITY.", [
        ("BEFORE", (240, 1184, 320, 83)),
        ("OUR NIGHTMARES", (68, 1264, 662, 93)),
        ("BECAME REALITY.", (68, 1363, 662, 146)),
    ], classification="narration")


def _p005_top_group():
    return _group(5, "BALAO_1", "REAL COFFEE.",
                  [("REAL COFFEE.", (262, 395, 421, 78))], classification="speech")


def _p005_bottom_group():
    group = _group(5, "BALAO_2", "NOT THE CHEAP SYNTHETIC STUFF I'M USED TO GETTING IN THE SLUMS.", [
        ("NOT THE CHEAP", (170, 1620, 465, 72)),
        ("SYNTHETIC STUFF I'M", (83, 1714, 637, 78)),
        ("USED TO GETTING", (138, 1807, 527, 81)),
        ("IN THE SLUMS.", (177, 1909, 441, 76)),
    ], classification="speech")
    group.region_id = "p005:REGION_002"
    return group


def _p006_decorative_group():
    group = _group(6, "BALAO_2", "IT BETTER BE WORTH IT.", [
        ("IT BETTER BE", (161, 1771, 480, 92)),
        ("WORTH IT.", (219, 1884, 363, 99)),
    ], classification="decorative")
    group.region_id = "p006:REGION_002"
    return group


def _p024_red_display_group():
    return _group(24, "BALAO_1", "I HAVE BEEN MARKED...", [
        ("I HAVE BEEN", (237, 525, 320, 86)),
        ("MARKED...", (130, 630, 537, 159)),
    ], classification="speech")


def _p025_red_display_group():
    return _group(25, "BALAO_1", "BY THE NIGHTMARE SPELL", [
        ("BY THE", (246, 1098, 304, 86)),
        ("NIGHTMARE", (122, 1200, 555, 141)),
        ("SPELL", (253, 1364, 295, 141)),
    ], classification="speech")


def _profile(page_no, group):
    image = _source_page(page_no)
    return ob.typography_profile_for_region(image, group, group.box)


class RegressionFixtureFontRoleTests(unittest.TestCase):
    """Every named regression fixture must keep resolving to a *distinct*,
    source-appropriate font role - none of them may collapse back onto a
    single shared bucket."""

    def test_p001_mystic_blue_uses_system_text_role(self):
        profile = _profile(1, _p001_control_group())
        self.assertEqual(profile["visual_class"], "mystic_blue_system")
        self.assertEqual(profile["font_role"], "system_text")
        self.assertFalse(profile["font_role_fallback"])

    def test_p002_mystic_blue_uses_system_text_role(self):
        profile = _profile(2, _p002_blue_display_group())
        self.assertEqual(profile["visual_class"], "mystic_blue_system")
        self.assertEqual(profile["font_role"], "system_text")

    def test_p005_top_ink_display_uses_display_role(self):
        profile = _profile(5, _p005_top_group())
        self.assertEqual(profile["visual_class"], "ink_display")
        self.assertEqual(profile["font_role"], "display")

    def test_p005_bottom_ink_display_uses_display_role_not_system(self):
        profile = _profile(5, _p005_bottom_group())
        self.assertEqual(profile["visual_class"], "ink_display")
        self.assertEqual(profile["font_role"], "display")
        self.assertNotEqual(profile["font_role"], "system_text")

    def test_p006_ink_display_uses_display_role(self):
        profile = _profile(6, _p006_decorative_group())
        self.assertEqual(profile["visual_class"], "ink_display")
        self.assertEqual(profile["font_role"], "display")

    def test_p024_dramatic_red_uses_dramatic_display_role(self):
        profile = _profile(24, _p024_red_display_group())
        self.assertEqual(profile["visual_class"], "dramatic_red_display")
        self.assertEqual(profile["font_role"], "dramatic_display")

    def test_p025_dramatic_red_uses_dramatic_display_role(self):
        profile = _profile(25, _p025_red_display_group())
        self.assertEqual(profile["visual_class"], "dramatic_red_display")
        self.assertEqual(profile["font_role"], "dramatic_display")

    def test_dramatic_display_and_system_text_resolve_to_different_font_files(self):
        """P024/P025 (dramatic_display) and P001/P002 (system_text) must not
        share a font family just because both are "display"-shaped."""
        dramatic = _profile(24, _p024_red_display_group())
        system = _profile(1, _p001_control_group())
        _, dramatic_runtime = font_fidelity.resolve_font(dramatic["font_role"], 32)
        _, system_runtime = font_fidelity.resolve_font(system["font_role"], 32)
        self.assertNotEqual(
            dramatic_runtime["actual_font_identity"], system_runtime["actual_font_identity"])


# ---------------------------------------------------------------------------
# Synthetic fixtures: no real webtoon page can prove the *ordinary* speech and
# narration path (the real pages skew toward display lettering), so these
# stand-ins render an organic/handwritten-looking source directly.
# ---------------------------------------------------------------------------

def _flat_background(height, width, *, color=(250, 249, 251)):
    return np.full((height, width, 3), color, dtype=np.uint8)


def _ordinary_balloon_source_image():
    """White speech balloon, organic (script-style) comic source lettering -
    not the geometric block a generic fallback would use. Kept short/squat
    (height < 120px, aspect < 3.2) so it reads as ordinary dialogue geometry
    rather than tall multi-line display lettering."""
    canvas = _flat_background(110, 280)
    cv2.putText(canvas, "come with me", (14, 45), cv2.FONT_HERSHEY_SCRIPT_COMPLEX,
                1.0, (35, 30, 40), 2, cv2.LINE_AA)
    cv2.putText(canvas, "right now", (14, 90), cv2.FONT_HERSHEY_SCRIPT_COMPLEX,
                1.0, (35, 30, 40), 2, cv2.LINE_AA)
    return canvas


def _ordinary_balloon_group():
    return _group(901, "SYNTH_BALLOON", "come with me right now",
                  [("come with me", (12, 15, 240, 42)), ("right now", (12, 60, 190, 42))],
                  classification="speech")


def _narration_box_source_image():
    """White rectangular narration box, black comic-style source font, low
    occupancy (well-spaced caption text) - not tall/condensed display ink."""
    canvas = _flat_background(140, 420)
    cv2.rectangle(canvas, (2, 2), (417, 137), (60, 55, 65), 1)
    cv2.putText(canvas, "Three days later,", (24, 62), cv2.FONT_HERSHEY_COMPLEX,
                0.75, (30, 26, 34), 1, cv2.LINE_AA)
    cv2.putText(canvas, "the city fell silent.", (24, 105), cv2.FONT_HERSHEY_COMPLEX,
                0.75, (30, 26, 34), 1, cv2.LINE_AA)
    return canvas


def _narration_box_group():
    return _group(902, "SYNTH_NARRATION", "Three days later, the city fell silent.", [
        ("Three days later,", (20, 30, 300, 40)),
        ("the city fell silent.", (20, 75, 320, 40)),
    ], classification="narration")


def _display_control_source_image():
    """Condensed, tall source lettering - a control that must keep selecting
    a display role, proving the fix does not "correct everything to comic"."""
    canvas = np.zeros((220, 320, 3), dtype=np.uint8)
    canvas[:] = (20, 18, 22)
    for text, origin in (("BOOM", (30, 100)), ("CRASH NOW", (10, 190))):
        cv2.putText(canvas, text, origin, cv2.FONT_HERSHEY_DUPLEX, 1.6,
                    (245, 245, 248), 6, cv2.LINE_AA)
    return canvas


def _display_control_group():
    return _group(903, "SYNTH_DISPLAY", "BOOM CRASH NOW", [
        ("BOOM", (25, 40, 220, 90)),
        ("CRASH NOW", (5, 140, 300, 90)),
    ], classification="unknown")


class SyntheticFixtureRedGreenTests(unittest.TestCase):
    def test_ordinary_balloon_selects_organic_comic_role_not_generic_block(self):
        image = _ordinary_balloon_source_image()
        group = _ordinary_balloon_group()

        profile = ob.typography_profile_for_region(image, group, group.box)

        self.assertEqual(profile["font_role"], "balloon_dialogue")
        _, runtime = font_fidelity.resolve_font(profile["font_role"], 28)
        self.assertIn(runtime["actual_font_identity"], {"comic", "comici", "segoepr"})
        self.assertNotIn(runtime["actual_font_identity"], {"arial", "calibri", "segoeui"})

    def test_narration_box_selects_appropriate_role_not_heavy_display(self):
        image = _narration_box_source_image()
        group = _narration_box_group()

        profile = ob.typography_profile_for_region(image, group, group.box)

        self.assertEqual(profile["font_role"], "narration_box")
        _, runtime = font_fidelity.resolve_font(profile["font_role"], 28)
        self.assertNotEqual(runtime["actual_font_identity"], "impact")
        self.assertNotEqual(runtime["actual_font_identity"], "bahnschrift")

    def test_display_control_still_selects_a_display_role(self):
        """Guards against overcorrecting every role to comic dialogue."""
        image = _display_control_source_image()
        group = _display_control_group()

        profile = ob.typography_profile_for_region(image, group, group.box)

        self.assertIn(profile["font_role"], {"display", "dramatic_display"})
        self.assertNotEqual(profile["font_role"], "balloon_dialogue")


class DifferentRolesSameTextTests(unittest.TestCase):
    def test_same_literal_text_renders_different_roles_for_balloon_vs_dramatic(self):
        text = "GET OUT OF HERE"
        _, balloon_runtime = font_fidelity.resolve_font("balloon_dialogue", 30, text=text)
        _, dramatic_runtime = font_fidelity.resolve_font("dramatic_display", 30, text=text)
        self.assertNotEqual(
            balloon_runtime["actual_font_identity"], dramatic_runtime["actual_font_identity"])


class RoleAwareFontResolutionUnitTests(unittest.TestCase):
    """Direct unit coverage for ocr_balloon._resolve_font_role - the exact
    generic mapping that replaced the page/region-agnostic collapse point."""

    def test_visual_class_mapping_takes_precedence(self):
        role, source = ob._resolve_font_role("unknown", "dramatic_red_display", "tall_display")
        self.assertEqual(role, "dramatic_display")
        self.assertEqual(source, "visual_class_mapping")

    def test_thought_classification_overrides_balloon_visual_class(self):
        role, source = ob._resolve_font_role("thought", "balloon_dialogue", "comic_sans_style")
        self.assertEqual(role, "thought_dialogue")

    def test_text_role_mapping_used_when_no_visual_class_match(self):
        role, source = ob._resolve_font_role("caption", "generic", "unknown_fallback")
        self.assertEqual(role, "story_caption")
        self.assertEqual(source, "text_role_mapping")

    def test_generic_fallback_defaults_to_ordinary_comic_dialogue_not_regular(self):
        role, source = ob._resolve_font_role("unknown", "generic", "unknown_fallback")
        self.assertEqual(role, "balloon_dialogue")
        self.assertEqual(source, "generic_fallback")


class FontRegistryDiversityTests(unittest.TestCase):
    """Mini role audit (mission #41/#42): the eight minimum font roles must
    not collapse onto one or two font families."""

    ROLES = (
        "balloon_dialogue", "thought_dialogue", "narration_box", "story_caption",
        "display", "dramatic_display", "system_text", "location_label",
    )

    def test_each_role_resolves_without_fallback(self):
        for role in self.ROLES:
            with self.subTest(role=role):
                _, runtime = font_fidelity.resolve_font(role, 28)
                self.assertTrue(runtime["font_load_success"])
                self.assertFalse(runtime["fallback_used"], runtime.get("fallback_reason"))

    def test_role_font_families_are_not_dominated_by_a_single_family(self):
        identities = []
        for role in self.ROLES:
            _, runtime = font_fidelity.resolve_font(role, 28)
            identities.append(runtime["actual_font_identity"])
        most_common_count = max(identities.count(name) for name in set(identities))
        self.assertLess(
            most_common_count / len(identities), 0.5,
            f"font family distribution too concentrated: {identities}",
        )


if __name__ == "__main__":
    unittest.main()
