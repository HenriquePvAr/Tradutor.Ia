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

    def _attempt(self, text, confidence):
        return {
            "text": text,
            "normalized_text": ob._source_recovery_normalized(text),
            "word_signature": ob._source_recovery_word_signature(text),
            "confidence": confidence,
        }

    def test_p31a_reordered_reads_of_same_tokens_agree(self):
        """TEST P31-A: independent reads, same relevant tokens, different order.

        Coverage is total (every attempt spells the identical set of words),
        no critical token (a negation, a number, a name) is missing from any
        read, so the multiset agreement in ``_reorder_tolerant_agreement`` may
        trust the region instead of leaving a correct PT-BR translation
        rejected as ``source_segmentation_incomplete``.
        """
        attempts = [
            self._attempt(
                "WHAT YOU DO DURING THE TRIAL WILL DETERMINE THE REWARDS.", 0.86
            ),
            self._attempt(
                "THE TRIAL WILL DO DURING THE WHAT YOU DETERMINE REWARDS.", 0.84
            ),
            self._attempt(
                "DO THE TRIAL WILL DURING WHAT YOU THE DETERMINE REWARDS.", 0.83
            ),
        ]
        record = ob._source_recovery_agreement(attempts)

        self.assertTrue(record["trusted"], record)
        self.assertEqual(record["reason"], "order_independent_variant_agreement")
        self.assertEqual(record["agreement_count"], 3)

    def test_p31b_reordered_reads_disagreeing_on_a_negation_stay_unresolved(self):
        """TEST P31-B: three reads, no two of which share the same tokens.

        One drops "NOT", another spells the negation a different way. Every
        attempt's multiset is therefore unique to itself, so no cluster ever
        reaches the two-attempt corroboration the agreement requires - the
        ambiguity a real negation disagreement represents is preserved, not
        resolved by a coincidental majority.
        """
        attempts = [
            self._attempt("YOU WILL NOT SUCCEED IN THE TRIAL.", 0.86),
            self._attempt("YOU WILL SUCCEED IN THE TRIAL.", 0.85),
            self._attempt("YOU WILL NEVER SUCCEED IN THE TRIAL.", 0.84),
        ]
        record = ob._source_recovery_agreement(attempts)

        self.assertFalse(record["trusted"], record)
        self.assertEqual(record["reason"], "insufficient_variant_agreement")

    def test_p31c_genuinely_different_reads_stay_fail_closed(self):
        """TEST P31-C: two corroborated readings that actually disagree."""
        attempts = [
            self._attempt("YOU BECOME A GATE THROUGH WHICH A MONSTER APPEARS.", 0.88),
            self._attempt("YOU BECOME A GATE THROUGH WHICH A MONSTER APPEARS.", 0.88),
            self._attempt("YOU BECOME A DOOR THROUGH WHICH A CREATURE APPEARS.", 0.88),
            self._attempt("YOU BECOME A DOOR THROUGH WHICH A CREATURE APPEARS.", 0.88),
        ]
        record = ob._source_recovery_agreement(attempts)

        self.assertFalse(record["trusted"], record)
        self.assertEqual(record["reason"], "material_variant_disagreement")

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
