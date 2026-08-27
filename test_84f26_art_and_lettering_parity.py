"""TDD #84F26 - real art and source-lettering parity hardening.

These contracts are offline/local.  They use the already-persisted #84F24 source
page images as read-only sentinels for the failure modes found by manual PDF
review: generic typography on display lettering, Page 5 nonuniform art cleanup,
and source-scoped cleanup staying fail-closed without provenance.
"""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import os
import unittest

import cv2
import numpy as np

import ocr_balloon as ob
import ocr_line_provenance
import source_completeness
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
    polygon = np.array(
        [[x, y], [x + w, y], [x + w, y + h], [x, y + h]],
        dtype=np.int32,
    )
    return OCRLine(
        text=text,
        raw_text=text,
        confidence=0.97,
        polygon=polygon,
        box=(x, y, w, h),
        engine="rapidocr",
        page=page,
        metadata={"ocr_line_id": line_id},
    )


def _group(page: int, group_id: str, text: str, boxes, *, classification="speech"):
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


def _p001_control_group():
    return _group(
        1,
        "BALAO_5",
        "THAT'S WHAT THEY USED TO SAY",
        [("THAT'S WHAT THEY USED TO SAY", (44, 2957, 714, 212))],
        classification="unknown",
    )


def _p002_blue_display_group():
    return _group(
        2,
        "BALAO_1",
        "BEFORE OUR NIGHTMARES BECAME REALITY.",
        [
            ("BEFORE OUR", (167, 1184, 467, 83)),
            ("NIGHTMARES BECAME", (68, 1264, 662, 93)),
            ("REALITY.", (242, 1363, 314, 146)),
        ],
        classification="narration",
    )


def _p005_top_black_display_group():
    return _group(
        5,
        "BALAO_1",
        "REAL COFFEE.",
        [("REAL COFFEE.", (262, 395, 421, 78))],
        classification="speech",
    )


def _p005_bottom_group():
    group = _group(
        5,
        "BALAO_2",
        "NOT THE CHEAP SYNTHETIC STUFF I'M USED TO GETTING IN THE SLUMS.",
        [
            ("NOT THE CHEAP", (170, 1620, 465, 72)),
            ("SYNTHETIC STUFF I'M", (83, 1714, 637, 78)),
            ("USED TO GETTING", (138, 1807, 527, 81)),
            ("IN THE SLUMS.", (177, 1909, 441, 76)),
        ],
        classification="speech",
    )
    group.region_id = "p005:REGION_002"
    group.translation = (
        "NÃO ESSA PORCARIA SINTÉTICA E BARATA QUE ESTOU ACOSTUMADO "
        "A GANHAR NOS CORTIÇOS."
    )
    group.translation_candidate = group.translation
    return group


def _p024_red_display_group():
    return _group(
        24,
        "BALAO_1",
        "I HAVE BEEN MARKED...",
        [
            ("I HAVE BEEN", (237, 525, 320, 86)),
            ("MARKED...", (130, 630, 537, 159)),
        ],
        classification="speech",
    )


def _p025_red_display_group():
    return _group(
        25,
        "BALAO_1",
        "BY THE NIGHTMARE SPELL",
        [
            ("BY THE", (246, 1098, 304, 86)),
            ("NIGHTMARE", (122, 1200, 555, 141)),
            ("SPELL", (253, 1364, 295, 141)),
        ],
        classification="speech",
    )


def _profile(page_no: int, group):
    image = _source_page(page_no)
    return ob.typography_profile_for_region(image, group, group.box)


