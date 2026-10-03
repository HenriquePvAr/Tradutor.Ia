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


def _p001_mystic_blue_display_group():
    group = _group(
        1,
        "BALAO_4",
        '"DARE TO DREAM"',
        '"OUSE SONHAR"',
        [('"DARE TO DREAM"', (83, 912, 652, 85))],
        classification="decorative",
        region_id="REGION_004",
    )
    group.background_type = "textured_art"
    group.background_metrics = {
        "background_type": "textured_art",
        "context_brightness_mean": 17.686,
        "context_dark_pixel_ratio": 0.991,
        "context_white_pixel_ratio": 0.0,
        "context_saturation_mean": 171.524,
        "brightness_mean": 102.228,
        "saturation_mean": 145.39,
    }
    group.main_text_score = 0.48
    group.quality_reasons = [
        "dictionary_near_miss",
        "generic_ocr_repair_available",
    ]
    return group


def _scan_watermark_group():
    group = _group(
        1,
        "WATERMARK",
        "VORTEXSCANS.COM",
        "VORTEXSCANS.COM",
        [("VORTEXSCANS.COM", (470, 845, 250, 34))],
        classification="decorative",
        region_id="REGION_SCAN",
    )
    group.background_type = "textured_art"
    group.background_metrics = {
        "background_type": "textured_art",
        "context_brightness_mean": 25.0,
        "context_dark_pixel_ratio": 0.92,
        "context_white_pixel_ratio": 0.0,
        "context_saturation_mean": 100.0,
    }
    group.main_text_score = 0.4
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
    def test_short_mystic_blue_display_story_text_is_translatable_not_decorative_skip(self):
        group = _p001_mystic_blue_display_group()

        ob._apply_classification_policy(group)

        self.assertFalse(group.ignored)
        self.assertEqual(group.ignore_reason, "")
        self.assertTrue(ob._should_translate_group(group))

    def test_scan_watermark_remains_preserved_not_promoted_by_display_caption_rule(self):
        group = _scan_watermark_group()

        ob._apply_classification_policy(group)

        self.assertTrue(group.ignored)
        self.assertEqual(group.ignore_reason, "url")
        self.assertFalse(ob._should_translate_group(group))

    def test_promoted_display_story_text_completes_classification_not_just_ignored_flag(self):
        """#84F30 regression: promotion must not stop at ``ignored = False``.

        A weak "decorative" legacy label promoted on real story/display
        evidence has to update ``group.classification`` itself to a
        downstream-recognised weak label too. Leaving the stale "decorative"
        label behind after promotion is exactly what let a promoted region
        keep failing the generic isolated-retry gate (which only trusts
        speech/thought/narration/unknown) and never reach translation/render.
        """
        group = _p001_mystic_blue_display_group()

        ob._apply_classification_policy(group)

        self.assertFalse(group.ignored)
        self.assertNotEqual(
            group.classification,
            "decorative",
            "promotion cleared ignored but left the downstream classification"
            " on the weak legacy label, so retry/authority checks still see"
            " a decorative region",
        )
        self.assertIn(group.classification, {"speech", "thought", "narration", "unknown"})
        self.assertTrue(ob._group_has_story_translation_authority(group))

    def test_promoted_display_story_text_is_eligible_for_isolated_strong_retry(self):
        """A promoted display-story group must not be excluded from the one
        extra fully-translating attempt merely because its original weak OCR
        taxonomy label was "decorative" (the previous failure class: a first
        candidate identical to the source, or another weak candidate, needs
        an isolated stronger retry to actually reach a PT-BR target)."""
        group = _p001_mystic_blue_display_group()

        ob._apply_classification_policy(group)

        self.assertTrue(ob._needs_isolated_retry(group, "candidate_equals_source"))
        self.assertTrue(
            ob._needs_isolated_retry(group, "residual_source_language: dare")
        )

    def test_unpromoted_decorative_text_stays_ineligible_for_isolated_strong_retry(self):
        """Negative control: an ordinary decorative label with no story/display
        evidence (weak confidence, no dark/saturated display context) must
        stay outside the strong-retry gate — promotion must not become a
        blanket bypass for every decorative region."""
        group = _group(
            1,
            "BALAO_5",
            "SPARKLE",
            "BRILHO",
            [("SPARKLE", (10, 10, 100, 40))],
            classification="decorative",
            region_id="REGION_005",
        )

        ob._apply_classification_policy(group)

        self.assertEqual(group.classification, "decorative")
        self.assertFalse(ob._needs_isolated_retry(group, "candidate_equals_source"))

    def test_p002_valid_blue_display_candidate_is_physically_rendered(self):
        """#84F30 P002 regression: the source-scoped display cleanup used to
        leave a visibly flat/smoothed rectangular patch where the speckled
        starfield background had been (Telea inpainting over a large merged
        display-caption mask erases real per-pixel grain). The fix
        (``_restore_lost_local_texture``) reinjects grain matched to the
        proven-textured surrounding context so the reconstruction no longer
        reads as an invented flat block. That grain is statistically matched,
        not recovered, so - per ART-RECON-001 - it must not silently promote
        the disposition to "clean": the render must still honestly report
        "review" (``texture_synthesized``), while the *visible* rectangle
        defect (measured by ``flat_patch_texture_ratio``) is gone.
        """
        _source, _rendered, group = _render(2, _p002_blue_display_group())

        self.assertTrue(group.redrawn, group.visual_attempts)
        self.assertEqual(group.translation_final_state, "translated")
        self.assertEqual(group.source_completeness["status"], source_completeness.STATUS_PASS)
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
        # The reconstruction must not read as a flat rectangular block: its
        # interior texture must be comparable to (not far below) the proven
        # textured ring around it.
        self.assertGreaterEqual(
            float((group.mask_metrics or {}).get("flat_patch_texture_ratio") or 0.0),
            0.55,
            group.mask_metrics,
        )
        self.assertTrue(
            bool((group.mask_metrics or {}).get("texture_synthesized")),
            group.mask_metrics,
        )
        # Fidelity stays honestly uncertain: the improved ratio above is a
        # cosmetic repair, not a claim the original artwork was recovered.
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
        """#84F30 P005-bottom regression: the source-scoped mask here is a
        single merged blob spanning four balloon lines over sky/cloud/building
        art.  A *whole-mask* texture average could pass even while one
        sub-region (say, over a cloud) reconstructed as a locally flat light
        polygon - exactly the "light/polygonal reconstruction patch" defect
        seen in the real run, hidden by an average that let other sub-regions
        compensate.  ``_restore_lost_local_texture`` now measures a spatially
        varying expected-texture field (real ring texture extended inward)
        instead of one page-wide mean, so a locally flat patch is caught and
        grained even when the old whole-mask ratio looked fine.  As with P002,
        that grain is synthesised, not recovered, so fidelity honestly stays
        "review" (``texture_synthesized``) rather than silently "clean".
        """
        _source, _rendered, group = _render(5, _p005_bottom_group())

        self.assertTrue(group.redrawn, group.visual_attempts)
        self.assertEqual(group.translation_final_state, "translated")
        self.assertTrue(
            bool(group.mask_metrics.get("texture_synthesized")),
            group.mask_metrics,
        )
        self.assertTrue(
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
        # Hermeticity: this contract is "no advanced fallback is available",
        # not "the developer machine happens to lack the optional model". A
        # real production model can be installed at the default
        # ADVANCED_ART_INPAINT_MODEL_PATH (e.g. under %LOCALAPPDATA%), in
        # which case the primary strategies' fail-closed behaviour is masked
        # by a genuinely successful LaMa fallback. Force the "unavailable"
        # state explicitly instead of relying on ambient machine state, the
        # same way test_p006_decorative_story_text_can_use_verified_lama_fallback
        # explicitly forces the "available" state.
        old_enabled = config.ADVANCED_ART_INPAINTING
        try:
            config.ADVANCED_ART_INPAINTING = False
            advanced_art_inpainting.reset_default_inpainter_for_tests()
            _source, _rendered, group = _render(6, _p006_decorative_story_group())
        finally:
            config.ADVANCED_ART_INPAINTING = old_enabled
            advanced_art_inpainting.reset_default_inpainter_for_tests()

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
