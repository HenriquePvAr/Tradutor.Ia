"""TDD #84F27 - render parity, source occupancy and texture fidelity.

All fixtures are offline and read-only.  They use the already-persisted #84F24
source pages to pin the failure modes found by visual review: a valid PT-BR
candidate being withheld after source-style detection, translated display text
rendering much smaller than its source lettering, and source-scoped cleanup
leaving visibly flat reconstruction on textured art.
"""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import os
import unittest

import cv2
import numpy as np

import advanced_art_inpainting
import config
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


def _group(page: int, group_id: str, source: str, translation: str, boxes, *,
           classification="speech", region_id="REGION_001"):
    lines = [
        _line(line_text, box, page=page, line_id=f"p{page:03d}:{group_id}:{idx}")
        for idx, (line_text, box) in enumerate(boxes, 1)
    ]
    group = ob.TextGroup(group_id=group_id, lines=lines)
    group.text = source
    group.translation = translation
    group.translation_candidate = translation
    group.classification = classification
    group.region_id = f"p{page:03d}:{region_id}"
    group.source_engine = "rapidocr"
    return group


def _render(page_no: int, group):
    image = _source_page(page_no)
    recorder = ocr_line_provenance.activate()
    try:
        with ocr_line_provenance.page(page_no):
            ocr_line_provenance.record_input_lines(group.lines, origin="rapidocr")
            ocr_line_provenance.record_group(group)
            rendered = ob.render_analyzed_image(
                image,
                group.lines,
                [],
                [group],
                page_index=page_no,
            )
    finally:
        ocr_line_provenance.deactivate()
    return image, rendered, group


def _p002_blue_display_group():
    return _group(
        2,
        "BALAO_1",
        "BEFORE OUR NIGHTMARES BECAME REALITY.",
        "ANTES DOS NOSSOS PESADELOS SE TORNAREM REALIDADE.",
        [
            ("BEFORE", (240, 1184, 320, 83)),
            ("OUR NIGHTMARES", (68, 1264, 662, 93)),
            ("BECAME REALITY.", (68, 1363, 662, 146)),
        ],
        classification="narration",
    )


def _p005_top_group():
    return _group(
        5,
        "BALAO_1",
        "REAL COFFEE.",
        "CAFÉ DE VERDADE.",
        [("REAL COFFEE.", (262, 395, 421, 78))],
        classification="speech",
    )


def _p005_bottom_group():
    return _group(
        5,
        "BALAO_2",
        "NOT THE CHEAP SYNTHETIC STUFF I'M USED TO GETTING IN THE SLUMS.",
        (
            "NÃO ESSA PORCARIA SINTÉTICA E BARATA QUE ESTOU ACOSTUMADO "
            "A GANHAR NOS CORTIÇOS."
        ),
        [
            ("NOT THE CHEAP", (170, 1620, 465, 72)),
            ("SYNTHETIC STUFF I'M", (83, 1714, 637, 78)),
            ("USED TO GETTING", (138, 1807, 527, 81)),
            ("IN THE SLUMS.", (177, 1909, 441, 76)),
        ],
        classification="speech",
        region_id="REGION_002",
    )


def _p006_decorative_story_group():
    group = _group(
        6,
        "BALAO_2",
        "IT BETTER BE WORTH IT.",
        "É MELHOR QUE VALHA A PENA.",
        [
            ("IT BETTER BE", (161, 1771, 480, 92)),
            ("WORTH IT.", (219, 1884, 363, 99)),
        ],
        classification="decorative",
        region_id="REGION_002",
    )
    group.main_text_score = 0.8
    group.quality_reasons = []
    return group


def _p024_red_display_group():
    return _group(
        24,
        "BALAO_1",
        "I HAVE BEEN MARKED...",
        "EU FUI MARCADO...",
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
        "PELO FEITIÇO DO PESADELO.",
        [
            ("BY THE", (246, 1098, 304, 86)),
            ("NIGHTMARE", (122, 1200, 555, 141)),
            ("SPELL", (253, 1364, 295, 141)),
        ],
        classification="speech",
    )


def _source_lettering_ratio(image, group):
    mask = ob._build_text_mask(image.shape, [group])
    footprint = ob.source_lettering_footprint(image, group, mask, mask)
    return float(np.count_nonzero(footprint)) / max(1, group.box[2] * group.box[3])


class RenderParityContracts(unittest.TestCase):
    def test_p002_valid_blue_display_candidate_is_physically_rendered(self):
        _source, _rendered, group = _render(2, _p002_blue_display_group())

        self.assertTrue(group.redrawn, group.visual_attempts)
        self.assertEqual(group.translation_final_state, "translated")
        self.assertEqual(group.source_completeness["status"], source_completeness.STATUS_PASS)
        self.assertIn(group.art_reconstruction_status, {"clean", "review"})
        self.assertNotEqual(group.art_reconstruction_reason, "dark_blotch_created_on_textured_art")
        self.assertLessEqual(
            int((group.mask_metrics or {}).get("residual_text_pixels_after_cleanup") or 0),
            0,
            group.mask_metrics,
        )
        self.assertTrue(
            bool((group.mask_metrics or {}).get("source_scoped_display_dark_evidence_cleanup")),
            group.mask_metrics,
        )
        self.assertTrue(
            bool((group.mask_metrics or {}).get("art_fidelity_uncertain")),
            group.mask_metrics,
        )
        self.assertEqual(group.art_reconstruction_status, "review")

    def test_p005_top_valid_light_region_candidate_is_physically_rendered(self):
        _source, _rendered, group = _render(5, _p005_top_group())

        self.assertTrue(group.redrawn, group.visual_attempts)
        self.assertEqual(group.translation_final_state, "translated")
        self.assertEqual(group.source_completeness["status"], source_completeness.STATUS_PASS)
        self.assertNotEqual(group.art_reconstruction_reason, "large_white_patch_on_nonwhite_background")

    def test_p025_valid_red_display_candidate_stays_physically_rendered(self):
        _source, _rendered, group = _render(25, _p025_red_display_group())

        self.assertTrue(group.redrawn, group.visual_attempts)
        self.assertEqual(group.translation_final_state, "translated")


