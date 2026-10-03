"""TDD #84F29 - optional advanced art inpainting adapter contracts.

The LaMa-family model is a local external asset.  These tests pin the safety
contract without requiring the heavy model for the ordinary suite: no model
means unavailable/review, and a hash mismatch fails closed before inference.
"""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import hashlib
import tempfile
import unittest
from pathlib import Path

import numpy as np

import advanced_art_inpainting
import config


class AdvancedArtInpaintingIntegrityTests(unittest.TestCase):
    def tearDown(self):
        advanced_art_inpainting.reset_default_inpainter_for_tests()

    def test_missing_model_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing.pt"
            inpainter = advanced_art_inpainting.AdvancedArtInpainter(
                model_path=missing,
            )

            self.assertFalse(inpainter.available())
            with self.assertRaises(advanced_art_inpainting.AdvancedInpaintUnavailable):
                inpainter.verify_integrity()

    def test_hash_mismatch_fails_closed_before_model_load(self):
        old_hash = config.ADVANCED_ART_INPAINT_MODEL_SHA256
        try:
            config.ADVANCED_ART_INPAINT_MODEL_SHA256 = "0" * 64
            with tempfile.TemporaryDirectory() as temp_dir:
                model = Path(temp_dir) / "model.pt"
                model.write_bytes(b"not a torchscript model")
                inpainter = advanced_art_inpainting.AdvancedArtInpainter(
                    model_path=model,
                )

                self.assertTrue(inpainter.available())
                with self.assertRaises(
                    advanced_art_inpainting.AdvancedInpaintIntegrityError
                ):
                    inpainter.verify_integrity()
        finally:
            config.ADVANCED_ART_INPAINT_MODEL_SHA256 = old_hash

    def test_matching_hash_is_verified_without_loading_model(self):
        old_hash = config.ADVANCED_ART_INPAINT_MODEL_SHA256
        payload = b"hash-only fixture"
        digest = hashlib.sha256(payload).hexdigest()
        try:
            config.ADVANCED_ART_INPAINT_MODEL_SHA256 = digest
            with tempfile.TemporaryDirectory() as temp_dir:
                model = Path(temp_dir) / "model.pt"
                model.write_bytes(payload)
                inpainter = advanced_art_inpainting.AdvancedArtInpainter(
                    model_path=model,
                )

                self.assertEqual(inpainter.verify_integrity(), digest)
                self.assertIsNone(inpainter._model)
                self.assertEqual(inpainter.metrics["hash_count"], 1)
                # Repeated stage-boundary verification must use the cached
                # digest while the model file is unchanged.
                self.assertEqual(inpainter.verify_integrity(), digest)
                self.assertEqual(inpainter.metrics["hash_count"], 1)
                self.assertEqual(inpainter.metrics["verify_count"], 2)
        finally:
            config.ADVANCED_ART_INPAINT_MODEL_SHA256 = old_hash

    def test_file_change_invalidates_integrity_cache(self):
        old_hash = config.ADVANCED_ART_INPAINT_MODEL_SHA256
        first = b"hash-only fixture"
        second = b"changed fixture"
        first_digest = hashlib.sha256(first).hexdigest()
        second_digest = hashlib.sha256(second).hexdigest()
        try:
            config.ADVANCED_ART_INPAINT_MODEL_SHA256 = first_digest
            with tempfile.TemporaryDirectory() as temp_dir:
                model = Path(temp_dir) / "model.pt"
                model.write_bytes(first)
                inpainter = advanced_art_inpainting.AdvancedArtInpainter(
                    model_path=model,
                )
                self.assertEqual(inpainter.verify_integrity(), first_digest)
                self.assertEqual(inpainter.metrics["hash_count"], 1)
                model.write_bytes(second)
                config.ADVANCED_ART_INPAINT_MODEL_SHA256 = second_digest
                self.assertEqual(inpainter.verify_integrity(), second_digest)
                self.assertEqual(inpainter.metrics["hash_count"], 2)
        finally:
            config.ADVANCED_ART_INPAINT_MODEL_SHA256 = old_hash

    def test_reconstruct_rejects_empty_mask_before_model_access(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing.pt"
            inpainter = advanced_art_inpainting.AdvancedArtInpainter(
                model_path=missing,
            )
            image = np.zeros((8, 8, 3), dtype=np.uint8)
            mask = np.zeros((8, 8), dtype=np.uint8)

            with self.assertRaises(ValueError):
                inpainter.reconstruct(image, mask)


if __name__ == "__main__":
    unittest.main()
