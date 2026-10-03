import _test_bootstrap  # noqa: F401

import unittest

import cv2
import numpy as np

import art_text_inpainting as ati


def _masks(size: int = 24):
    zero = np.zeros((size, size), np.uint8)
    fill = zero.copy()
    fill[10:14, 10:14] = 255
    outline = zero.copy()
    outline[9:15, 9:15] = 255
    outline[10:14, 10:14] = 0
    antialias = zero.copy()
    antialias[8:16, 8:16] = 255
    antialias[9:15, 9:15] = 0
    return zero, fill, outline, antialias


class ContaminationContractTests(unittest.TestCase):
    def test_contract_is_deterministic_and_covers_all_source_layers(self):
        zero, fill, outline, antialias = _masks()
        kwargs = dict(
            text_fill_mask=fill,
            outline_mask=outline,
            antialias_mask=antialias,
            shadow_mask=zero,
            residual_mask=zero,
            protected_structure_mask=zero,
            identity={"source_hash": "source"},
        )
        first = ati.build_contamination_contract(**kwargs)
        second = ati.build_contamination_contract(**kwargs)
        self.assertEqual(
            first["contamination_contract_id"],
            second["contamination_contract_id"])
        self.assertEqual(
            first["hashes"]["contamination_moat_hash"],
            second["hashes"]["contamination_moat_hash"])
        for layer in (fill, outline, antialias):
            self.assertEqual(
                np.count_nonzero(
                    (layer > 0)
                    & ~(first["contaminated_context_mask"] > 0)),
                0,
            )
        self.assertEqual(
            np.count_nonzero(
                (first["clean_context_mask"] > 0)
                & (first["contaminated_context_mask"] > 0)),
            0,
        )

    def test_moat_is_derived_from_observed_layers(self):
        zero, fill, outline, antialias = _masks()
        contract = ati.build_contamination_contract(
            text_fill_mask=fill,
            outline_mask=outline,
            antialias_mask=antialias,
            shadow_mask=zero,
            residual_mask=zero,
        )
        self.assertEqual(
            contract["moat_method"],
            "observed_stroke_antialias_shadow_distance")
        self.assertGreater(
            contract["moat_pixel_count"],
            np.count_nonzero(contract["contaminated_context_mask"]))


class VerifiedDonorPoolTests(unittest.TestCase):
    def test_donor_pool_rejects_contamination_target_and_uncertain_pixels(self):
        zero, fill, outline, antialias = _masks()
        uncertain = zero.copy()
        uncertain[1, 1] = 255
        contract = ati.build_contamination_contract(
            text_fill_mask=fill,
            outline_mask=outline,
            antialias_mask=antialias,
            shadow_mask=zero,
            residual_mask=zero,
        )
        target = contract["contaminated_context_mask"]
        pool = ati.build_verified_donor_pool(
            contamination_contract=contract,
            target_mask=target,
            uncertain_mask=uncertain,
        )
        eligible = pool["donor_eligibility_mask"] > 0
        self.assertEqual(pool["contamination_overlap"], 0)
        self.assertEqual(pool["target_pixels_eligible"], 0)
        self.assertFalse(eligible[1, 1])

    def test_empty_allowed_scope_fails_closed(self):
        zero, fill, outline, antialias = _masks()
        contract = ati.build_contamination_contract(
            text_fill_mask=fill,
            outline_mask=outline,
            antialias_mask=antialias,
            shadow_mask=zero,
            residual_mask=zero,
        )
        pool = ati.build_verified_donor_pool(
            contamination_contract=contract,
            target_mask=fill,
            allowed_scope_mask=zero,
        )
        self.assertEqual(pool["status"], "blocked")
        self.assertIn("verified_donor_pool_empty", pool["reason_codes"])

    def test_verified_reconstruction_uses_no_target_or_contaminated_donor(self):
        zero, fill, outline, antialias = _masks()
        image = np.zeros((24, 24, 3), np.uint8)
        image[:, :] = [80, 100, 120]
        image[fill > 0] = [1, 2, 3]
        contract = ati.build_contamination_contract(
            text_fill_mask=fill,
            outline_mask=outline,
            antialias_mask=antialias,
            shadow_mask=zero,
            residual_mask=zero,
        )
        pool = ati.build_verified_donor_pool(
            contamination_contract=contract, target_mask=fill)
        result = ati.reconstruct_from_verified_donors(
            image, target_mask=fill, donor_pool=pool)
        self.assertEqual(result["status"], "valid")
        manifest = result["sampling_manifest"]
        self.assertEqual(manifest["contaminated_samples_used"], 0)
        self.assertEqual(manifest["target_pixels_used_as_donors"], 0)
        self.assertEqual(manifest["unresolved_target_pixels"], 0)
        residual = ati.detect_post_reconstruction_residuals(
            image, result["image"], source_mask=fill)
        self.assertEqual(residual["post_reconstruction_residual_pixels"], 0)

    def test_sanitized_context_inpainting_keeps_sampling_clean(self):
        zero, fill, outline, antialias = _masks()
        image = np.zeros((24, 24, 3), np.uint8)
        image[:, :, 0] = np.arange(24, dtype=np.uint8)[None, :] * 5
        image[:, :, 1] = 90
        image[:, :, 2] = 140
        image[fill > 0] = [0, 0, 0]
        contract = ati.build_contamination_contract(
            text_fill_mask=fill,
            outline_mask=outline,
            antialias_mask=antialias,
            shadow_mask=zero,
            residual_mask=zero,
        )
        pool = ati.build_verified_donor_pool(
            contamination_contract=contract, target_mask=fill)
        result = ati.reconstruct_with_sanitized_context(
            image,
            target_mask=fill,
            contamination_contract=contract,
            donor_pool=pool,
            method=cv2.INPAINT_TELEA,
            radius=2,
        )
        self.assertEqual(result["status"], "valid")
        self.assertEqual(
            result["sampling_manifest"]["contaminated_samples_used"], 0)
        self.assertEqual(
            result["sampling_manifest"]["target_pixels_used_as_donors"], 0)

    def test_selection_prefers_zero_residual_over_higher_scoring_candidate(self):
        common = {
            "edge_continuity_score": 1.0,
            "texture_consistency_score": 1.0,
            "seam_score": 0.0,
            "artifact_score": 0.0,
            "changed_pixels_outside_change_mask": 0,
            "source_contaminated_samples_used": 0,
        }
        selected = ati.select_best_candidate([
            {
                **common, "method": "contaminated", "overall_score": 1.0,
                "post_reconstruction_residual_pixels": 1,
                "post_reconstruction_residual_components": 1,
            },
            {
                **common, "method": "clean", "overall_score": 0.8,
                "post_reconstruction_residual_pixels": 0,
                "post_reconstruction_residual_components": 0,
            },
        ])
        self.assertEqual(selected["method"], "clean")
        self.assertEqual(selected["status"], "passed")


if __name__ == "__main__":
    unittest.main()
