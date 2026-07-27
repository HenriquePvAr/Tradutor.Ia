import _test_bootstrap  # noqa: F401
import unittest

import numpy as np

import residual_mask_revisions as rmr


class AmbiguousComponentDecompositionTests(unittest.TestCase):
    def _layers(self):
        component = np.zeros((24, 24), np.uint8)
        component[8:16, 7:11] = 255
        component[11, 11:19] = 255
        core = np.zeros_like(component)
        outline = np.zeros_like(component)
        antialias = np.zeros_like(component)
        core[9:15, 8:10] = 255
        outline[8:16, 7:11] = 255
        antialias[8:16, 7:12] = 255
        protected = np.zeros_like(component)
        protected[11, 14:19] = 255
        return component, core, outline, antialias, protected

    def test_seed_derivation_keeps_text_art_and_unknown_separate(self):
        component, core, outline, antialias, protected = self._layers()
        seeds = rmr.derive_component_seeds(
            component_mask=component,
            text_core_mask=core,
            outline_mask=outline,
            antialias_mask=antialias,
            protected_edge_mask=protected,
        )
        self.assertGreater(np.count_nonzero(seeds["text_seed_mask"]), 0)
        self.assertGreater(np.count_nonzero(seeds["art_seed_mask"]), 0)
        self.assertGreater(np.count_nonzero(seeds["unknown_seed_mask"]), 0)
        self.assertEqual(
            np.count_nonzero(seeds["text_seed_mask"] & seeds["art_seed_mask"]), 0)

    def test_consensus_does_not_erase_protected_diagonal_art(self):
        component, core, outline, antialias, protected = self._layers()
        seeds = rmr.derive_component_seeds(
            component_mask=component, text_core_mask=core,
            outline_mask=outline, antialias_mask=antialias,
            protected_edge_mask=protected)
        result = rmr.decompose_ambiguous_component(
            component_mask=component, seeds=seeds,
            protected_edge_mask=protected)
        self.assertEqual(result["protected_overlap"], 0)
        self.assertEqual(
            np.count_nonzero(result["confirmed_text_mask"] & protected), 0)
        self.assertGreater(result["still_ambiguous_pixels"], 0)
        self.assertEqual(result["status"], "blocked_pending_component_review")

    def test_triple_layer_evidence_confirms_small_glyph_fragment(self):
        component = np.zeros((12, 12), np.uint8)
        component[4:6, 4:7] = 255
        empty = np.zeros_like(component)
        seeds = rmr.derive_component_seeds(
            component_mask=component, text_core_mask=component,
            outline_mask=component, antialias_mask=component,
            protected_edge_mask=empty)
        result = rmr.decompose_ambiguous_component(
            component_mask=component, seeds=seeds,
            protected_edge_mask=empty)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["confirmed_text_pixels"], 6)
        self.assertEqual(result["still_ambiguous_pixels"], 0)

    def test_seed_conflict_remains_unknown(self):
        component = np.zeros((8, 8), np.uint8)
        component[3:5, 3:5] = 255
        seeds = rmr.derive_component_seeds(
            component_mask=component, text_core_mask=component,
            outline_mask=component, antialias_mask=component,
            protected_edge_mask=component)
        result = rmr.decompose_ambiguous_component(
            component_mask=component, seeds=seeds,
            protected_edge_mask=component)
        self.assertEqual(result["confirmed_text_pixels"], 0)
        self.assertGreater(result["still_ambiguous_pixels"], 0)
        self.assertEqual(result["status"], "blocked_pending_component_review")

    def test_consensus_updates_only_the_safe_delta(self):
        shape = (10, 10)
        previous = np.zeros(shape, np.uint8)
        previous[1:3, 1:3] = 255
        residual = {
            "suggested_mask_delta": np.zeros(shape, np.uint8),
            "unresolved_residual_components": 1,
        }
        confirmed = np.zeros(shape, np.uint8)
        confirmed[4, 4] = 255
        decomposition = {
            "confirmed_text_mask": confirmed,
            "still_ambiguous_pixels": 0,
            "status": "confirmed",
            "reason_codes": [],
        }
        updated = rmr.apply_component_consensus(residual, decomposition)
        revised = rmr.build_revised_mask(previous, updated)
        self.assertEqual(revised["status"], "valid_for_local_preview_candidate")
        self.assertEqual(revised["delta_area"], 1)
        self.assertEqual(revised["protected_overlap"], 0)


if __name__ == "__main__":
    unittest.main()
