import _test_bootstrap  # noqa: F401
import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

import residual_analysis_artifacts as raa
import residual_mask_revisions as rmr


def make_bundle(mask=None, contract=None):
    if mask is None:
        mask = np.zeros((12, 16), np.uint8)
        mask[1:4, 2:5] = 255
        mask[7:10, 11:15] = 255
    if contract is None:
        contract = raa.default_detector_contract(
            detector_name="test_detector", detector_version="1",
            implementation_revision="test-revision")
    return raa.create_residual_analysis(
        residual_bitmap=mask, owner="owner", job_id="job", run_id="run",
        revision_id="revision", page_id="page", region_id="region",
        source_page_hash="a" * 64, source_crop_hash="b" * 64,
        previous_preview_hash="c" * 64, base_mask_hash="d" * 64,
        validation_halo_hash="e" * 64, ocr_geometry_hash="f" * 64,
        detector_contract=contract, image_dimensions=list(mask.shape),
        pixel_origin=[0, 0], supersedes_for_future_processing="legacy")


class ResidualBitmapContractTests(unittest.TestCase):
    def test_bitmap_roundtrip_is_byte_exact(self):
        bundle = make_bundle()
        decoded = raa.decode_canonical_bitmap(bundle["bitmap_bytes"])
        self.assertTrue(np.array_equal(decoded, bundle["bitmap"]))
        self.assertEqual(
            raa.canonical_bitmap_bytes(decoded), bundle["bitmap_bytes"])

    def test_one_pixel_changes_bitmap_and_analysis_hash(self):
        first = make_bundle()
        changed = first["bitmap"].copy()
        changed[0, 0] = 255
        second = make_bundle(changed)
        self.assertNotEqual(
            first["artifact"]["residual_bitmap_hash"],
            second["artifact"]["residual_bitmap_hash"])
        self.assertNotEqual(
            first["artifact"]["residual_analysis_id"],
            second["artifact"]["residual_analysis_id"])

    def test_parameter_change_changes_analysis_id(self):
        first = make_bundle()
        contract = copy.deepcopy(first["artifact"]["detector_contract"])
        contract["threshold_contract"]["binary_foreground"] = "value_ge_128"
        second = make_bundle(contract=contract)
        self.assertNotEqual(
            first["artifact"]["residual_analysis_id"],
            second["artifact"]["residual_analysis_id"])

    def test_defaults_are_materialized(self):
        contract = raa.default_detector_contract(
            detector_name="detector", detector_version="2",
            implementation_revision="commit")
        for key in (
                "color_space", "dtype", "connectivity", "threshold_contract",
                "morphology_contract", "operation_order",
                "component_extraction_contract", "component_sort_contract",
                "library_versions"):
            self.assertIn(key, contract)


