"""ART-RECON-001 - source lettering footprint, residual validation and render disposition.

TDD #84F2.  Three dimensions the real #84 run proved were conflated:

* what the source lettering physically occupies (body *and* outline/halo/shadow),
* whether that footprint really left the page after cleanup,
* whether the render may still ship when art *fidelity* is merely uncertain.

The real #84 evidence behind these contracts: pages 5 and 6 had good PT-BR
candidates dropped because every strategy was refused, the last one with
``large_white_patch_on_nonwhite_background``; and page 6's first region shipped
with the white outline of ``SINCE IT COST ME EVERYTHING I HAD LEFT...`` still
legible behind the Portuguese while the run reported ``art_reconstruction_status:
clean``.  Everything below is synthetic and offline - no provider, no network, no
real job.
"""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest

import cv2
import numpy as np

import ocr_balloon as ob
import ocr_line_provenance
from ocr_engine import OCRLine


def _line(text, box, *, line_id="L0"):
    x, y, w, h = box
    polygon = np.array(
        [[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.int32)
    return OCRLine(
        text=text, confidence=0.97, polygon=polygon, box=(x, y, w, h),
        raw_text=text, engine="synthetic", page=1,
        metadata={"ocr_line_id": line_id})


def _group(lines, *, classification="narration",
           translation="ISSO E UMA FRASE DE HISTORIA."):
    group = ob.TextGroup(group_id="BALAO_1", lines=list(lines))
    group.text = " ".join(line.text for line in lines)
    group.translation = translation
    group.translation_candidate = translation
    group.classification = classification
    group.source_completeness = {"status": "pass"}
    group.region_id = "REGION_001"
    group.source_engine = "synthetic"
    group.main_text_score = 1.0
    return group


def _texture_canvas(shape=(320, 900), seed=11, base=150, amplitude=45):
    rng = np.random.default_rng(seed)
    noise = cv2.GaussianBlur(rng.normal(0.0, amplitude / 2.0, shape), (0, 0), 3.0)
    plane = np.linspace(-amplitude, amplitude, shape[1])[None, :]
    gray = np.clip(base + noise * 2.0 + plane, 0, 255).astype(np.uint8)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def _outlined_lettering(canvas, text, origin, *, glyph=(20, 20, 20),
                        outline=(255, 255, 255), scale=2.6):
    """Draw the two visual components of outlined lettering: outline then body."""
    cv2.putText(canvas, text, origin, cv2.FONT_HERSHEY_SIMPLEX, scale,
                outline, 20, cv2.LINE_AA)
    cv2.putText(canvas, text, origin, cv2.FONT_HERSHEY_SIMPLEX, scale,
                glyph, 7, cv2.LINE_AA)
    return canvas


def _lettered_case(*, glyph=(20, 20, 20), outline=(255, 255, 255), seed=11,
                   base=150, text="STORY LINE HERE"):
    """A P5/P6-class region: outlined story lettering laid over textured art."""
    image = _texture_canvas(seed=seed, base=base)
    origin = (40, 200)
    _outlined_lettering(image, text, origin, glyph=glyph, outline=outline)
    (tw, th), baseline = cv2.getTextSize(
        text, cv2.FONT_HERSHEY_SIMPLEX, 2.6, 20)
    box = (origin[0] - 8, origin[1] - th - 12, tw + 16, th + baseline + 24)
    return image, _group([_line(text, box)])


def _detailed_lettered_case():
    """The real #84 page 5 class: outlined lettering over *detailed* artwork.

    Non-generative inpainting cannot rebuild the structure a large lettering
    footprint covers, so the reconstruction is safe and usable but not faithful.
    """
    image, group = _lettered_case()
    rng = np.random.default_rng(5)
    grain = rng.normal(0.0, 8.0, image.shape[:2])
    gray = np.clip(
        cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32) + grain,
        0, 255).astype(np.uint8)
    detailed = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    # Keep the lettering itself crisp: only the artwork carries the grain.
    lettering = ob._build_text_mask(image.shape, [group])
    letters = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    crisp = ((letters <= 60) | (letters >= 245)) & (lettering > 0)
    detailed[crisp] = image[crisp]
    return detailed, group


def _dark_display_lettered_case():
    """P002-class display lettering on a dark textured/gradient field."""

    image = np.full((900, 1400, 3), (20, 24, 30), dtype=np.uint8)
    for y in range(image.shape[0]):
        image[y, :, 0] = np.clip(30 + y * 0.08, 0, 255)
    rng = np.random.default_rng(84)
    stars = rng.integers(0, min(image.shape[:2]), size=(180, 2))
    for y, x in stars:
        cv2.circle(image, (int(x), int(y)), 1, (125, 180, 190), -1)
    text = "STORY LINE HERE"
    origin = (100, 450)
    cv2.putText(image, text, origin, cv2.FONT_HERSHEY_SIMPLEX, 2.0,
                (255, 245, 190), 18, cv2.LINE_AA)
    cv2.putText(image, text, origin, cv2.FONT_HERSHEY_SIMPLEX, 2.0,
                (255, 120, 40), 6, cv2.LINE_AA)
    (tw, th), baseline = cv2.getTextSize(
        text, cv2.FONT_HERSHEY_SIMPLEX, 2.0, 18)
    group = _group(
        [_line(text, (90, origin[1] - th - 18, tw + 36,
                     th + baseline + 36))],
        translation="TEXTO DE HISTORIA.",
    )
    group.classification = "narration"
    return image, group


def _ghost_silhouette_pixels(original, cleaned, group, cleanup_mask):
    """Test helper: source-shaped contrast still visible after cleanup.

    This intentionally does not OCR text.  It asks whether pixels in the source
    lettering footprint still carry a high-contrast silhouette compared with the
    local cleaned background.
    """

    envelope = ob._build_text_mask(original.shape, [group])
    footprint = ob.source_lettering_footprint(original, group, cleanup_mask, envelope)
    if not np.any(footprint):
        return 0
    cleaned_gray = cv2.cvtColor(cleaned, cv2.COLOR_BGR2GRAY)
    ring = cv2.dilate(envelope, np.ones((31, 31), np.uint8), iterations=1)
    ring = cv2.bitwise_and(ring, cv2.bitwise_not(envelope))
    if not np.any(ring):
        ring = cv2.bitwise_not(envelope)
    values = cleaned_gray[ring > 0]
    median = float(np.median(values)) if values.size else float(np.median(cleaned_gray))
    spread = max(18.0, float(np.std(values)) if values.size else 18.0)
    delta = np.abs(cleaned_gray.astype(np.float32) - median)
    ghost = (footprint > 0) & (delta > max(28.0, spread * 1.8))
    return int(np.count_nonzero(ghost))


def _render_page(image, group, page_index=1):
    """Drive the production render path, provenance recorder and all."""
    ocr_line_provenance.activate()
    try:
        with ocr_line_provenance.page(page_index):
            ocr_line_provenance.record_input_lines(
                group.lines, origin="analyze_input", reason="art_fidelity_test")
            ocr_line_provenance.record_group(group)
            final, _debug = ob._render_analyzed_image(
                image, [], [], [group], page_index=page_index)
    finally:
        ocr_line_provenance.deactivate()
    return final


def _cleanup(image, group, strategy="source_scoped"):
    return ob._remove_text_for_group(image.copy(), image, group, strategy=strategy)


class SourceLetteringFootprintContract(unittest.TestCase):
    """The footprint the cleanup owns is the lettering, not only its glyph body."""

    def test_footprint_covers_the_light_outline_around_dark_glyphs(self):
        image, group = _lettered_case()
        _cleaned, mask, _metrics = _cleanup(image, group)

        footprint = ob.source_lettering_footprint(
            image, group, mask, ob._build_text_mask(image.shape, [group]))

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        outline = (gray >= 245) & (
            cv2.dilate(mask, np.ones((9, 9), np.uint8)) > 0)
        covered = int(np.count_nonzero(outline & (footprint > 0)))
        self.assertGreater(int(outline.sum()), 500, "fixture drew no outline")
        self.assertGreaterEqual(
            covered / max(1, int(outline.sum())), 0.9,
            "source lettering outline is outside the cleanup footprint")

    def test_footprint_covers_a_light_glyph_with_a_dark_outline(self):
        image, group = _lettered_case(
            glyph=(250, 250, 250), outline=(15, 15, 15), base=140)
        _cleaned, mask, _metrics = _cleanup(image, group)

        footprint = ob.source_lettering_footprint(
            image, group, mask, ob._build_text_mask(image.shape, [group]))

        self.assertGreaterEqual(
            int(np.count_nonzero(footprint)), int(np.count_nonzero(mask)),
            "inverse-contrast lettering lost part of the cleanup footprint")

    def test_footprint_never_leaves_the_owned_source_evidence(self):
        image, group = _lettered_case()
        _cleaned, mask, _metrics = _cleanup(image, group)
        envelope = ob._build_text_mask(image.shape, [group])

        footprint = ob.source_lettering_footprint(image, group, mask, envelope)

        self.assertEqual(
            0, int(np.count_nonzero((footprint > 0) & (envelope == 0))),
            "footprint reached artwork outside the owned source evidence")

    def test_nearby_art_line_outside_the_lettering_is_preserved(self):
        image, group = _lettered_case()
        cv2.line(image, (40, 300), (860, 300), (30, 30, 30), 5)
        before = image.copy()
        cleaned, _mask, _metrics = _cleanup(image, group)

        art = np.zeros(image.shape[:2], np.uint8)
        cv2.line(art, (40, 300), (860, 300), 255, 5)
        changed = int(np.count_nonzero(
            (art > 0) & np.any(cleaned != before, axis=2)))
        self.assertLessEqual(
            changed / max(1, int(np.count_nonzero(art))), 0.05,
            "cleanup absorbed a nearby art line as source lettering")


class ResidualSourceLetteringContract(unittest.TestCase):
    """ART-RECON-001: surviving source lettering can never be reported clean."""

    def test_surviving_outline_is_detected_as_residual_source_lettering(self):
        image, group = _lettered_case()
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        # Reproduce the real #84 page 6 defect: only the dark glyph body was
        # removed, the light outline it sits inside stayed on the page.
        body = ((gray <= 90) & (
            ob._build_text_mask(image.shape, [group]) > 0)).astype(np.uint8) * 255
        cleaned = ob._apply_cleanup_mask(
            image.copy(), image, group, body, strategy="source_scoped")

        metrics = ob.residual_source_lettering_metrics(
            image, cleaned, group, body,
            ob._build_text_mask(image.shape, [group]))

        self.assertTrue(metrics["residual_source_lettering_detected"])
        self.assertGreater(metrics["residual_source_lettering_pixels"], 0)

    def test_surviving_glow_is_detected_as_a_source_shaped_ghost(self):
        image, group = _dark_display_lettered_case()
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        body = ((gray >= 130) & (
            ob._build_text_mask(image.shape, [group]) > 0)).astype(np.uint8) * 255
        cleaned = image.copy()
        cleaned[body > 0] = (24, 24, 28)

        ghost_pixels = _ghost_silhouette_pixels(image, cleaned, group, body)

        self.assertGreater(
            ghost_pixels,
            1200,
            "source-like glow silhouette was not detected without OCR readability",
        )

    def test_blurred_letter_shape_is_detected_as_a_source_shaped_ghost(self):
        image, group = _dark_display_lettered_case()
        envelope = ob._build_text_mask(image.shape, [group])
        blurred = cv2.GaussianBlur(image, (0, 0), 5.0)
        cleaned = image.copy()
        cleaned[envelope > 0] = blurred[envelope > 0]

        ghost_pixels = _ghost_silhouette_pixels(image, cleaned, group, envelope)

        self.assertGreater(
            ghost_pixels,
            1200,
            "blurred source lettering silhouette must be detected without OCR",
        )

    def test_full_footprint_cleanup_reports_no_residual_lettering(self):
        image, group = _lettered_case()
        cleaned, mask, _metrics = _cleanup(image, group)

        metrics = ob.residual_source_lettering_metrics(
            image, cleaned, group, mask,
            ob._build_text_mask(image.shape, [group]))

        self.assertFalse(
            metrics["residual_source_lettering_detected"],
            metrics)

    def test_untouched_art_beside_the_region_is_not_residual_lettering(self):
        image, group = _lettered_case()
        cv2.line(image, (40, 300), (860, 300), (30, 30, 30), 5)
        cleaned, mask, _metrics = _cleanup(image, group)

        metrics = ob.residual_source_lettering_metrics(
            image, cleaned, group, mask,
            ob._build_text_mask(image.shape, [group]))

        self.assertFalse(
            metrics["residual_source_lettering_detected"], metrics)

    def test_dark_display_cleanup_removes_the_full_source_owned_footprint(self):
        image, group = _dark_display_lettered_case()

        cleaned, mask, metrics = _cleanup(image, group)

        self.assertTrue(metrics.get("mask_valid"), metrics)
        self.assertTrue(metrics.get("source_scoped_display_footprint_required"), metrics)
        self.assertTrue(metrics.get("source_scoped_display_dark_evidence_cleanup"), metrics)
        self.assertEqual(int(metrics.get("residual_text_pixels_after_cleanup") or 0), 0)
        self.assertLessEqual(_ghost_silhouette_pixels(image, cleaned, group, mask), 800)

    def test_dark_gradient_background_is_not_a_ghost_by_itself(self):
        image = np.full((420, 900, 3), (15, 18, 25), dtype=np.uint8)
        for x in range(image.shape[1]):
            value = int(15 + x * 0.08)
            image[:, x] = (value + 10, value + 6, value)
        text = "STORY LINE HERE"
        line = _line(text, (120, 160, 520, 90))
        group = _group([line], translation="TEXTO DE HISTORIA.")
        mask = ob._build_text_mask(image.shape, [group])

        self.assertEqual(_ghost_silhouette_pixels(image, image.copy(), group, mask), 0)

    def test_residual_source_lettering_forbids_a_clean_art_verdict(self):
        attempts = [{
            "strategy": "source_scoped",
            "visual_validation_passed": True,
            "residual_source_lettering_detected": True,
            "residual_source_lettering_pixels": 4200,
        }]

        status, reason = ob.art_reconstruction_verdict(attempts, accepted=True)

        self.assertEqual(status, "review")
        self.assertEqual(reason, "residual_source_lettering_after_cleanup")


class ArtFidelityVerdictContract(unittest.TestCase):
    """An accepted reconstruction may still be honest about uncertain fidelity."""

    def test_accepted_reconstruction_without_findings_is_clean(self):
        attempts = [{"strategy": "primary", "visual_validation_passed": True}]

        self.assertEqual(
            ob.art_reconstruction_verdict(attempts, accepted=True),
            ("clean", ""))

    def test_uncertain_fidelity_downgrades_an_accepted_render_to_review(self):
        attempts = [{
            "strategy": "source_scoped",
            "visual_validation_passed": True,
            "art_fidelity_uncertain": True,
        }]

        status, reason = ob.art_reconstruction_verdict(attempts, accepted=True)

        self.assertEqual(status, "review")
        self.assertEqual(reason, "art_reconstruction_fidelity_uncertain")


class RenderDispositionContract(unittest.TestCase):
    """One canonical disposition driven by three independent decisions."""

    def test_clean_translation_and_clean_art_render_clean(self):
        self.assertEqual(
            ob.render_disposition(
                translation="clean", source_removed=True, art="clean"),
            (ob.RENDER_CLEAN, ""))

    def test_clean_translation_with_art_review_still_renders(self):
        disposition, reason = ob.render_disposition(
            translation="clean", source_removed=True, art="review",
            art_reason="art_reconstruction_fidelity_uncertain")

        self.assertEqual(disposition, ob.RENDER_WITH_REVIEW)
        self.assertEqual(reason, "art_reconstruction_fidelity_uncertain")

    def test_semantic_review_with_clean_art_renders_with_review(self):
        disposition, reason = ob.render_disposition(
            translation="review", source_removed=True, art="clean",
            translation_reason="source_ocr_suspicious:SLLM")

        self.assertEqual(disposition, ob.RENDER_WITH_REVIEW)
        self.assertEqual(reason, "source_ocr_suspicious:SLLM")

    def test_semantic_review_with_art_review_renders_with_combined_review(self):
        disposition, reason = ob.render_disposition(
            translation="review", source_removed=True, art="review",
            translation_reason="source_ocr_suspicious:SLLM",
            art_reason="art_reconstruction_fidelity_uncertain")

        self.assertEqual(disposition, ob.RENDER_WITH_REVIEW)
        self.assertIn("source_ocr_suspicious:SLLM", reason)
        self.assertIn("art_reconstruction_fidelity_uncertain", reason)

    def test_semantic_reject_never_renders_whatever_the_art_says(self):
        for art in ("clean", "review", "fail"):
            with self.subTest(art=art):
                disposition, _reason = ob.render_disposition(
                    translation="reject", source_removed=True, art=art)
                self.assertEqual(disposition, ob.DO_NOT_RENDER)

    def test_source_text_still_on_the_page_never_renders(self):
        disposition, reason = ob.render_disposition(
            translation="clean", source_removed=False, art="review")

        self.assertEqual(disposition, ob.DO_NOT_RENDER)
        self.assertEqual(reason, "source_text_not_removed")

    def test_destructive_art_never_renders(self):
        disposition, reason = ob.render_disposition(
            translation="clean", source_removed=True, art="fail",
            art_reason="large_white_patch_on_nonwhite_background")

        self.assertEqual(disposition, ob.DO_NOT_RENDER)
        self.assertEqual(reason, "large_white_patch_on_nonwhite_background")

    def test_review_disposition_always_carries_a_structured_reason(self):
        disposition, reason = ob.render_disposition(
            translation="clean", source_removed=True, art="review")

        self.assertEqual(disposition, ob.RENDER_WITH_REVIEW)
        self.assertTrue(reason, "render_with_review without a review reason")


class OutlinedLetteringRenderContract(unittest.TestCase):
    """End to end on the P5/P6 class: the candidate survives a safe cleanup."""

    def test_outlined_story_lettering_is_removed_without_a_white_patch(self):
        image, group = _lettered_case()

        _cleaned, _mask, metrics = _cleanup(image, group)

        self.assertTrue(metrics.get("mask_valid"), metrics.get("reason"))
        self.assertFalse(metrics.get("white_patch_rejected"), metrics)
        self.assertFalse(metrics.get("flat_patch_rejected"), metrics)
        self.assertFalse(metrics.get("seam_suspected"), metrics)
        self.assertFalse(
            metrics.get("residual_source_lettering_detected"), metrics)

    def test_removed_lettering_leaves_no_bright_glyph_ghost(self):
        image, group = _lettered_case()
        cleaned, mask, _metrics = _cleanup(image, group)

        original = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.int16)
        result = cv2.cvtColor(cleaned, cv2.COLOR_BGR2GRAY).astype(np.int16)
        ghost = (mask > 0) & (original < 220) & (result >= 244)
        self.assertLessEqual(
            int(ghost.sum()) / max(1, int(np.count_nonzero(mask))), 0.01,
            "cleanup repainted the glyph bodies as a bright ghost")


