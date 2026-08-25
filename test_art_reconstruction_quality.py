"""Art reconstruction quality contracts (TDD #81).

Source-text removal and art reconstruction are two independent verdicts.  These
contracts pin the second one: a flat colour fill is only allowed where the local
artwork is provably one tone, an OCR line quadrilateral is never a cleanup
strategy on illustration, the glyph footprint has to include outline/glow, and a
reconstruction that flattens textured artwork is rejected rather than shipped.

Most of it is synthetic; the two production-parity cases replay the real Page 5
and Page 25 sentinels from a persisted run, read only.  Nothing here contacts a
provider or the network - the offline guard fails any attempt.
"""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import os
import unittest

import cv2
import numpy as np

import config
import ocr_balloon as ob
from ocr_engine import OCRLine

# Persisted real run used as a read-only visual sentinel.  Only the source page
# images are read, and only into memory: the run itself is never rewritten.  The
# geometry below is the OCR line evidence that run recorded, kept inline so no
# copyrighted page has to live in the repository.
_SENTINEL_RUN = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "output", "shadow_slave_chapter_1_5",
    "72347726-3697-4357-8b77-c4b58cc8b0ea",
)


def _sentinel_page(page_no):
    path = os.path.join(_SENTINEL_RUN, "smart_input_pages", f"page_{page_no:03d}.png")
    return cv2.imread(path) if os.path.exists(path) else None


