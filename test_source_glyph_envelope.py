import _test_bootstrap  # noqa: F401

import unittest

import cv2
import numpy as np

import source_glyph_envelope as sge


def fixture():
    image = np.full((48, 72, 3), 140, np.uint8)
    seed = np.zeros(image.shape[:2], np.uint8)
    seed[20:28, 30:38] = 255
    fill = seed.copy()
    outline = cv2.dilate(seed, np.ones((5, 5), np.uint8)) & ~seed
    antialias = cv2.dilate(seed, np.ones((7, 7), np.uint8))
    antialias &= ~cv2.dilate(seed, np.ones((5, 5), np.uint8))
    protected = np.zeros_like(seed)
    return image, seed, fill, outline, antialias, protected


class SourceGlyphVisualEnvelopeTests(unittest.TestCase):
    def test_complete_envelope_expands_from_seed_using_layers(self):
        image, seed, fill, outline, antialias, protected = fixture()
        result = sge.derive_source_glyph_visual_envelope(
            source_bgr=image, confirmed_text_seed=seed,
            fill_evidence=fill, outline_evidence=outline,
            antialias_evidence=antialias, protected_art=protected)
        self.assertEqual(result["status"], "confirmed")
        self.assertGreater(np.count_nonzero(result["complete_glyph_mask"]),
                           np.count_nonzero(seed))
        self.assertGreater(np.count_nonzero(result["outer_outline_mask"]), 0)
        self.assertGreater(np.count_nonzero(result["antialias_mask"]), 0)

    def test_identity_is_content_addressed_and_deterministic(self):
        image, seed, fill, outline, antialias, protected = fixture()
        kwargs = dict(
            source_bgr=image, confirmed_text_seed=seed,
            fill_evidence=fill, outline_evidence=outline,
            antialias_evidence=antialias, protected_art=protected,
            identity={"owner": "owner", "source_hash": "source"})
        self.assertEqual(
            sge.derive_source_glyph_visual_envelope(**kwargs)["glyph_envelope_id"],
            sge.derive_source_glyph_visual_envelope(**kwargs)["glyph_envelope_id"])

    def test_protected_conflict_is_uncertain_and_blocks(self):
        image, seed, fill, outline, antialias, protected = fixture()
        protected[19, 29:39] = 255
        result = sge.derive_source_glyph_visual_envelope(
            source_bgr=image, confirmed_text_seed=seed,
            fill_evidence=fill, outline_evidence=outline,
            antialias_evidence=antialias, protected_art=protected)
        self.assertEqual(result["status"], "blocked_pending_glyph_envelope_review")
        self.assertGreater(result["unknown_pixel_count"], 0)
        self.assertEqual(result["protected_overlap"], 0)
        self.assertEqual(np.count_nonzero(
            (result["complete_glyph_mask"] > 0) & (protected > 0)), 0)

    def test_protected_edge_connected_to_seed_is_not_silently_art(self):
        image, seed, fill, outline, antialias, protected = fixture()
        protected[20:28, 38] = 255
        result = sge.derive_source_glyph_visual_envelope(
            source_bgr=image, confirmed_text_seed=seed,
            fill_evidence=fill, outline_evidence=outline,
            antialias_evidence=antialias, protected_art=protected)
        self.assertEqual(
            result["status"], "blocked_pending_glyph_envelope_review")
        self.assertGreater(result["unknown_pixel_count"], 0)

    def test_unconnected_layer_is_not_forced_into_envelope(self):
        image, seed, fill, outline, antialias, protected = fixture()
        outline[2:5, 2:5] = 255
        result = sge.derive_source_glyph_visual_envelope(
            source_bgr=image, confirmed_text_seed=seed,
            fill_evidence=fill, outline_evidence=outline,
            antialias_evidence=antialias, protected_art=protected)
        self.assertEqual(np.count_nonzero(
            result["complete_glyph_mask"][2:5, 2:5]), 0)


class GlobalRetainedSourceDetectorTests(unittest.TestCase):
    def test_retained_outline_outside_old_change_mask_is_detected(self):
        image, seed, fill, outline, antialias, _protected = fixture()
        envelope = fill | outline | antialias
        candidate = image.copy()
        candidate[seed > 0] = [1, 2, 3]
        result = sge.detect_retained_source_globally(
            image, candidate, change_mask=seed, glyph_envelope=envelope,
            validation_halo=cv2.dilate(envelope, np.ones((3, 3), np.uint8)),
            fill_mask=fill, outline_mask=outline,
            antialias_mask=antialias)
        self.assertEqual(result["status"], "blocked")
        self.assertGreater(result["retained_source_outside_change_mask"], 0)
        self.assertGreater(result["retained_outline_pixels"], 0)

    def test_fully_changed_envelope_passes(self):
        image, seed, fill, outline, antialias, _protected = fixture()
        envelope = fill | outline | antialias
        candidate = image.copy()
        candidate[envelope > 0] = [1, 2, 3]
        halo = envelope.copy()
        result = sge.detect_retained_source_globally(
            image, candidate, change_mask=envelope,
            glyph_envelope=envelope, validation_halo=halo,
            fill_mask=fill, outline_mask=outline,
            antialias_mask=antialias)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["retained_source_inside_visual_envelope"], 0)

    def test_unchanged_background_in_halo_is_not_source_glyph_evidence(self):
        image, seed, fill, outline, antialias, _protected = fixture()
        envelope = fill | outline | antialias
        candidate = image.copy()
        candidate[envelope > 0] = [1, 2, 3]
        result = sge.detect_retained_source_globally(
            image, candidate, change_mask=envelope,
            glyph_envelope=envelope,
            validation_halo=cv2.dilate(
                envelope, np.ones((9, 9), np.uint8)),
            fill_mask=fill, outline_mask=outline,
            antialias_mask=antialias)
        self.assertEqual(result["retained_source_inside_halo"], 0)
        self.assertEqual(result["status"], "passed")


if __name__ == "__main__":
    unittest.main()