class ArtFidelityEvidenceContract(unittest.TestCase):
    """Fidelity is measured, not assumed, and it never withholds the render."""

    def test_reconstruction_far_below_the_surrounding_texture_is_uncertain(self):
        image, group = _detailed_lettered_case()

        _cleaned, _mask, metrics = _cleanup(image, group)

        self.assertTrue(metrics.get("mask_valid"), metrics.get("reason"))
        self.assertFalse(
            metrics.get("flat_patch_rejected"),
            "safety verdict must not change - this is a fidelity finding")
        self.assertTrue(
            metrics.get("art_fidelity_uncertain"),
            f"smoothed reconstruction of textured art reported as faithful: "
            f"{metrics.get('flat_patch_texture_ratio')}")

    def test_flat_balloon_reconstruction_is_not_marked_uncertain(self):
        image = np.full((320, 900, 3), 252, dtype=np.uint8)
        text = "REAL COFFEE."
        cv2.putText(image, text, (60, 200), cv2.FONT_HERSHEY_SIMPLEX, 2.6,
                    (20, 20, 20), 7, cv2.LINE_AA)
        (tw, th), baseline = cv2.getTextSize(
            text, cv2.FONT_HERSHEY_SIMPLEX, 2.6, 7)
        group = _group([_line(text, (52, 200 - th - 12, tw + 16,
                                     th + baseline + 24))],
                       classification="speech")

        _cleaned, _mask, metrics = _cleanup(image, group, strategy="primary")

        self.assertFalse(metrics.get("art_fidelity_uncertain"), metrics)

    def test_art_review_render_is_accounted_as_review_not_as_clean(self):
        image, group = _detailed_lettered_case()

        _final = _render_page(image, group)

        self.assertTrue(group.redrawn, "usable PT-BR was dropped for art review")
        self.assertEqual(group.art_reconstruction_status, "review")
        self.assertEqual(group.render_disposition, ob.RENDER_WITH_REVIEW)
        self.assertTrue(group.render_disposition_reason)
        self.assertEqual(group.translation_quality_impact, "review_required")
        self.assertTrue(group.manual_review_required)

    def test_uncertain_fidelity_only_counts_on_the_attempt_that_shipped(self):
        attempts = [
            {"strategy": "primary", "visual_validation_passed": False,
             "art_fidelity_uncertain": True,
             "reason": "broad_mask_rejected_on_nonuniform_background"},
            {"strategy": "conservative", "visual_validation_passed": True},
        ]

        self.assertEqual(
            ob.art_reconstruction_verdict(attempts, accepted=True),
            ("clean", ""))


if __name__ == "__main__":
    unittest.main()