def _line(text, box, *, line_id="L0"):
    x, y, w, h = box
    polygon = np.array(
        [[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.int32)
    return OCRLine(
        text=text, confidence=0.97, polygon=polygon, box=(x, y, w, h),
        raw_text=text, engine="synthetic", page=1,
        metadata={"ocr_line_id": line_id})


def _group(lines, *, classification="speech", translation="TEXTO"):
    group = ob.TextGroup(group_id="BALAO_1", lines=list(lines))
    group.text = " ".join(line.text for line in lines)
    group.translation = translation
    group.translation_candidate = translation
    group.classification = classification
    group.source_completeness = {"status": "pass"}
    group.region_id = "REGION_001"
    group.source_engine = "synthetic"
    return group


def _draw_text(image, text, origin, *, scale=2.4, color=(20, 20, 20),
               thickness=6, outline=None, outline_thickness=14):
    if outline is not None:
        cv2.putText(image, text, origin, cv2.FONT_HERSHEY_SIMPLEX, scale,
                    outline, outline_thickness, cv2.LINE_AA)
    cv2.putText(image, text, origin, cv2.FONT_HERSHEY_SIMPLEX, scale, color,
                thickness, cv2.LINE_AA)
    return image


def _flat_canvas(shape=(400, 700), level=255):
    return np.full((shape[0], shape[1], 3), level, dtype=np.uint8)


def _texture_canvas(shape=(400, 700), seed=17, base=170, amplitude=55):
    rng = np.random.default_rng(seed)
    noise = rng.normal(0.0, amplitude / 2.0, shape)
    noise = cv2.GaussianBlur(noise, (0, 0), 3.0)
    plane = np.linspace(-amplitude, amplitude, shape[1])[None, :]
    gray = np.clip(base + noise * 2.0 + plane, 0, 255).astype(np.uint8)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def _gradient_canvas(shape=(400, 700), low=90, high=245):
    ramp = np.linspace(low, high, shape[0])[:, None]
    gray = np.repeat(ramp, shape[1], axis=1).astype(np.uint8)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


class FlatFillSafetyRule(unittest.TestCase):
    """A single colour may only replace artwork proven to be a single colour."""

    def test_flat_balloon_interior_supports_flat_fill(self):
        image = _flat_canvas()
        _draw_text(image, "REAL COFFEE.", (60, 230))
        mask = np.zeros(image.shape[:2], np.uint8)
        mask[170:250, 50:640] = 255
        evidence = ob._local_background_flatness(image, mask)
        self.assertTrue(evidence["flat_fill_supported"])
        self.assertLessEqual(
            evidence["flat_fill_ring_luminance_spread"],
            config.MAX_FLAT_FILL_RING_SPREAD,
        )

    def test_textured_artwork_never_supports_flat_fill(self):
        image = _texture_canvas()
        mask = np.zeros(image.shape[:2], np.uint8)
        mask[170:250, 50:640] = 255
        evidence = ob._local_background_flatness(image, mask)
        self.assertFalse(evidence["flat_fill_supported"])
        self.assertGreater(
            evidence["flat_fill_ring_luminance_spread"],
            config.MAX_FLAT_FILL_RING_SPREAD,
        )

    def test_gradient_artwork_never_supports_flat_fill(self):
        image = _gradient_canvas()
        mask = np.zeros(image.shape[:2], np.uint8)
        mask[170:250, 50:640] = 255
        evidence = ob._local_background_flatness(image, mask)
        self.assertFalse(evidence["flat_fill_supported"])

    def test_cleanup_on_textured_art_does_not_paint_one_colour(self):
        image = _texture_canvas()
        _draw_text(image, "NIGHTMARE", (60, 230), color=(30, 30, 220))
        group = _group([_line("NIGHTMARE", (50, 160, 590, 100))])
        group.background_type = "textured_art"
        group.background_metrics = {
            "open_light_art_caption": True,
            "brightness_mean": 200.0,
            "dark_pixel_ratio": 0.02,
        }
        mask = np.zeros(image.shape[:2], np.uint8)
        mask[160:260, 50:640] = 255
        cleaned = ob._apply_cleanup_mask(
            image, image, group, mask, strategy="source_scoped")
        filled = cleaned[mask > 0].reshape(-1, 3)
        self.assertGreater(
            float(cv2.cvtColor(
                filled.reshape(1, -1, 3), cv2.COLOR_BGR2GRAY).std()),
            6.0,
            "textured artwork was replaced by a near-uniform patch",
        )


class RectangleIsNotAMaskStrategy(unittest.TestCase):
    """OCR line quadrilaterals may not become the cleanup mask on artwork."""

    def test_line_quadrilateral_mask_rejected_on_textured_background(self):
        image = _texture_canvas()
        _draw_text(image, "BY THE", (60, 230), color=(30, 30, 220))
        group = _group([_line("BY THE", (50, 160, 590, 100))])
        group.background_metrics = {
            "strongly_uniform_white": False,
            "strict_uniform_light": True,
            "uniform_light": True,
            "open_light_art_caption": True,
            "brightness_mean": 200.0,
            "dark_pixel_ratio": 0.02,
        }
        group.background_type = "textured_art"
        mask, metrics = ob._uniform_light_line_text_mask(image, group)
        self.assertTrue(metrics.get("uniform_light_line_rejected"))
        self.assertEqual(int(np.count_nonzero(mask)), 0)

    def test_line_quadrilateral_mask_allowed_on_proven_flat_balloon(self):
        image = _flat_canvas()
        _draw_text(image, "REAL COFFEE.", (60, 230))
        group = _group([_line("REAL COFFEE.", (50, 160, 590, 100))])
        group.background_type = "white_balloon"
        group.background_metrics = {
            "strongly_uniform_white": True,
            "strict_uniform_light": True,
            "uniform_light": True,
            "dominant_white_enclosure": True,
        }
        mask, metrics = ob._uniform_light_line_text_mask(image, group)
        self.assertFalse(metrics.get("uniform_light_line_rejected"))
        self.assertGreater(int(np.count_nonzero(mask)), 0)


class FlatPatchDetector(unittest.TestCase):
    """The validator fails a synthetic block even when the source text is gone."""

    def test_flat_patch_over_texture_is_rejected(self):
        image = _texture_canvas()
        group = _group([_line("NIGHTMARE", (50, 160, 590, 100))])
        mask = np.zeros(image.shape[:2], np.uint8)
        mask[160:260, 50:640] = 255
        cleaned = image.copy()
        cleaned[mask > 0] = (238, 238, 238)
        metrics = ob._flat_patch_artifact_metrics(image, cleaned, group, mask)
        self.assertTrue(metrics["flat_patch_rejected"])
        self.assertLess(
            metrics["flat_patch_texture_ratio"],
            config.MAX_FLAT_PATCH_TEXTURE_RATIO,
        )

    def test_genuine_white_balloon_is_not_flagged(self):
        image = _flat_canvas()
        _draw_text(image, "REAL COFFEE.", (60, 230))
        group = _group([_line("REAL COFFEE.", (50, 160, 590, 100))])
        mask = np.zeros(image.shape[:2], np.uint8)
        mask[170:250, 50:640] = 255
        cleaned = image.copy()
        cleaned[mask > 0] = (255, 255, 255)
        metrics = ob._flat_patch_artifact_metrics(image, cleaned, group, mask)
        self.assertFalse(metrics["flat_patch_rejected"])

    def test_texture_preserving_reconstruction_is_not_flagged(self):
        image = _texture_canvas()
        group = _group([_line("NIGHTMARE", (50, 160, 590, 100))])
        mask = np.zeros(image.shape[:2], np.uint8)
        mask[160:260, 50:640] = 255
        cleaned = cv2.inpaint(image, mask, 3, cv2.INPAINT_TELEA)
        metrics = ob._flat_patch_artifact_metrics(image, cleaned, group, mask)
        self.assertFalse(metrics["flat_patch_rejected"])


class GlyphFootprintCoverage(unittest.TestCase):
    """Body, outline and glow belong to the source footprint; art does not."""

    def test_footprint_padding_scales_with_lettering(self):
        small = _group([_line("texto", (10, 10, 120, 18))])
        large = _group([_line("NIGHTMARE", (10, 10, 555, 111))])
        base = min(config.MAX_MASK_EXPANSION, config.TEXT_MASK_PADDING + 1)
        self.assertEqual(ob._glyph_footprint_padding(small, base), base)
        self.assertGreater(ob._glyph_footprint_padding(large, base), base)
        self.assertLessEqual(
            ob._glyph_footprint_padding(large, base),
            config.MAX_MASK_EXPANSION,
        )

    def test_punctuation_outside_the_polygon_is_still_covered(self):
        image = _flat_canvas(shape=(220, 620))
        _draw_text(image, "COFFEE.", (40, 150), scale=3.0, thickness=8)
        dark = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) < 128
        xs = np.where(dark.any(axis=0))[0]
        # The OCR polygon stops a few pixels short of the final full stop, which
        # is exactly the geometry that used to leave a crescent ghost behind.
        line = _line("COFFEE.", (int(xs.min()) - 4, 90, int(xs.max() - xs.min()) - 2, 80))
        group = _group([line])
        group.background_type = "white_balloon"
        group.inside_balloon_like_region = True
        cleaned, mask, metrics = ob._remove_text_for_group(
            image, image, group, strategy="primary")
        self.assertTrue(metrics["mask_valid"], metrics.get("reason"))
        survivors = cv2.cvtColor(cleaned, cv2.COLOR_BGR2GRAY) < 200
        self.assertEqual(
            int(survivors.sum()), 0,
            "source glyph pixels survived outside the OCR polygon",
        )

    def test_outlined_lettering_leaves_no_halo(self):
        image = _texture_canvas(shape=(300, 700), base=120, amplitude=35)
        _draw_text(image, "SLUMS", (60, 200), scale=3.0, color=(20, 20, 20),
                   thickness=8, outline=(250, 250, 250), outline_thickness=14)
        group = _group([_line("SLUMS", (50, 120, 600, 110))])
        group.background_type = "textured_art"
        masks, _metrics = ob._component_text_mask(
            image,
            group,
            maximum_mask=ob._build_text_mask(image.shape, [group]),
        )
        outline, _outline_metrics = ob._outlined_light_text_mask(
            image,
            group,
            maximum_mask=ob._build_text_mask(image.shape, [group]),
        )
        covered = cv2.bitwise_or(masks, outline) > 0
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        halo = (gray >= 238) & ~covered
        halo[:120, :] = False
        halo[230:, :] = False
        self.assertLess(
            int(halo.sum()), 400,
            "the bright outline of the source lettering was left behind",
        )


class OvermaskProtection(unittest.TestCase):
    """Covering the text is not enough; neighbouring artwork has to survive."""

    def test_cleanup_never_reaches_far_outside_the_owned_geometry(self):
        image = _flat_canvas(shape=(400, 700))
        _draw_text(image, "REAL COFFEE.", (60, 230))
        cv2.line(image, (40, 340), (660, 340), (10, 10, 10), 5)
        group = _group([_line("REAL COFFEE.", (50, 160, 590, 100))])
        group.background_type = "white_balloon"
        group.inside_balloon_like_region = True
        cleaned, mask, metrics = ob._remove_text_for_group(
            image, image, group, strategy="primary")
        self.assertTrue(metrics["mask_valid"], metrics.get("reason"))
        self.assertEqual(
            int(np.count_nonzero(mask[330:350, :])), 0,
            "protected line art was pulled into the cleanup mask",
        )
        self.assertTrue(
            np.array_equal(cleaned[330:350, :], image[330:350, :]),
            "protected line art was destroyed by the cleanup",
        )


class ReconstructionAccounting(unittest.TestCase):
    """Art reconstruction is reported separately from story-text coverage."""

    def test_accepted_region_is_clean(self):
        status, reason = ob.art_reconstruction_verdict([], accepted=True)
        self.assertEqual(status, "clean")
        self.assertEqual(reason, "")

    def test_rejected_region_carries_its_reconstruction_reason(self):
        attempts = [
            {"reason": "source_scoped_requires_translation_candidate"},
            {"reason": "flat_reconstruction_patch_on_textured_background"},
        ]
        status, reason = ob.art_reconstruction_verdict(attempts, accepted=False)
        self.assertEqual(status, "review")
        self.assertEqual(reason, "flat_reconstruction_patch_on_textured_background")

    def test_unclassified_failure_still_routes_to_structured_review(self):
        status, reason = ob.art_reconstruction_verdict(
            [{"reason": "something_else"}], accepted=False)
        self.assertEqual(status, "review")
        self.assertEqual(reason, ob.REVIEW_REQUIRED_ART_RECONSTRUCTION)


class Page25ProductionParity(unittest.TestCase):
    """The real Page 25 sentinel: display lettering over textured smoke.

    The persisted run recorded ``uniform_light_line_pixels: 127863`` for this
    group - the three OCR quadrilaterals - and filled them with one colour, which
    is the light rectangular patch that broke the artwork.  The background is
    smooth rather than noisy, so the coarse texture classifier called it
    ``uniform_light``; the evidence that separates it from a real balloon is the
    luminance spread of the surrounding ring, measured here at ~42 against real
    balloons at or under ~24.
    """

    # Real OCR line evidence recorded by the persisted run for BALAO_1.
    LINES = [
        ("BY THE", (253, 1098, 292, 106), "L2e5a7d1b612511d4"),
        ("NIGHTMARE", (122, 1242, 555, 111), "L163b564e8ab29f4b"),
        ("SPELL", (264, 1390, 269, 115), "L7f87dd15621d2c62"),
    ]
    # Background metrics the run recorded: smooth, bright, no edges - which is
    # exactly why the coarse classifier trusted it.
    BACKGROUND_METRICS = {
        "brightness_mean": 233.952, "brightness_std": 13.683,
        "white_pixel_ratio": 0.851, "dark_pixel_ratio": 0.0,
        "local_texture_mean": 0.373, "edge_density": 0.0,
        "gradient_strength": 5.843, "background_type": "textured_art",
        "uniform_light": True, "strict_uniform_light": True,
        "strongly_uniform_white": False, "dominant_white_enclosure": True,
        "stylized_white_enclosure": True, "open_light_art_caption": True,
    }

    def setUp(self):
        self.image = _sentinel_page(25)
        if self.image is None:
            self.skipTest("persisted Page 25 sentinel not available")
        self.group = _group(
            [_line(text, box, line_id=lid) for text, box, lid in self.LINES],
            translation="PELO FEITICO DO PESADELO",
        )
        self.group.background_type = "textured_art"
        self.group.background_metrics = dict(self.BACKGROUND_METRICS)

    def test_line_quadrilaterals_are_not_the_cleanup_mask(self):
        mask, metrics = ob._uniform_light_line_text_mask(self.image, self.group)
        self.assertTrue(
            metrics.get("uniform_light_line_rejected"),
            "the three OCR quadrilaterals were accepted as the cleanup mask again",
        )
        self.assertEqual(int(np.count_nonzero(mask)), 0)
        self.assertGreater(
            metrics["flat_fill_ring_luminance_spread"],
            config.MAX_FLAT_FILL_RING_SPREAD,
            "the smoke around the lettering was measured as flat",
        )

    def test_textured_smoke_is_never_replaced_by_one_colour(self):
        # Feed the exact rectangular mask the old pipeline built, so the fill
        # decision - not the mask - is what this contract measures.
        rect = np.zeros(self.image.shape[:2], np.uint8)
        for _text, (x, y, w, h), _lid in self.LINES:
            rect[y:y + h, x:x + w] = 255
        cleaned = ob._apply_cleanup_mask(
            self.image, self.image, self.group, rect, strategy="source_scoped")
        filled = cleaned[rect > 0].reshape(-1, 3)
        self.assertGreater(
            len(np.unique(filled, axis=0)), 1,
            "127863 pixels of textured smoke were painted a single colour",
        )
        self.assertGreater(
            float(cv2.cvtColor(
                filled.reshape(1, -1, 3), cv2.COLOR_BGR2GRAY).std()),
            4.0,
            "the reconstruction collapsed the smoke into a flat patch",
        )

    def test_flat_patch_detector_fails_the_old_reconstruction(self):
        rect = np.zeros(self.image.shape[:2], np.uint8)
        for _text, (x, y, w, h), _lid in self.LINES:
            rect[y:y + h, x:x + w] = 255
        old = self.image.copy()
        old[rect > 0] = (238, 238, 238)  # what the old flat fill produced
        metrics = ob._flat_patch_artifact_metrics(self.image, old, self.group, rect)
        self.assertTrue(
            metrics["flat_patch_rejected"],
            "the validator passed the Page 25 flat rectangle",
        )


class Page5ProductionParity(unittest.TestCase):
    """The real Page 5 sentinel: outlined lettering over city artwork.

    The persisted run left ``uncovered_source_text_pixels: 110`` with a largest
    surviving component of 53 px behind - the visible ghost - and still reported
    ``source_text_coverage: 0.998`` as a pass.  Measured on the same pixels the
    old mask leaves 161 uncovered source pixels here; the contract is that the
    glyph footprint now covers the outline instead of clipping it.
    """

    LINES = [
        ("NOT THE CHEAP", (170, 1620, 465, 72), "Ld59877078a16469b"),
        ("SYNTHETIC STUFF I'M", (83, 1714, 637, 78), "Lb8e0d67838ca3f95"),
        ("USED TO GETTING", (138, 1807, 527, 81), "L7ad4382988494b9c"),
        ("IN THE SLUMS.", (177, 1909, 441, 76), "L92fa0f8ad8c33ed7"),
    ]
    BACKGROUND_METRICS = {
        "brightness_mean": 125.376, "brightness_std": 49.493,
        "white_pixel_ratio": 0.0153, "dark_pixel_ratio": 0.2489,
        "local_texture_mean": 3.334, "edge_density": 0.0593,
        "gradient_strength": 34.924, "background_type": "textured_art",
        "uniform_light": False, "strict_uniform_light": False,
        "strongly_uniform_white": False, "open_light_art_caption": False,
    }

    def setUp(self):
        self.image = _sentinel_page(5)
        if self.image is None:
            self.skipTest("persisted Page 5 sentinel not available")
        self.group = _group(
            [_line(text, box, line_id=lid) for text, box, lid in self.LINES],
            translation="NAO E A PORCARIA SINTETICA BARATA",
        )
        self.group.background_type = "textured_art"
        self.group.background_metrics = dict(self.BACKGROUND_METRICS)

    def test_source_lettering_footprint_leaves_no_ghost(self):
        _cleaned, mask, _metrics = ob._remove_text_for_group(
            self.image, self.image, self.group, strategy="primary")
        evidence = ob._uncovered_source_text_evidence(
            self.image, self.image, self.group, mask)
        self.assertTrue(evidence["measured"])
        # The old mask left 161 uncovered source pixels, largest component 15.
        self.assertLess(
            int(evidence["uncovered_source_text_pixels"]), 60,
            "source lettering survived the cleanup mask as a ghost",
        )
        self.assertLess(
            int(evidence["largest_uncovered_source_component"]), 12,
            "a connected fragment of the source lettering survived",
        )

    def test_cleanup_does_not_swallow_the_surrounding_artwork(self):
        _cleaned, mask, _metrics = ob._remove_text_for_group(
            self.image, self.image, self.group, strategy="primary")
        owned = ob._build_text_mask(self.image.shape, [self.group])
        outside = int(np.count_nonzero((mask > 0) & (owned == 0)))
        self.assertEqual(
            outside, 0,
            "the cleanup mask reached outside the owned source geometry",
        )


if __name__ == "__main__":
    unittest.main()