class ResidualComponentIdentityTests(unittest.TestCase):
    def test_component_ids_are_content_addressed_and_order_independent(self):
        bundle = make_bundle()
        manifest = bundle["component_manifest"]
        self.assertEqual(len(manifest["component_ids"]), 2)
        reversed_components = list(reversed(manifest["components"]))
        self.assertEqual(
            sorted(item["component_id"] for item in reversed_components),
            manifest["component_ids"])

    def test_component_union_is_exact_and_disjoint(self):
        manifest = make_bundle()["component_manifest"]
        self.assertTrue(manifest["union_matches_residual"])
        self.assertTrue(manifest["components_are_disjoint"])

    def test_bitmap_mismatch_fails_closed(self):
        bundle = make_bundle()
        damaged = bytearray(bundle["bitmap_bytes"])
        damaged[-1] ^= 1
        bundle["bitmap_bytes"] = bytes(damaged)
        with self.assertRaisesRegex(ValueError, "payload_hash_mismatch"):
            raa.validate_analysis_bundle(bundle)

    def test_component_manifest_mismatch_fails_closed(self):
        bundle = make_bundle()
        bundle["artifact"]["component_manifest_hash"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "component_manifest_hash_mismatch"):
            raa.validate_analysis_bundle(bundle)

    def test_decomposition_validates_component_hash(self):
        component = np.zeros((8, 8), np.uint8)
        component[2:5, 2:5] = 255
        seeds = rmr.derive_component_seeds(
            component_mask=component, text_core_mask=component,
            outline_mask=component, antialias_mask=component)
        with self.assertRaisesRegex(ValueError, "component_bitmap_hash_mismatch"):
            rmr.decompose_ambiguous_component(
                component_mask=component, seeds=seeds,
                residual_analysis_id="analysis", component_id="component",
                expected_component_bitmap_hash="0" * 64)

    def test_store_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.sqlite3"
            store = raa.ResidualAnalysisStore(db)
            bundle = make_bundle()
            first = store.persist(bundle, residual_bitmap_asset="asset.bin")
            second = store.persist(bundle, residual_bitmap_asset="asset.bin")
            self.assertEqual(
                first["residual_analysis_id"], second["residual_analysis_id"])
            count = store._conn.execute(
                "SELECT COUNT(*) FROM residual_analyses").fetchone()[0]
            self.assertEqual(count, 1)
            store.close()

    def test_reopen_restores_same_analysis_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "restore.sqlite3"
            bundle = make_bundle()
            store = raa.ResidualAnalysisStore(db)
            store.persist(bundle, residual_bitmap_asset="asset.bin")
            store.close()
            reopened = raa.ResidualAnalysisStore(db)
            restored = reopened.latest_for_region(
                owner="owner", job_id="job", run_id="run",
                revision_id="revision", region_id="region")
            self.assertEqual(
                restored["residual_analysis_id"],
                bundle["artifact"]["residual_analysis_id"])
            self.assertEqual(
                restored["residual_bitmap_hash"],
                bundle["artifact"]["residual_bitmap_hash"])
            reopened.close()

    def test_store_can_read_from_request_worker_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "threaded.sqlite3"
            store = raa.ResidualAnalysisStore(db)
            try:
                bundle = make_bundle()
                store.persist(bundle, residual_bitmap_asset="asset.bin")

                with ThreadPoolExecutor(max_workers=1) as executor:
                    restored = executor.submit(
                        store.latest_for_region,
                        owner="owner",
                        job_id="job",
                        run_id="run",
                        revision_id="revision",
                        region_id="region",
                    ).result()

                self.assertEqual(
                    restored["residual_analysis_id"],
                    bundle["artifact"]["residual_analysis_id"],
                )
            finally:
                store.close()


class ResidualDeterminismTests(unittest.TestCase):
    def test_same_inputs_produce_identical_manifests(self):
        first = make_bundle()
        second = make_bundle()
        self.assertEqual(first["bitmap_bytes"], second["bitmap_bytes"])
        self.assertEqual(first["artifact"], second["artifact"])
        self.assertEqual(
            first["component_manifest"], second["component_manifest"])

    def test_independent_processes_produce_same_identity(self):
        code = (
            "import json;"
            "from test_residual_analysis_identity import make_bundle;"
            "b=make_bundle();"
            "print(json.dumps([b['artifact']['residual_analysis_id'],"
            "b['artifact']['residual_bitmap_hash'],"
            "b['artifact']['component_manifest_hash']]))"
        )
        outputs = [
            subprocess.check_output(
                [sys.executable, "-c", code], text=True).strip()
            for _ in range(2)
        ]
        self.assertEqual(outputs[0], outputs[1])

    def test_successor_is_not_silently_equivalent_to_legacy(self):
        artifact = make_bundle()["artifact"]
        self.assertEqual(artifact["analysis_generation"], "successor_analysis")
        self.assertNotIn("equivalent_to_legacy_analysis", artifact)


if __name__ == "__main__":
    unittest.main()
