"""Mission 10 Review Manifest V1 acceptance tests."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from artifact_contract import ArtifactType, OutputProfile, profile_artifacts
from review_manifest import (
    build_review_manifest, validate_review_manifest, write_review_manifest,
)


def raw_item(region_id="grp_2", *, text="HELLO", translation="OLÁ", **extra):
    return {"id": region_id, "region_id": region_id, "classification": "dialogue",
            "text": text, "translation": translation, "manual_review_required": True,
            "confidence": 0.92, **extra}


class ReviewManifestTests(unittest.TestCase):
    def build(self, **kwargs):
        base = {"logical_page_count": 1, "job_outcome": "review_required",
                "artifact_root": tempfile.gettempdir(), "quality_report": {"pages": []}}
        base.update(kwargs)
        return build_review_manifest(**base)

    def test_perfect_job_has_zero_items_and_complete_pages(self):
        manifest = self.build(job_outcome="finished", logical_page_count=3)
        self.assertEqual(manifest["summary"], {"logical_pages": 3, "pages_represented": 3,
                         "items_total": 0, "items_pending": 0, "items_approved": 0,
                         "items_resolved": 0})
        self.assertFalse(manifest["review_required"])

    def test_review_item_uses_reviewitem_model_and_persisted_overlay(self):
        quality = {"pages": [{"index": 1, "translation_terminal_items": [raw_item()]}]}
        manifest = self.build(quality_report=quality, effective_review_items=[{
            "page_index": 1, "region_id": "p001:grp_2", "source_text_effective": "HELLO",
            "target_text_original": "OLÁ", "target_text_effective": "OI!",
            "state": "edited", "revision_version": 4,
        }])
        item = manifest["pages"][0]["review_items"][0]
        self.assertEqual(item["provider_translation"], "OLÁ")
        self.assertEqual(item["translated_text_effective"], "OI!")
        self.assertEqual(item["review_status"], "RESOLVED")
        self.assertEqual(item["provenance"]["human_revision_version"], 4)

    def test_approved_and_resolved_are_item_states_not_job_outcomes(self):
        quality = {"pages": [{"index": 1, "translation_terminal_items": [
            raw_item("r-approved"), raw_item("r-resolved")]}]}
        manifest = self.build(quality_report=quality, effective_review_items=[
            {"page_index": 1, "region_id": "r-approved", "state": "approved"},
            {"page_index": 1, "region_id": "r-resolved", "state": "resolved"},
        ])
        self.assertEqual(manifest["job_outcome"], "review_required")
        self.assertEqual(manifest["summary"]["items_approved"], 1)
        self.assertEqual(manifest["summary"]["items_resolved"], 1)

    def test_warning_severity_and_unicode_multiline_roundtrip(self):
        text = "Ol\u00e1, a\u00e7\u00e3o \u2014 \u201ccora\u00e7\u00e3o\u201d\nsegunda linha"
        manifest = self.build(quality_report={"pages": [{"index": 1,
            "suspicious_groups": [raw_item(text=text, translation="Tudo bem?\nSim!",
                                               translation_stale=True,
                                               quality_reasons=["ENGLISH_RESIDUAL"])]}]})
        item = manifest["pages"][0]["review_items"][0]
        self.assertEqual(item["source_text"], text)
        self.assertEqual(item["translated_text_effective"], "Tudo bem?\nSim!")
        self.assertIn("ENGLISH_RESIDUAL", item["warnings"])
        self.assertIn("SEMANTIC_VALIDATION", item["warnings"])
        self.assertEqual(item["quality_severity"], "REVIEW")

    def test_actual_editable_psd_writer_layer_provenance_links(self):
        import editable_psd_writer as writer
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "psd").mkdir()
            (root / "psd" / "001.psd").write_bytes(b"psd fixture")
            image = np.full((32, 80, 3), 255, dtype=np.uint8)
            psd = root / "psd" / "095.psd"
            result = writer.write_editable_psd(psd, image, image, [
                writer.EditableTextRegion(region_id="grp_2", semantic_type="dialogue",
                    text="OLÁ", source_text="HELLO", translation_status="translated",
                    bbox=(2, 2, 40, 20), font_size=12)
            ], (80, 32))
            manifest = build_review_manifest(
                quality_report={"pages": [{"index": 1,
                    "translation_terminal_items": [raw_item()]}]},
                logical_page_count=1, job_outcome="review_required", artifact_root=root,
                psd_provenance={1: {"path": str(psd), "text_layers": result["text_layers"]}},
            )
            item = manifest["pages"][0]["review_items"][0]
            self.assertEqual(item["psd_artifact"], "psd/095.psd")
            self.assertEqual(item["psd_layer_name"], result["text_layers"][0]["name"])

    def test_source_fallback_is_not_misrepresented_as_translation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "psd").mkdir()
            (root / "psd" / "001.psd").write_bytes(b"psd fixture")
            manifest = self.build(artifact_root=root, quality_report={"pages": [{"index": 1,
                "translation_terminal_items": [raw_item(text="THANK YOU", translation="",
                                                           translation_candidate="GRACIAS",
                                                           preserved_original=True)]}]},
                psd_provenance={1: {"path": str(root / "psd" / "001.psd"), "text_layers": [{
                    "name": "BALAO_004_REVIEW", "region_id": "grp_2", "semantic_type": "dialogue",
                    "translation_status": "source_fallback", "bbox": [2, 2, 40, 20],
                }]}})
        item = manifest["pages"][0]["review_items"][0]
        self.assertEqual(item["translation_status"], "SOURCE_FALLBACK")
        self.assertEqual(item["provider_translation"], "GRACIAS")
        self.assertEqual(item["translated_text_effective"], "THANK YOU")
        self.assertEqual(item["warnings"], ["SOURCE_FALLBACK"])
        self.assertEqual(item["review_status"], "PENDING")

    def test_legacy_missing_region_id_uses_opaque_id_and_no_fake_region_id(self):
        manifest = self.build(quality_report={"pages": [{"index": 1,
            "text_overflow_items": [{"id": "legacy-1", "classification": "dialogue",
                "text": "SOURCE", "translation": "TARGET", "text_overflow_ratio": 0.2,
                "bounding_box": [1, 2, 10, 8]}]}]})
        item = manifest["pages"][0]["review_items"][0]
        self.assertIsNone(item["region_id"])
        self.assertTrue(item["review_item_id"].startswith("legacy:"))
        self.assertIn("legacy", item["provenance"]["region_link_strategy"])

    def test_deterministic_order_duplicate_validation_and_unsafe_paths(self):
        quality = {"pages": [{"index": 1, "suspicious_groups": [
            raw_item("b", bounding_box=[5, 20, 3, 3]),
            raw_item("a", bounding_box=[1, 10, 3, 3]),
        ]}]}
        one = self.build(quality_report=quality)
        two = self.build(quality_report=quality)
        self.assertEqual(one, two)
        self.assertEqual([i["region_id"] for i in one["pages"][0]["review_items"]], ["a", "b"])
        bad = json.loads(json.dumps(one))
        bad["pages"][0]["translated_artifact"] = "..\\escape.png"
        with self.assertRaises(ValueError):
            validate_review_manifest(bad)
        for path in ("C:/outside.png", "C:\\outside.png", "\\\\server\\share\\outside.png"):
            bad = json.loads(json.dumps(one))
            bad["pages"][0]["translated_artifact"] = path
            with self.assertRaises(ValueError):
                validate_review_manifest(bad)
        bad = json.loads(json.dumps(one))
        bad["pages"][0]["review_items"].append(dict(bad["pages"][0]["review_items"][0]))
        bad["summary"]["items_total"] += 1
        with self.assertRaises(ValueError):
            validate_review_manifest(bad)

    def test_208_pages_blank_short_and_no_text_coverage(self):
        report_pages = [{"index": i, "suspicious_groups": [raw_item(f"r{i}") ] if i == 48 else []}
                        for i in range(1, 209)]
        manifest = self.build(logical_page_count=208, quality_report={"pages": report_pages})
        self.assertEqual(manifest["summary"]["pages_represented"], 208)
        self.assertEqual(manifest["pages"][117]["review_items"], [])
        self.assertEqual(manifest["pages"][142]["review_items"], [])
        self.assertEqual(manifest["pages"][206]["review_items"], [])

    def test_profiles_only_request_manifest_for_professional(self):
        self.assertIn(ArtifactType.REVIEW_MANIFEST, profile_artifacts(OutputProfile.PROFESSIONAL_QC))
        self.assertNotIn(ArtifactType.REVIEW_MANIFEST, profile_artifacts(OutputProfile.AUTOMATIC))

    def test_atomic_utf8_write_and_secret_key_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "psd").mkdir()
            (root / "psd" / "001.psd").write_bytes(b"psd fixture")
            manifest = self.build(artifact_root=root, quality_report={"pages": [{"index": 1,
                "suspicious_groups": [raw_item(text="a\u00e7\u00e3o\n\u00e7\u00e3", translation="\u201cOK\u201d")]}]})
            path = root / "qc" / "review_manifest.json"
            write_review_manifest(path, manifest)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), manifest)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["pages"][0]["review_items"][0]["source_text"],
                             "a\u00e7\u00e3o\n\u00e7\u00e3")
            secret = json.loads(json.dumps(manifest))
            secret["access_token"] = "do-not-write"
            with self.assertRaises(ValueError):
                validate_review_manifest(secret)


if __name__ == "__main__":
    unittest.main()
