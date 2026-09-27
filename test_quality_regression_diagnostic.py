from __future__ import annotations

import json
from pathlib import Path
import unittest

from quality_regression_diagnostic import (
    classify_item, normalize_artifact, progress_page_snapshot, review_to_expectation,
)


class DiagnosticBoundaryTests(unittest.TestCase):
    def test_all_required_failure_boundaries(self):
        cases = [
            ({"raw_line_present": False}, "DETECTION_OR_OCR_MISS"),
            ({"ocr_empty": True}, "OCR_EMPTY"),
            ({"filtered_pre_group": True, "low_confidence": True}, "OCR_LOW_CONFIDENCE_FILTER"),
            ({"filtered_pre_group": True}, "FILTERED_PRE_GROUP"),
            ({"grouping_dropped": True}, "GROUPING_DROP"),
            ({"classifier_ignored": True}, "CLASSIFIER_IGNORED"),
            ({"ignored_reason": "weak_unknown_text"}, "WEAK_UNKNOWN_REJECTED"),
            ({"classification": "sfx"}, "SFX_PRESERVED"),
            ({"translation_eligible": False}, "NOT_TRANSLATION_ELIGIBLE"),
            ({"translation_eligible": True, "translation_result": ""}, "TRANSLATION_EMPTY_OR_FAILED"),
            ({"render_excluded": True}, "RENDER_EXCLUDED"),
            ({"render_failed": True}, "RENDER_FAILED"),
            ({"output_present": True}, "OUTPUT_PRESENT"),
            ({}, "UNKNOWN_BOUNDARY"),
        ]
        for item, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(classify_item(item), expected)

    def test_missing_expected_text_does_not_claim_detector_failure(self):
        out = normalize_artifact({"case_id": "synthetic", "expected": [{"text": "I AM HERE"}], "regions": []})
        self.assertEqual(out["missing_expected_text"][0]["failure_boundary"], "DETECTION_OR_OCR_MISS")
        self.assertIn("detector vs OCR is unresolved", out["missing_expected_text"][0]["evidence_note"])

    def test_output_is_stable_and_sorted_by_page_y_x_region(self):
        payload = {"regions": [
            {"page_index": 2, "region_id": "b", "bbox": [5, 2, 1, 1]},
            {"page_index": 1, "region_id": "z", "bbox": [8, 9, 1, 1]},
            {"page_index": 1, "region_id": "a", "bbox": [9, 9, 1, 1]},
        ]}
        first = json.dumps(normalize_artifact(payload), sort_keys=True)
        second = json.dumps(normalize_artifact(payload), sort_keys=True)
        self.assertEqual(first, second)
        self.assertEqual([row["region_id"] for row in normalize_artifact(payload)["regions"]], ["z", "a", "b"])

    def test_boundary_matrix_fixture_matches_classifier(self):
        fixture = Path(__file__).parent / "test_fixtures" / "quality_regression_diagnostics" / "missed_balloon_boundary_matrix.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        for case in payload["cases"]:
            with self.subTest(case=case["case"]):
                self.assertEqual(classify_item(case["input"]), case["expected"])

    def test_i1_fixture_is_explicitly_post_ocr_and_contains_both_sides(self):
        fixture = Path(__file__).parent / "test_fixtures" / "quality_regression_diagnostics" / "i_1_post_ocr_synthetic.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertEqual(payload["evidence_kind"], "POST_OCR_SYNTHETIC_FIXTURE")
        self.assertEqual(len(payload["positive_contexts"]), 4)
        self.assertEqual(len(payload["negative_numeric_contexts"]), 6)
        self.assertEqual(payload["autocorrection"], "NOT_IMPLEMENTED")

    def test_outside_story_fixture_keeps_positive_contexts_and_sfx_negatives(self):
        fixture = Path(__file__).parent / "test_fixtures" / "quality_regression_diagnostics" / "outside_balloon_post_ocr_synthetic.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertEqual(payload["evidence_kind"], "POST_OCR_SYNTHETIC_FIXTURE")
        self.assertEqual(len(payload["positive_story_cases"]), 4)
        negatives = set(payload["negative_sfx_controls"])
        self.assertTrue({"THUMP", "BAM", "WHOOSH", "SCRRCH", "CRACK"}.issubset(negatives))

    def test_progress_adapter_does_not_invent_missing_fields(self):
        result = progress_page_snapshot({"pages": [{"index": 6, "debug_data": {
            "items": [{"id": "LINE_001", "raw_text": "m", "clean_text": "m",
                       "classification": "unknown", "classification_reason": "line_ignored_before_grouping",
                       "ignore_reason": "too_few_useful_chars"}]
        }}]}, 6)
        item = result["regions"][0]
        self.assertEqual(item["failure_boundary"], "FILTERED_PRE_GROUP")
        self.assertNotIn("bbox", item)
        self.assertNotIn("translation_eligible", item)

    def test_review_conversion_is_explicit_and_preserves_override_evidence(self):
        out = review_to_expectation({"regions": [{
            "page_index": 6, "region_id": "manual:1", "source_text_original": "1 AM",
            "source_text_override": "I AM", "target_text_override": "ESTOU AQUI",
            "translate_override": "translate", "bbox_override": [1, 2, 3, 4],
            "is_manual_region": True,
        }]}, case_id="review-local")
        self.assertEqual(out["source"], "EXPLICIT_LOCAL_REVIEW_SNAPSHOT")
        self.assertEqual(out["regions"][0]["source_text_override"], "I AM")
        self.assertTrue(out["regions"][0]["is_manual_region"])

    def test_confirmed_real_slurp_fixture_runs_as_sfx_negative(self):
        root = Path(__file__).parent / "test_fixtures" / "quality_regression_diagnostics" / "real" / "sfx_slurp_p006"
        expectation = json.loads((root / "expectation.json").read_text(encoding="utf-8"))
        provenance = json.loads((root / "provenance.json").read_text(encoding="utf-8"))
        artifact = json.loads((root / "diagnostic_input.json").read_text(encoding="utf-8"))
        self.assertEqual(expectation["evidence_level"], "CONFIRMED_REAL")
        self.assertEqual(provenance["raw_ocr"], expectation["expected_text"])
        self.assertEqual(classify_item(artifact["regions"][0]), expectation["expected_boundary"])


if __name__ == "__main__":
    unittest.main()
