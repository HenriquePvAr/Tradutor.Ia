import _test_bootstrap  # noqa: F401

import unittest

import numpy as np

import art_text_inpainting as ati


class TextureConsistencyMetricTests(unittest.TestCase):
    def setUp(self):
        self.mask = np.ones((32, 32), np.uint8) * 255

    def test_identical_gradient_scores_high(self):
        gradient = np.tile(
            np.arange(32, dtype=np.uint8)[None, :] * 7, (32, 1))
        image = np.dstack([gradient, gradient, gradient])
        result = ati.evaluate_texture_consistency(
            image, image,
            evaluation_mask=self.mask, reference_mask=self.mask)
        self.assertEqual(result["status"], "valid")
        self.assertGreater(result["normalized_score"], 0.95)

    def test_fragmented_mosaic_scores_below_coherent_gradient(self):
        gradient = np.tile(
            np.arange(32, dtype=np.uint8)[None, :] * 7, (32, 1))
        reference = np.dstack([gradient, gradient, gradient])
        mosaic = reference.copy()
        mosaic[::2, ::2] = 255 - mosaic[::2, ::2]
        coherent = ati.evaluate_texture_consistency(
            reference, reference,
            evaluation_mask=self.mask, reference_mask=self.mask)
        fragmented = ati.evaluate_texture_consistency(
            mosaic, reference,
            evaluation_mask=self.mask, reference_mask=self.mask)
        self.assertLess(
            fragmented["normalized_score"], coherent["normalized_score"])
        self.assertGreater(fragmented["fragmentation_score"], 0)

    def test_empty_roi_is_explicitly_invalid(self):
        image = np.zeros((8, 8, 3), np.uint8)
        empty = np.zeros((8, 8), np.uint8)
        result = ati.evaluate_texture_consistency(
            image, image, evaluation_mask=empty, reference_mask=self.mask[:8, :8])
        self.assertEqual(result["status"], "invalid")
        self.assertIsNone(result["normalized_score"])
        self.assertIn("texture_metric_empty_roi", result["reason_codes"])

    def test_nan_input_is_explicitly_invalid(self):
        image = np.zeros((8, 8, 3), np.float32)
        image[0, 0, 0] = np.nan
        mask = np.ones((8, 8), np.uint8) * 255
        result = ati.evaluate_texture_consistency(
            image, image, evaluation_mask=mask, reference_mask=mask)
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(result["nan_detected"])
        self.assertIsNone(result["normalized_score"])

    def test_uint8_and_float_constant_inputs_are_valid(self):
        mask = np.ones((8, 8), np.uint8) * 255
        for dtype in (np.uint8, np.float32):
            image = np.full((8, 8, 3), 80, dtype)
            result = ati.evaluate_texture_consistency(
                image, image, evaluation_mask=mask, reference_mask=mask)
            self.assertEqual(result["status"], "valid")
            self.assertGreater(result["normalized_score"], 0.95)


if __name__ == "__main__":
    unittest.main()
