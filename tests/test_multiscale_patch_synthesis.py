import _test_bootstrap  # noqa: F401

import unittest

import numpy as np

import multiscale_patch_synthesis as mps


class MultiscalePatchSynthesisTests(unittest.TestCase):
    def _fixture(self):
        y, x = np.mgrid[0:32, 0:32]
        gray = ((x * 4 + y * 2) % 180 + 30).astype(np.uint8)
        image = np.dstack([gray, gray, gray])
        target = np.zeros((32, 32), np.uint8)
        target[12:20, 12:20] = 255
        donor = np.ones((32, 32), np.uint8) * 255
        donor[8:24, 8:24] = 0
        return image, target, donor

    def test_profile_and_schedule_are_content_addressed_and_deterministic(self):
        image, target, donor = self._fixture()
        first = mps.analyze_texture_profile(image, donor)
        second = mps.analyze_texture_profile(image, donor)
        self.assertEqual(first["texture_profile_id"], second["texture_profile_id"])
        schedule1 = mps.derive_patch_schedule(first, target)
        schedule2 = mps.derive_patch_schedule(second, target)
        self.assertEqual(schedule1["patch_schedule_id"], schedule2["patch_schedule_id"])
        self.assertTrue(all(level["patch_size"] % 2 for level in schedule1["levels"]))

    def test_empty_donor_context_fails_closed(self):
        image, _target, donor = self._fixture()
        donor[:] = 0
        result = mps.analyze_texture_profile(image, donor)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("insufficient_clean_texture_context", result["reason_codes"])

    def test_pyramid_hashes_repeat(self):
        image, target, donor = self._fixture()
        profile = mps.analyze_texture_profile(image, donor)
        schedule = mps.derive_patch_schedule(profile, target)
        first = mps.build_deterministic_pyramid(image, target, donor, schedule)
        second = mps.build_deterministic_pyramid(image, target, donor, schedule)
        self.assertEqual(first["pyramid_id"], second["pyramid_id"])
        self.assertEqual(first["level_hashes"], second["level_hashes"])

    def test_synthesis_never_changes_outside_or_uses_target_as_donor(self):
        image, target, donor = self._fixture()
        contaminated = image.copy()
        contaminated[target > 0] = 0
        profile = mps.analyze_texture_profile(contaminated, donor)
        schedule = mps.derive_patch_schedule(profile, target)
        result = mps.synthesize_multiscale(
            contaminated,
            target_mask=target,
            donor_eligibility_mask=donor,
            patch_schedule=schedule,
        )
        manifest = result["sampling_manifest"]
        self.assertEqual(manifest["changed_pixels_outside_mask"], 0)
        self.assertEqual(manifest["target_pixels_used_as_donors"], 0)
        self.assertEqual(manifest["contaminated_samples_used"], 0)
        self.assertEqual(manifest["source_text_similar_samples_used"], 0)


if __name__ == "__main__":
    unittest.main()
