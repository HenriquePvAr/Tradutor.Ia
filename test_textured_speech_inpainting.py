"""TDD #37 - safe mask acceptance for translated speech on textured backgrounds.

Ordinary speech with a valid candidate, complete source ownership and traceable
line provenance must not degrade to ``preserved_original`` only because a broad
mask fails a nonuniform-background heuristic.  A narrower, provenance-backed
source-text mask may still be safe.  Everything else keeps failing closed.
"""

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest

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
        self.assertEqual(
            metrics.get("reason"),
            "source_scoped_region_too_large_for_safe_cleanup",
        )

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
