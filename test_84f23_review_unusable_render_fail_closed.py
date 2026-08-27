"""TDD #84F23 - REVIEW_UNUSABLE physical render fail-closed.

The source-style lettering layer from #84F22 is only allowed to run after the
semantic render disposition says the translated candidate is physically
renderable.  These tests are local-only and never call a provider or network.
"""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest
from unittest import mock

import cv2
import numpy as np

import ocr_balloon as ob
import semantic_fidelity
from ocr_engine import OCRLine


P65_SOURCE = "..YOU BE COME AGATETHROUGHWHICH AMONSTERAPPEARSIN THEREALWORLD."
P65_BAD_TARGET = (
    "...VOCÊ SE TORNA A GÁTETA ATRAVÉS DA QUAL UM MONSTRO APARECE NO MUNDO REAL."
)
P65_GOOD_TARGET = (
    "...VOCÊ SE TORNA UM PORTAL ATRAVÉS DO QUAL UM MONSTRO APARECE NO MUNDO REAL."
)


def _line(text, box=(40, 45, 260, 44), *, confidence=0.95):
    x, y, w, h = box
    polygon = np.array(
        [[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.int32
    )
    return OCRLine(
        text=text,
        raw_text=text,
        confidence=confidence,
        polygon=polygon,
        box=box,
        engine="rapidocr",
        page=65,
    )


def _group(source="THEY ARE COMING.", target="ELES ESTÃO VINDO."):
    group = ob.TextGroup(group_id="BALAO_2", lines=[_line(source)])
    group.text = source
    group.translation = target
    group.translation_candidate = target
    group.sent_to_translation = True
    group.translation_valid = True
    group.translation_final_state = "translated"
    group.translation_final_reason = "ok"
    group.classification = "speech"
    group.region_id = "REGION_002"
    group.source_engine = "rapidocr"
    group.quality_score = 0.90
    return group


class RenderDispositionMatrixTests(unittest.TestCase):
    def test_clean_translation_is_renderable(self):
        group = _group()

        allowed, reason = ob.translation_allows_physical_render(group)

        self.assertTrue(allowed)
        self.assertEqual(reason, "")
        self.assertEqual(ob.translation_render_state(group), ("clean", ""))

    def test_review_renderable_translation_is_renderable_with_review(self):
        group = _group()
        group.semantic_review_reason = "terminology_conflict_after_retries"

        allowed, reason = ob.translation_allows_physical_render(group)

        self.assertTrue(allowed)
        self.assertEqual(reason, "terminology_conflict_after_retries")
        self.assertEqual(
            ob.translation_render_state(group),
            ("review", "terminology_conflict_after_retries"),
        )

    def test_review_unusable_translation_is_not_renderable(self):
        group = _group(P65_SOURCE, P65_BAD_TARGET)
        group.semantic_review_reason = (
            f"{semantic_fidelity.SOURCE_SEGMENTATION_INCOMPLETE}:AGATETHROUGHWHICH"
        )

        allowed, reason = ob.translation_allows_physical_render(group)

        self.assertFalse(allowed)
        self.assertTrue(reason.startswith(semantic_fidelity.SOURCE_SEGMENTATION_INCOMPLETE))
        self.assertEqual(ob.translation_render_state(group)[0], "reject")

    def test_rejected_translation_is_not_renderable(self):
        group = _group()
        group.translation_final_state = "rejected"
        group.translation_final_reason = "semantic_fidelity_failed_after_retries"

        allowed, reason = ob.translation_allows_physical_render(group)

        self.assertFalse(allowed)
        self.assertEqual(reason, "semantic_fidelity_failed_after_retries")


class PhysicalFailClosedTests(unittest.TestCase):
    def test_p65_review_unusable_never_reaches_cleanup_or_typography(self):
        original = np.full((180, 420, 3), 255, dtype=np.uint8)
        group = _group(P65_SOURCE, P65_BAD_TARGET)
        group.semantic_review_reason = (
            f"{semantic_fidelity.SOURCE_SEGMENTATION_INCOMPLETE}:AGATETHROUGHWHICH"
        )

        with (
            mock.patch.object(ob, "_remove_text_for_group") as remove,
            mock.patch.object(ob, "_draw_group_translation") as draw,
            mock.patch.object(ob, "typography_profile_for_region") as typography,
        ):
            rendered, _debug = ob.render_analyzed_image(
                original,
                group.lines,
                [],
                [group],
                font_path="",
                page_index=65,
            )

        self.assertTrue(np.array_equal(rendered, original))
        remove.assert_not_called()
        draw.assert_not_called()
        typography.assert_not_called()
        self.assertFalse(group.redrawn)
        self.assertTrue(group.preserved_original)
        self.assertEqual(group.render_disposition, ob.DO_NOT_RENDER)
        self.assertTrue(group.manual_review_required)

    def test_renderable_review_still_enters_typography_and_render_path(self):
        original = np.full((180, 420, 3), 255, dtype=np.uint8)
        mask = np.zeros(original.shape[:2], dtype=np.uint8)
        mask[45:90, 40:300] = 255
        group = _group()
        group.semantic_review_reason = "terminology_conflict_after_retries"

        with (
            mock.patch.object(
                ob,
                "_remove_text_for_group",
                return_value=(original.copy(), mask, {"mask_valid": True}),
            ) as remove,
            mock.patch.object(
                ob,
                "_draw_group_translation",
                side_effect=lambda img, *_args, **_kwargs: img.copy(),
            ) as draw,
            mock.patch.object(
                ob,
                "_enforce_visual_bounds",
                return_value=(original.copy(), {"visual_validation_passed": True}),
            ),
            mock.patch.object(
                ob,
                "_uncovered_source_text_evidence",
                return_value={"measured": True, "source_text_coverage": 1.0},
            ),
        ):
            ob.render_analyzed_image(
                original,
                group.lines,
                [],
                [group],
                font_path="",
                page_index=65,
            )

        remove.assert_called()
        draw.assert_called()
        self.assertEqual(group.render_disposition, ob.RENDER_WITH_REVIEW)
        self.assertTrue(group.manual_review_required)


class RapidOCRSourceRecoveryTests(unittest.TestCase):
    def test_variant_agreement_accepts_only_independent_source_pixels(self):
        image = np.full((180, 420, 3), 255, dtype=np.uint8)
        group = _group(P65_SOURCE, P65_BAD_TARGET)

        class StubEngine:
            def __init__(self):
                self.calls = 0

            def detect_lines(self, _image, **_kwargs):
                self.calls += 1
                if self.calls in {1, 2, 4}:
                    return [_line("YOU BECOME A GATE THROUGH WHICH A MONSTER APPEARS IN THE REAL WORLD.", confidence=0.91)]
                return [_line("YOU BECOME AGATETHROUGHWHICH A MONSTER APPEARS IN THEREALWORLD.", confidence=0.60)]

        record = ob.recover_source_with_rapidocr_variants(
            image,
            group,
            "eng",
            page_index=65,
            engine=StubEngine(),
            max_variants=4,
        )

        self.assertTrue(record["trusted"], record)
        self.assertEqual(record["agreement_count"], 3)
        self.assertFalse(record["target_used_as_source"])
        self.assertEqual(group.canonical_source_text, record["canonical_source"])

    def test_variant_disagreement_stays_ambiguous(self):
        image = np.full((180, 420, 3), 255, dtype=np.uint8)
        group = _group(P65_SOURCE, P65_BAD_TARGET)

        class StubEngine:
            def __init__(self):
                self.calls = 0

            def detect_lines(self, _image, **_kwargs):
                self.calls += 1
                if self.calls in {1, 2}:
                    return [_line("YOU BECOME A GATE THROUGH WHICH A MONSTER APPEARS.", confidence=0.88)]
                return [_line("YOU BECOME AGATE THROUGH WHICH A MONSTER APPEARS.", confidence=0.88)]

        record = ob.recover_source_with_rapidocr_variants(
            image,
            group,
            "eng",
            page_index=65,
            engine=StubEngine(),
            max_variants=4,
        )

        self.assertFalse(record["trusted"], record)
        self.assertEqual(record["reason"], "material_variant_disagreement")
        self.assertEqual(group.canonical_source_text, "")

    def test_trustworthy_recovered_source_can_feed_fake_retry(self):
        group = _group(P65_SOURCE, P65_BAD_TARGET)
        group.source_recovery = {
            "trusted": True,
            "canonical_source": (
                "YOU BECOME A GATE THROUGH WHICH A MONSTER APPEARS IN THE REAL WORLD."
            ),
        }

        self.assertEqual(
            ob._canonical_retry_source(
                group,
                f"{semantic_fidelity.SOURCE_SEGMENTATION_INCOMPLETE}:AGATETHROUGHWHICH",
            ),
            group.source_recovery["canonical_source"],
        )

    def test_ambiguous_recovered_source_never_replaces_raw_source(self):
        group = _group(P65_SOURCE, P65_BAD_TARGET)
        group.source_recovery = {
            "trusted": False,
            "canonical_source": "YOU BECOME A GATE THROUGH WHICH...",
        }

        self.assertEqual(
            ob._canonical_retry_source(
                group,
                f"{semantic_fidelity.SOURCE_SEGMENTATION_INCOMPLETE}:AGATETHROUGHWHICH",
            ),
            P65_SOURCE,
        )


class VisualStyleRegressionOrderTests(unittest.TestCase):
    def test_84f22_styles_remain_available_for_renderable_regions(self):
        from test_84f22_visual_fidelity import (
            _dark_blue_canvas,
            _draw_original_text,
            _group as visual_group,
        )

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
        group = visual_group(
            "ASPIRANTE! BEM-VINDO AO FEITIÇO DO PESADELO.",
            (120, 150, 520, 140),
            classification="narration",
        )

        profile = ob.typography_profile_for_region(img, group, group.box)

        self.assertEqual(profile["visual_class"], "mystic_blue_system")
        self.assertEqual(profile["font_class"], "condensed_display")
        self.assertGreaterEqual(profile["stroke_width"], 2)
        self.assertGreater(profile["glow_strength"], 0)
        self.assertEqual(profile["case_style"], "uppercase")


if __name__ == "__main__":
    unittest.main()