class RealSourceTypographyParityTests(unittest.TestCase):
    def test_p001_existing_mystic_blue_control_stays_source_pixel_based(self):
        profile = _profile(1, _p001_control_group())

        self.assertEqual(profile["visual_class"], "mystic_blue_system")
        self.assertEqual(profile["font_class"], "condensed_display")
        self.assertEqual(profile["selected_font_role"], "shout")
        self.assertEqual(profile["style_source"], "original_pixels")
        self.assertGreater(profile["glow_strength"], 0)

    def test_p002_blue_display_does_not_fall_back_to_generic_or_dialogue(self):
        profile = _profile(2, _p002_blue_display_group())

        self.assertEqual(profile["visual_class"], "mystic_blue_system")
        self.assertEqual(profile["font_class"], "condensed_display")
        self.assertEqual(profile["selected_font_role"], "shout")
        self.assertEqual(profile["style_source"], "original_pixels")
        self.assertGreater(profile["font_match_confidence"], 0.42)
        self.assertNotEqual(profile["font_class"], "unknown_fallback")

    def test_p005_top_black_display_is_not_plain_balloon_dialogue(self):
        profile = _profile(5, _p005_top_black_display_group())

        self.assertEqual(profile["visual_class"], "ink_display")
        self.assertEqual(profile["font_class"], "tall_display")
        self.assertEqual(profile["selected_font_role"], "shout")
        self.assertEqual(profile["style_source"], "original_pixels")
        self.assertEqual(profile["glow_strength"], 0)
        self.assertNotEqual(profile["visual_class"], "balloon_dialogue")

    def test_p024_red_display_uses_dramatic_source_style(self):
        profile = _profile(24, _p024_red_display_group())

        self.assertEqual(profile["visual_class"], "dramatic_red_display")
        self.assertEqual(profile["font_class"], "tall_display")
        self.assertEqual(profile["selected_font_role"], "shout")
        self.assertEqual(profile["style_source"], "original_pixels")
        self.assertGreater(profile["glow_strength"], 0)

    def test_p025_red_display_uses_dramatic_source_style(self):
        profile = _profile(25, _p025_red_display_group())

        self.assertEqual(profile["visual_class"], "dramatic_red_display")
        self.assertEqual(profile["font_class"], "tall_display")
        self.assertEqual(profile["selected_font_role"], "shout")
        self.assertEqual(profile["style_source"], "original_pixels")
        self.assertGreater(profile["glow_strength"], 0)


class PageFiveSourceScopedArtTests(unittest.TestCase):
    def test_p005_nonuniform_art_renders_only_with_complete_source_provenance(self):
        image = _source_page(5)
        group = _p005_bottom_group()
        recorder = ocr_line_provenance.activate()
        try:
            with ocr_line_provenance.page(5):
                ocr_line_provenance.record_input_lines(group.lines, origin="rapidocr")
                ocr_line_provenance.record_group(group)
                rendered = ob.render_analyzed_image(
                    image,
                    group.lines,
                    [],
                    [group],
                    page_index=5,
                )
        finally:
            ocr_line_provenance.deactivate()

        self.assertIsNot(rendered, image)
        self.assertTrue(group.redrawn, group.visual_attempts)
        self.assertEqual(group.translation_final_state, "translated")
        self.assertEqual(group.source_completeness["status"], source_completeness.STATUS_PASS)
        self.assertEqual(group.art_reconstruction_status, "review")
        self.assertEqual(group.art_reconstruction_reason, "art_reconstruction_fidelity_uncertain")
        self.assertEqual(group.visual_attempts[-1]["strategy"], "source_scoped")
        self.assertNotEqual(
            group.visual_attempts[-1].get("reason"),
            "broad_mask_rejected_on_nonuniform_background",
        )

    def test_source_scoped_cleanup_stays_fail_closed_without_source_completeness(self):
        image = _source_page(5)
        group = _p005_bottom_group()
        group.source_completeness = {"status": source_completeness.STATUS_UNAVAILABLE}

        _cleaned, mask, metrics = ob._remove_text_for_group(
            image,
            image,
            group,
            strategy="source_scoped",
        )

        self.assertEqual(int(np.count_nonzero(mask)), 0)
        self.assertFalse(metrics["mask_valid"])
        self.assertEqual(metrics["reason"], "source_scoped_requires_source_completeness_pass")


class RenderingMaskAllowanceTests(unittest.TestCase):
    def test_allowed_modification_box_includes_source_derived_stroke_and_glow(self):
        group = _p024_red_display_group()
        group.translation_box = (160, 580, 480, 126)
        group.font_size = 58
        group.typography_profile = {
            "stroke_width": 3,
            "glow_strength": 5,
            "style_source": "original_pixels",
        }

        mask = ob._draw_allowed_group_mask((1500, 800, 3), group)

        self.assertGreater(int(np.count_nonzero(mask)), 0)
        self.assertEqual(group.allowed_modification_box, (152, 572, 496, 142))


if __name__ == "__main__":
    unittest.main()
