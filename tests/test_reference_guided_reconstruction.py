import _test_bootstrap  # noqa: F401

import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

import reference_guided_reconstruction as rgr


class ReferenceInventoryTests(unittest.TestCase):
    def test_inventory_is_owner_scoped_and_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reference.png"
            cv2.imwrite(str(path), np.full((20, 30, 3), 100, np.uint8))
            result = rgr.inventory_authorized_references([
                {"path": path, "owner": "owner", "authorized": True,
                 "source_type": "raw_source", "asset_role": "source"},
                {"path": path, "owner": "other", "authorized": True},
            ], owner="owner", source_hash="not-identical")
            self.assertEqual(result["usable_candidate_count"], 1)
            self.assertEqual(len(result["rejected"]), 1)

    def test_missing_or_unauthorized_reference_is_rejected(self):
        result = rgr.inventory_authorized_references([
            {"path": "missing.png", "owner": "owner", "authorized": False}
        ], owner="owner", source_hash="source")
        self.assertEqual(result["usable_candidate_count"], 0)


class ReferenceAlignmentTests(unittest.TestCase):
    def test_alignment_is_deterministic_for_translated_art(self):
        source = np.zeros((160, 180, 3), np.uint8)
        rng = np.random.default_rng(42)
        for _ in range(80):
            x, y = rng.integers(10, 150, size=2)
            cv2.circle(source, (int(x), int(y)), 2, (255, 255, 255), -1)
        matrix = np.float32([[1, 0, 5], [0, 1, 7]])
        reference = cv2.warpAffine(
            source, matrix, (180, 160), borderMode=cv2.BORDER_CONSTANT)
        first = rgr.align_reference(source, reference)
        second = rgr.align_reference(source, reference)
        self.assertEqual(first["status"], "valid")
        self.assertEqual(first["alignment_id"], second["alignment_id"])

    def test_featureless_reference_fails_closed(self):
        blank = np.zeros((80, 80, 3), np.uint8)
        result = rgr.align_reference(blank, blank)
        self.assertEqual(result["status"], "reference_alignment_failed")


class ReferenceTransferTests(unittest.TestCase):
    def test_transfer_stays_inside_authorized_mask(self):
        source = np.zeros((20, 20, 3), np.uint8)
        reference = np.full_like(source, 200)
        mask = np.zeros((20, 20), np.uint8)
        mask[5:10, 5:10] = 255
        result = rgr.transfer_observable_art(
            source, reference, authorized_change_mask=mask,
            observable_art_mask=np.full_like(mask, 255),
            reference_text_mask=np.zeros_like(mask),
            uncertainty_mask=np.zeros_like(mask))
        self.assertEqual(result["outside_mask_changes"], 0)
        self.assertEqual(result["observed_background_pixels"], 25)

    def test_reference_text_and_uncertainty_are_never_transferred(self):
        source = np.zeros((20, 20, 3), np.uint8)
        reference = np.full_like(source, 200)
        mask = np.full((20, 20), 255, np.uint8)
        text = np.zeros_like(mask)
        text[:10] = 255
        uncertain = np.zeros_like(mask)
        uncertain[10:] = 255
        result = rgr.transfer_observable_art(
            source, reference, authorized_change_mask=mask,
            observable_art_mask=mask, reference_text_mask=text,
            uncertainty_mask=uncertain)
        self.assertEqual(result["observed_background_pixels"], 0)
        self.assertEqual(result["reference_text_transferred"], 0)


class StructuralDiffTests(unittest.TestCase):
    def test_expected_text_change_is_separate_from_art_change(self):
        original = np.zeros((10, 10, 3), np.uint8)
        translated = original.copy()
        translated[2:4, 2:4] = 255
        expected = np.zeros((10, 10), np.uint8)
        expected[2:4, 2:4] = 255
        result = rgr.structural_diff(
            original, translated, expected_text_mask=expected)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["unexpected_art_change_pixels"], 0)

    def test_unexpected_art_change_blocks(self):
        original = np.zeros((10, 10, 3), np.uint8)
        translated = original.copy()
        translated[0, 0] = 255
        result = rgr.structural_diff(
            original, translated,
            expected_text_mask=np.zeros((10, 10), np.uint8))
        self.assertEqual(result["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
