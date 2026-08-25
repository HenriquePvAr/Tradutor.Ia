"""TDD #37 - safe mask acceptance for translated speech on textured backgrounds.

Ordinary speech with a valid candidate, complete source ownership and traceable
line provenance must not degrade to ``preserved_original`` only because a broad
mask fails a nonuniform-background heuristic.  A narrower, provenance-backed
source-text mask may still be safe.  Everything else keeps failing closed.
"""

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest
from unittest.mock import patch

import cv2
import numpy as np

import config
import ocr_balloon
import source_completeness
from ocr_balloon import (
    TextGroup,
    _build_text_mask,
    _remove_text_for_group,
    _source_scoped_speech_reason,
)
from ocr_engine import OCRLine

STRATEGIES = ("primary", "conservative", "glyph_overlay", "caption_overlay")
FALLBACK = "source_scoped"


def _line(text, box, line_id="L0"):
    x, y, w, h = box
    polygon = np.array(
        [[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.int32
    )
    line = OCRLine(
        text=text,
        confidence=0.97,
        polygon=polygon,
        box=(x, y, w, h),
        raw_text=text,
        engine="rapidocr",
        page=1,
    )
    line.metadata = {"ocr_line_id": line_id}
    return line


def _speech_group(lines, text, classification="speech"):
    group = TextGroup(
        group_id="BALAO_1",
        lines=list(lines),
        text=text,
        classification=classification,
    )
    group.translation = "TRADUCAO PT-BR"
    group.translation_candidate = "TRADUCAO PT-BR"
    group.translation_valid = True
    group.source_engine = "rapidocr"
    group.source_completeness = {"status": source_completeness.STATUS_PASS}
    return group


def _textured_dark_panel(shape=(1400, 800)):
    """Dark artwork whose *context* is textured: exactly the p002 shape."""
    image = np.full((shape[0], shape[1], 3), 12, dtype=np.uint8)
    rng = np.random.default_rng(37)
    noise = rng.integers(0, 26, size=(shape[0], shape[1], 1), dtype=np.uint8)
    image = np.clip(image.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    for x in range(-shape[0], shape[1], 90):
        cv2.line(image, (x, 0), (x + shape[0], shape[0]), (120, 130, 140), 3)
    return image


def _write_light_text(image, text, origin, scale=0.9):
    cv2.putText(
        image,
        text,
        origin,
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        (245, 245, 245),
        2,
        cv2.LINE_AA,
    )


def _attempts(image, group, strategies=STRATEGIES + (FALLBACK,)):
    results = {}
    for strategy in strategies:
        _cleaned, mask, metrics = _remove_text_for_group(
            image, image, group, strategy=strategy
        )
        results[strategy] = (mask, metrics)
    return results


class TexturedSpeechMaskAcceptance(unittest.TestCase):
    """RED matrix for the provenance-backed narrow speech cleanup fallback."""

    def _p002_shaped_case(self):
        image = _textured_dark_panel()
        boxes = [
            (250, 600, 300, 34),
            (220, 650, 360, 34),
            (240, 700, 330, 34),
        ]
        texts = ["IT'S NOT JUST", "THAT I LOST CONTROL", "AND ALMOST KILLED"]
        for text, (x, y, _w, h) in zip(texts, boxes):
            _write_light_text(image, text, (x + 4, y + h - 8), scale=0.8)
        lines = [
            _line(text, box, line_id="L%d" % index)
            for index, (text, box) in enumerate(zip(texts, boxes))
        ]
        return image, _speech_group(lines, " ".join(texts))

    # 24 / 26 - the p002 class must reach the safe narrow strategy.
    def test_textured_speech_broad_masks_reject_but_narrow_fallback_accepts(self):
        image, group = self._p002_shaped_case()
        results = _attempts(image, group)

        for strategy in STRATEGIES:
            self.assertFalse(
                results[strategy][1].get("mask_valid"),
                "%s unexpectedly accepted a broad mask on textured art" % strategy,
            )
        mask, metrics = results[FALLBACK]
        self.assertTrue(metrics.get("mask_valid"), metrics.get("reason"))
        self.assertGreater(int(np.count_nonzero(mask)), 0)
        self.assertEqual(metrics["mask_pixels_outside_source_evidence"], 0)

    # 19 / 37 - the accepted mask never leaves the owned source-line evidence.
    def test_narrow_fallback_mask_stays_inside_source_line_evidence(self):
        image, group = self._p002_shaped_case()
        cleaned, mask, metrics = _remove_text_for_group(
            image, image, group, strategy=FALLBACK
        )
        evidence = _build_text_mask(image.shape, [group])

        self.assertTrue(metrics["mask_valid"], metrics.get("reason"))
        self.assertEqual(int(np.count_nonzero((mask > 0) & (evidence == 0))), 0)
        changed = np.any(cleaned != image, axis=2)
        self.assertEqual(int(np.count_nonzero(changed & (evidence == 0))), 0)

    # 40 / 36 - changed pixels stay localized and inside the page.
    def test_narrow_fallback_changes_stay_localized_and_in_bounds(self):
        image, group = self._p002_shaped_case()
        cleaned, mask, _metrics = _remove_text_for_group(
            image, image, group, strategy=FALLBACK
        )
        ys, xs = np.where(np.any(cleaned != image, axis=2))
        self.assertTrue(len(xs))
        gx, gy, gw, gh = group.box
        self.assertGreaterEqual(int(xs.min()), max(0, gx - 8))
        self.assertGreaterEqual(int(ys.min()), max(0, gy - 8))
        self.assertLessEqual(int(xs.max()), gx + gw + 8)
        self.assertLessEqual(int(ys.max()), gy + gh + 8)
        self.assertLessEqual(int(np.count_nonzero(mask)), image.shape[0] * image.shape[1] // 4)

    # 34 - every owned line is covered, not just the first one.
    def test_narrow_fallback_covers_every_owned_source_line(self):
        image, group = self._p002_shaped_case()
        _cleaned, mask, metrics = _remove_text_for_group(
            image, image, group, strategy=FALLBACK
        )
        self.assertTrue(metrics["mask_valid"], metrics.get("reason"))
        for line in group.lines:
            x, y, w, h = line.box
            self.assertGreater(
                int(np.count_nonzero(mask[y:y + h, x:x + w])),
                0,
                "source line %r left uncovered" % line.text,
            )

    # 35 - two separated clusters stay two bounded regions, not one rectangle.
    def test_disjoint_text_clusters_do_not_produce_one_giant_rectangle(self):
        image = _textured_dark_panel()
        left = (60, 600, 180, 34)
        right = (520, 750, 170, 34)
        _write_light_text(image, "HEY OVER", (64, 628), scale=0.8)
        _write_light_text(image, "THERE NOW", (524, 778), scale=0.8)
        group = _speech_group(
            [_line("HEY OVER", left, "L0"), _line("THERE NOW", right, "L1")],
            "HEY OVER THERE NOW",
        )
        _cleaned, mask, metrics = _remove_text_for_group(
            image, image, group, strategy=FALLBACK
        )
        if metrics.get("mask_valid"):
            gx, gy, gw, gh = group.box
            self.assertLess(
                int(np.count_nonzero(mask)) / float(gw * gh),
                0.5,
                "disjoint clusters were bridged into one broad mask",
            )

    # 25 - the easy uniform case keeps using the normal strategies.
    def test_uniform_background_speech_still_uses_normal_strategy(self):
        image = np.full((240, 320, 3), 255, dtype=np.uint8)
        cv2.ellipse(image, (160, 120), (112, 72), 0, 0, 360, (0, 0, 0), 10)
        cv2.putText(
            image, "STORY TEXT", (82, 128), cv2.FONT_HERSHEY_SIMPLEX,
            0.72, (0, 0, 0), 2, cv2.LINE_AA,
        )
        group = _speech_group([_line("STORY TEXT", (78, 92, 164, 50))], "STORY TEXT")
        group.inside_balloon_like_region = True
        results = _attempts(image, group)
        self.assertTrue(
            any(results[name][1].get("mask_valid") for name in STRATEGIES),
            "uniform speech regressed off the normal cleanup strategies",
        )
        self.assertFalse(results[FALLBACK][1].get("mask_valid"))
        self.assertEqual(
            results[FALLBACK][1].get("reason"),
            "source_scoped_not_needed_on_uniform_background",
        )

    # 27 - an oversized textured region still fails closed.
    def test_oversized_textured_region_is_rejected(self):
        image = _textured_dark_panel()
        box = (40, 200, 720, 300)
        _write_light_text(image, "HUGE", (60, 420), scale=9.0)
        group = _speech_group([_line("HUGE TEXT REGION", box)], "HUGE TEXT REGION")
        _cleaned, _mask, metrics = _remove_text_for_group(
            image, image, group, strategy=FALLBACK
        )
        self.assertFalse(metrics.get("mask_valid"))
        # The rejection is no longer about the *region* being oversized: since
        # TDD #81 this fallback builds a glyph-scoped mask, so it now covers
        # about 1% of the page instead of the whole OCR polygon.  What actually
        # makes the case unsafe is that the display lettering is far too large
        # for that mask to cover, which the residual-source evidence states
        # directly.  Both reasons fail closed and both route to art
        # reconstruction review; this one is the accurate diagnosis.
        self.assertLess(metrics.get("source_scoped_mask_to_page_ratio"), 0.05)
        self.assertEqual(
            metrics.get("reason"),
            "residual_source_text_after_cleanup",
        )
        self.assertIn(
            metrics.get("reason"),
            ocr_balloon.ART_RECONSTRUCTION_REVIEW_REASONS,
        )

    def test_large_source_evidence_with_small_mask_is_measured_by_mask_risk(self):
        image = _textured_dark_panel(shape=(1000, 1000))
        box = (80, 260, 360, 260)
        _write_light_text(image, "SMALL STORY TEXT", (95, 320), scale=0.8)
        group = _speech_group(
            [_line("SMALL STORY TEXT", box)],
            "SMALL STORY TEXT",
        )
        component_mask = np.zeros(image.shape[:2], dtype=np.uint8)
        component_mask[300:340, 100:320] = 255
        component_metrics = {
            "text_component_pixels": int(np.count_nonzero(component_mask)),
            "accepted_text_components": 1,
            "component_based": True,
        }

        empty_mask = np.zeros(image.shape[:2], dtype=np.uint8)
        with patch.object(
            ocr_balloon,
            "_component_text_mask",
            return_value=(component_mask, dict(component_metrics)),
        ), patch.object(
            ocr_balloon,
            "_uniform_dark_line_text_mask",
            return_value=(empty_mask, {"uniform_dark_line_pixels": 0, "uniform_dark_line_count": 0}),
        ), patch.object(
            ocr_balloon,
            "_uniform_light_line_text_mask",
            return_value=(empty_mask, {"uniform_light_line_pixels": 0, "uniform_light_line_count": 0}),
        ), patch.object(
            ocr_balloon,
            "_detached_dark_text_components_mask",
            return_value=(empty_mask, {"detached_dark_text_components": 0, "detached_dark_text_pixels": 0}),
        ), patch.object(
            ocr_balloon,
            "_detached_light_text_components_mask",
            return_value=(empty_mask, {"detached_text_components": 0, "detached_text_pixels": 0}),
        ):
            _cleaned, _mask, metrics = _remove_text_for_group(
                image,
                image,
                group,
                strategy=FALLBACK,
            )

        self.assertGreater(
            metrics["source_evidence_to_page_ratio"],
            config.MAX_SOURCE_SCOPED_PAGE_AREA_RATIO,
        )
        self.assertLessEqual(
            metrics["source_scoped_mask_to_page_ratio"],
            config.MAX_SOURCE_SCOPED_PAGE_AREA_RATIO,
        )
        self.assertTrue(metrics["mask_valid"], metrics.get("reason"))

    def test_proven_light_enclosure_is_not_rejected_as_white_patch(self):
        image = np.full((180, 260, 3), 238, dtype=np.uint8)
        mask = np.zeros(image.shape[:2], dtype=np.uint8)
        mask[70:105, 60:200] = 255
        image[mask > 0] = (20, 20, 20)
        cleaned = image.copy()
        cleaned[mask > 0] = (248, 248, 248)
        group = _speech_group(
            [_line("BY THE NIGHTMARE SPELL", (60, 70, 140, 35))],
            "BY THE NIGHTMARE SPELL",
        )
        group.background_metrics = {
            "strict_uniform_light": True,
            "uniform_light": True,
            "dominant_white_enclosure": True,
        }

        metrics = ocr_balloon._white_patch_artifact_metrics(
            image,
            cleaned,
            group,
            mask,
            "textured_art",
        )

        self.assertFalse(metrics["white_patch_rejected"])

    def test_open_light_art_caption_is_not_rejected_as_white_patch(self):
        image = np.full((180, 260, 3), 205, dtype=np.uint8)
        mask = np.zeros(image.shape[:2], dtype=np.uint8)
        mask[70:105, 60:200] = 255
        image[mask > 0] = (48, 48, 48)
        cleaned = image.copy()
        cleaned[mask > 0] = (248, 248, 248)
        group = _speech_group(
            [_line("SINCE IT COST ME EVERYTHING", (60, 70, 140, 35))],
            "SINCE IT COST ME EVERYTHING",
        )
        group.background_metrics = {
            "open_light_art_caption": True,
            "brightness_mean": 216.0,
            "dark_pixel_ratio": 0.0,
        }

        metrics = ocr_balloon._white_patch_artifact_metrics(
            image,
            cleaned,
            group,
            mask,
            "textured_art",
        )

        self.assertFalse(metrics["white_patch_rejected"])

    def test_plain_light_art_caption_can_use_source_scoped_cleanup(self):
        image = np.full((1100, 800, 3), 224, dtype=np.uint8)
        cv2.fillPoly(
            image,
            [np.array([[0, 0], [170, 0], [65, 220], [0, 220]], dtype=np.int32)],
            (198, 198, 198),
        )
        cv2.fillPoly(
            image,
            [np.array([[360, 0], [800, 0], [800, 220], [430, 220]], dtype=np.int32)],
            (204, 204, 204),
        )
        cv2.putText(
            image,
            "IT COST ME",
            (92, 92),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.05,
            (0, 0, 0),
            5,
            cv2.LINE_AA,
        )
        cv2.putText(
            image,
            "EVERYTHING",
            (60, 155),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.05,
            (0, 0, 0),
            5,
            cv2.LINE_AA,
        )
        group = _speech_group(
            [
                _line("IT COST ME", (88, 55, 300, 48), line_id="L1"),
                _line("EVERYTHING", (56, 118, 360, 48), line_id="L2"),
            ],
            "IT COST ME EVERYTHING",
        )

        background_type, metrics = ocr_balloon._classify_background_region(
            image,
            group,
        )
        self.assertEqual(background_type, "textured_art")
        self.assertTrue(metrics["open_light_art_caption"])

        _cleaned, _mask, cleanup = _remove_text_for_group(
            image,
            image,
            group,
            strategy=FALLBACK,
        )

        self.assertTrue(cleanup.get("mask_valid"), cleanup.get("reason"))
        self.assertFalse(cleanup.get("white_patch_rejected"))

    def test_proven_light_enclosure_recovers_full_owned_line_mask(self):
        image = np.full((180, 260, 3), 238, dtype=np.uint8)
        group = _speech_group(
            [_line("BY THE NIGHTMARE SPELL", (45, 70, 170, 42))],
            "BY THE NIGHTMARE SPELL",
        )
        group.background_type = "textured_art"
        group.background_metrics = {
            "strict_uniform_light": True,
            "uniform_light": True,
            "dominant_white_enclosure": True,
        }

        mask, metrics = ocr_balloon._uniform_light_line_text_mask(image, group)

        self.assertEqual(metrics["uniform_light_line_count"], 1)
        self.assertGreater(metrics["uniform_light_line_pixels"], 170 * 42)
        self.assertGreater(int(np.count_nonzero(mask)), 0)

    def test_unproven_speed_lines_do_not_get_full_owned_line_mask(self):
        image = np.full((180, 260, 3), 150, dtype=np.uint8)
        group = _speech_group(
            [_line("NATIONAL MILITARIES", (45, 70, 170, 42))],
            "NATIONAL MILITARIES",
            classification="narration",
        )
        group.background_type = "speed_lines"
        group.background_metrics = {
            "strict_uniform_light": False,
            "uniform_light": False,
            "dominant_white_enclosure": False,
            "stylized_white_enclosure": False,
            "open_light_art_caption": False,
        }

        mask, metrics = ocr_balloon._uniform_light_line_text_mask(image, group)

        self.assertEqual(metrics["uniform_light_line_count"], 0)
        self.assertEqual(metrics["uniform_light_line_pixels"], 0)
        self.assertEqual(int(np.count_nonzero(mask)), 0)

    def test_saturated_uniform_dark_backdrop_is_not_rejected_as_dark_blotch(self):
        image = np.full((180, 260, 3), (18, 12, 28), dtype=np.uint8)
        mask = np.zeros(image.shape[:2], dtype=np.uint8)
        mask[70:105, 60:200] = 255
        cleaned = image.copy()
        cleaned[mask > 0] = (18, 12, 28)
        group = _speech_group(
            [_line("BEFORE OUR NIGHTMARES", (60, 70, 140, 35))],
            "BEFORE OUR NIGHTMARES",
        )
        group.background_metrics = {
            "brightness_mean": 20.0,
            "dark_pixel_ratio": 0.98,
            "interior_dark_std": 12.0,
            "interior_value_span": 39.0,
            "interior_dark_texture": 1.1,
            "interior_dark_gradient": 12.0,
        }

        metrics = ocr_balloon._dark_blotch_artifact_metrics(
            image,
            cleaned,
            group,
            mask,
            "unknown",
            FALLBACK,
        )

        self.assertTrue(metrics["dark_backdrop_restored"])
        self.assertFalse(metrics["dark_blotch_rejected"])

    def test_nonuniform_dark_art_still_rejects_dark_blotch(self):
        image = np.full((180, 260, 3), 120, dtype=np.uint8)
        mask = np.zeros(image.shape[:2], dtype=np.uint8)
        mask[70:105, 60:200] = 255
        cleaned = image.copy()
        cleaned[mask > 0] = (10, 10, 10)
        group = _speech_group(
            [_line("BEFORE OUR NIGHTMARES", (60, 70, 140, 35))],
            "BEFORE OUR NIGHTMARES",
        )
        group.background_metrics = {
            "brightness_mean": 80.0,
            "dark_pixel_ratio": 0.4,
            "interior_dark_std": 60.0,
            "interior_value_span": 160.0,
            "interior_dark_texture": 20.0,
            "interior_dark_gradient": 80.0,
        }

        metrics = ocr_balloon._dark_blotch_artifact_metrics(
            image,
            cleaned,
            group,
            mask,
            "unknown",
            FALLBACK,
        )

        self.assertFalse(metrics["dark_backdrop_restored"])
        self.assertTrue(metrics["dark_blotch_rejected"])

    def test_unproven_textured_white_patch_is_still_rejected(self):
        image = np.full((180, 260, 3), 120, dtype=np.uint8)
        mask = np.zeros(image.shape[:2], dtype=np.uint8)
        mask[70:105, 60:200] = 255
        cleaned = image.copy()
        cleaned[mask > 0] = (248, 248, 248)
        group = _speech_group(
            [_line("BY THE NIGHTMARE SPELL", (60, 70, 140, 35))],
            "BY THE NIGHTMARE SPELL",
        )
        group.background_metrics = {}

        metrics = ocr_balloon._white_patch_artifact_metrics(
            image,
            cleaned,
            group,
            mask,
            "textured_art",
        )

        self.assertTrue(metrics["white_patch_rejected"])

    # 28 - source completeness must gate the new path.
    def test_source_completeness_failure_fails_closed(self):
        _image, group = self._p002_shaped_case()
        for status in (
            source_completeness.STATUS_FAIL,
            source_completeness.STATUS_REVIEW,
            source_completeness.STATUS_UNAVAILABLE,
        ):
            group.source_completeness = {"status": status}
            self.assertEqual(
                _source_scoped_speech_reason(group),
                "source_scoped_requires_source_completeness_pass",
            )

    # 29 - without line provenance the mask is not source-backed.
    def test_missing_line_provenance_falls_back_to_legacy_behaviour(self):
        image, group = self._p002_shaped_case()
        for line in group.lines:
            line.metadata = {}
        _cleaned, _mask, metrics = _remove_text_for_group(
            image, image, group, strategy=FALLBACK
        )
        self.assertFalse(metrics.get("mask_valid"))
        self.assertEqual(metrics.get("reason"), "source_scoped_requires_line_provenance")

    # 30 / 31 / 33 - preservable classes and preserved entities never enter the path.
    def test_non_story_classes_are_not_routed_through_the_fallback(self):
        _image, group = self._p002_shaped_case()
        for classification in ("sfx", "decorative"):
            group.classification = classification
            self.assertEqual(
                _source_scoped_speech_reason(group),
                "source_scoped_requires_story_translation_authority",
            )
        for classification in ("narration", "unknown"):
            group.classification = classification
            self.assertEqual(_source_scoped_speech_reason(group), "")
        group.classification = "speech"
        group.preserve_as_name = True
        self.assertEqual(
            _source_scoped_speech_reason(group),
            "source_scoped_excludes_preserved_entity",
        )

    # 32 - unintelligible OCR belongs in manual review, not aggressive inpainting.
    def test_unintelligible_source_is_not_inpainted(self):
        _image, group = self._p002_shaped_case()
        group.text = "iHon"
        self.assertEqual(
            _source_scoped_speech_reason(group),
            "source_scoped_requires_intelligible_source",
        )
        group.text = "OK"
        group.ocr_quality_blocked = True
        self.assertEqual(
            _source_scoped_speech_reason(group),
            "source_scoped_requires_intelligible_source",
        )

    # 42 - no candidate means no physical success claim.
    def test_missing_candidate_fails_closed(self):
        _image, group = self._p002_shaped_case()
        group.translation = ""
        group.translation_candidate = ""
        self.assertEqual(
            _source_scoped_speech_reason(group),
            "source_scoped_requires_translation_candidate",
        )

    # 38 - the fallback is the last strategy, so an earlier accept still wins.
    def test_fallback_is_the_last_render_strategy(self):
        import inspect

        source = inspect.getsource(ocr_balloon._render_analyzed_image)
        self.assertIn('"%s",' % FALLBACK, source)
        self.assertLess(
            source.index('"caption_overlay"'),
            source.index('"%s"' % FALLBACK),
        )

    # 41 - the decision must stay explainable in the persisted evidence.
    def test_fallback_records_bounded_decision_telemetry(self):
        image, group = self._p002_shaped_case()
        _cleaned, _mask, metrics = _remove_text_for_group(
            image, image, group, strategy=FALLBACK
        )
        for key in (
            "strategy",
            "mask_pixels",
            "source_evidence_pixels",
            "mask_pixels_outside_source_evidence",
            "mask_to_source_evidence_ratio",
            "source_evidence_to_page_ratio",
            "background_type",
        ):
            self.assertIn(key, metrics)
        self.assertEqual(metrics["strategy"], FALLBACK)

    # 16 - the whole path stays switchable off without touching artwork rules.
    def test_fallback_can_be_disabled_without_touching_broad_protection(self):
        _image, group = self._p002_shaped_case()
        original = config.SOURCE_SCOPED_SPEECH_CLEANUP
        try:
            config.SOURCE_SCOPED_SPEECH_CLEANUP = False
            self.assertEqual(
                _source_scoped_speech_reason(group),
                "source_scoped_disabled",
            )
        finally:
            config.SOURCE_SCOPED_SPEECH_CLEANUP = original


if __name__ == "__main__":
    unittest.main()