class OccupancyAndTextureContracts(unittest.TestCase):
    def tearDown(self):
        advanced_art_inpainting.reset_default_inpainter_for_tests()

    def test_p024_translated_red_display_occupancy_tracks_source_lettering(self):
        source, _rendered, group = _render(24, _p024_red_display_group())

        source_ratio = _source_lettering_ratio(source, _p024_red_display_group())
        target_ratio = float(
            (group.visual_validation or {}).get("translated_text_occupancy_ratio") or 0.0
        )

        self.assertTrue(group.redrawn, group.visual_attempts)
        self.assertGreaterEqual(target_ratio, min(0.11, source_ratio * 0.55))

    def test_p005_bottom_source_scoped_cleanup_preserves_enough_texture(self):
        _source, _rendered, group = _render(5, _p005_bottom_group())

        self.assertTrue(group.redrawn, group.visual_attempts)
        self.assertEqual(group.translation_final_state, "translated")
        self.assertFalse(
            bool(group.mask_metrics.get("art_fidelity_uncertain")),
            group.mask_metrics,
        )
        self.assertEqual(
            int(group.mask_metrics.get("ordinary_source_residual_pixels_after_cleanup") or 0),
            0,
            group.mask_metrics,
        )
        self.assertLessEqual(
            int(group.mask_metrics.get("residual_text_pixels_after_cleanup") or 0),
            int(group.mask_metrics.get("residual_text_pixel_limit") or 0),
            group.mask_metrics,
        )
        self.assertGreaterEqual(
            float(group.mask_metrics.get("flat_patch_texture_ratio") or 0.0),
            0.55,
        )

    def test_p006_decorative_story_text_fails_closed_while_art_reconstruction_is_unsafe(self):
        _source, _rendered, group = _render(6, _p006_decorative_story_group())

        self.assertFalse(group.redrawn, group.visual_attempts)
        self.assertEqual(group.translation_final_state, "manual_review")
        self.assertEqual(group.art_reconstruction_status, "review")
        self.assertIn(
            group.art_reconstruction_reason,
            {
                "large_white_patch_on_nonwhite_background",
                "flat_reconstruction_patch_on_textured_background",
                "visible_reconstruction_seam_at_mask_boundary",
            },
        )
        self.assertNotEqual(
            group.mask_metrics.get("reason"),
            "source_scoped_requires_story_translation_authority",
            group.mask_metrics,
        )

    def test_p006_decorative_story_text_can_use_verified_lama_fallback(self):
        model_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            ".cache",
            "84f28_inpainting_models",
            "models",
            "inpainting",
            "anime-manga-big-lama.pt",
        )
        if not os.path.exists(model_path):
            self.skipTest(f"missing optional local LaMa test model: {model_path}")

        old_path = config.ADVANCED_ART_INPAINT_MODEL_PATH
        old_hash = config.ADVANCED_ART_INPAINT_MODEL_SHA256
        old_enabled = config.ADVANCED_ART_INPAINTING
        try:
            config.ADVANCED_ART_INPAINTING = True
            config.ADVANCED_ART_INPAINT_MODEL_PATH = model_path
            config.ADVANCED_ART_INPAINT_MODEL_SHA256 = (
                advanced_art_inpainting.ANIME_MANGA_LAMA_LARGE_JIT.expected_sha256
            )
            advanced_art_inpainting.reset_default_inpainter_for_tests()

            _source, _rendered, group = _render(6, _p006_decorative_story_group())
        finally:
            config.ADVANCED_ART_INPAINT_MODEL_PATH = old_path
            config.ADVANCED_ART_INPAINT_MODEL_SHA256 = old_hash
            config.ADVANCED_ART_INPAINTING = old_enabled
            advanced_art_inpainting.reset_default_inpainter_for_tests()

        self.assertTrue(group.redrawn, group.visual_attempts)
        self.assertEqual(group.translation_final_state, "translated")
        self.assertEqual(group.art_reconstruction_status, "clean")
        self.assertEqual(group.render_disposition, "render_clean")
        self.assertTrue(group.visual_validation.get("advanced_inpaint_used"))
        self.assertEqual(
            group.visual_validation.get("advanced_fallback_status"),
            "success",
        )
        self.assertTrue(group.visual_validation.get("model_hash_verified"))
        self.assertEqual(group.visual_validation.get("strategy"), "lama_large")
        self.assertFalse(
            group.visual_validation.get("residual_source_lettering_detected"),
            group.visual_validation,
        )
        self.assertEqual(
            group.visual_validation.get("advanced_art_reason"),
            "ok",
        )

    def test_easy_p005_bottom_does_not_invoke_lama_fallback(self):
        _source, _rendered, group = _render(5, _p005_bottom_group())

        self.assertTrue(group.redrawn, group.visual_attempts)
        self.assertEqual(group.translation_final_state, "translated")
        self.assertFalse(
            any(
                bool(attempt.get("advanced_fallback_attempted"))
                for attempt in group.visual_attempts
            ),
            group.visual_attempts,
        )


if __name__ == "__main__":
    unittest.main()
