"""Artifact Contract V2 foundation tests (Mission 3)."""
import unittest
from pathlib import Path

import artifact_contract as ac
from artifact_contract import ArtifactType as AT, OutputProfile as OP, Capability as CAP


class ArtifactContractTests(unittest.TestCase):
    # A) contract version
    def test_contract_version_is_2(self):
        self.assertEqual(ac.ARTIFACT_CONTRACT_VERSION, 2)

    # B) AUTOMATIC declares exactly its current types
    def test_automatic_profile_artifacts(self):
        self.assertEqual(ac.profile_artifacts(OP.AUTOMATIC),
                         (AT.FINAL_PDF, AT.TRANSLATED_IMAGE))

    # C) PROFESSIONAL_QC declares the professional types
    def test_professional_profile_artifacts(self):
        self.assertEqual(
            ac.profile_artifacts(OP.PROFESSIONAL_QC),
            (AT.FINAL_PDF, AT.TRANSLATED_IMAGE, AT.CLEAN_IMAGE, AT.EDITABLE_PSD,
             AT.QUALITY_REPORT, AT.REVIEW_MANIFEST))

    # D) capability distinguishes implemented from planned
    def test_capabilities_reflect_reality(self):
        self.assertEqual(ac.capability_of(AT.FINAL_PDF), CAP.SUPPORTED)
        self.assertEqual(ac.capability_of(AT.TRANSLATED_IMAGE), CAP.SUPPORTED)
        self.assertEqual(ac.capability_of(AT.QUALITY_REPORT), CAP.SUPPORTED)
        self.assertEqual(ac.capability_of(AT.REVIEW_MANIFEST), CAP.PLANNED)

    # E) EDITABLE_PSD is SUPPORTED since Mission 6 and stays distinct from legacy raster PSD
    def test_editable_psd_supported_and_distinct_from_raster(self):
        self.assertEqual(ac.capability_of(AT.EDITABLE_PSD), CAP.SUPPORTED)
        self.assertNotEqual(AT.EDITABLE_PSD, AT.LEGACY_RASTER_PSD)
        self.assertEqual(ac.capability_of(AT.LEGACY_RASTER_PSD), CAP.SUPPORTED)

    # F) legacy output without contract_version stays readable as V1
    def test_legacy_manifest_is_v1(self):
        self.assertEqual(ac.contract_version_of({}), 1)
        self.assertEqual(ac.contract_version_of({"pdf_path": "x"}), 1)
        self.assertTrue(ac.is_legacy_manifest({}))
        self.assertFalse(ac.is_legacy_manifest({"contract_version": 2}))

    # G-K) deterministic paths
    def test_final_pdf_path(self):
        self.assertEqual(ac.artifact_relative_path(AT.FINAL_PDF, chapter="Between Two Floors"),
                         "final/Between_Two_Floors_ptbr.pdf")

    def test_translated_image_path(self):
        self.assertEqual(ac.artifact_relative_path(AT.TRANSLATED_IMAGE, page=1), "images/translated/001.png")

    def test_clean_image_path(self):
        self.assertEqual(ac.artifact_relative_path(AT.CLEAN_IMAGE, page=1), "images/clean/001.png")

    def test_psd_path(self):
        self.assertEqual(ac.artifact_relative_path(AT.EDITABLE_PSD, page=1), "psd/001.psd")

    def test_qc_paths(self):
        self.assertEqual(ac.artifact_relative_path(AT.QUALITY_REPORT), "qc/quality_report.json")
        self.assertEqual(ac.artifact_relative_path(AT.REVIEW_MANIFEST), "qc/review_manifest.json")

    # L/M/N) page naming incl. >999
    def test_page_naming(self):
        self.assertEqual(ac.logical_page_name(1), "001")
        self.assertEqual(ac.logical_page_name(208), "208")
        self.assertEqual(ac.logical_page_name(208, total_pages=208), "208")
        self.assertEqual(ac.logical_page_name(1000), "1000")
        self.assertEqual(ac.logical_page_name(5, total_pages=1200), "0005")  # width follows total
        with self.assertRaises(ValueError):
            ac.logical_page_name(0)

    def test_page_1000_plus_path(self):
        self.assertEqual(ac.artifact_relative_path(AT.TRANSLATED_IMAGE, page=1000, total_pages=1000),
                         "images/translated/1000.png")

    # O) path traversal blocked; T) produced path never escapes root
    def test_path_traversal_blocked(self):
        root = Path("C:/out/run1") if Path("C:/").exists() else Path("/out/run1")
        # malicious chapter cannot escape
        p = ac.artifact_path(root, AT.FINAL_PDF, chapter="../../etc/passwd")
        self.assertTrue(str(p).replace("\\", "/").endswith("/out/run1/final/etc_passwd_ptbr.pdf")
                        or "run1" in str(p))
        self.assertIn("run1", str(p))
        for art, kw in ((AT.TRANSLATED_IMAGE, {"page": 1}), (AT.EDITABLE_PSD, {"page": 3}),
                        (AT.QUALITY_REPORT, {}), (AT.FINAL_PDF, {"chapter": "x/../.."})):
            full = ac.artifact_path(root, art, **kw)
            self.assertTrue(root.resolve() == full or root.resolve() in full.parents,
                            f"{art} escaped root: {full}")

    # P) unicode title safe
    def test_unicode_title_safe(self):
        slug = ac.safe_chapter_slug("사이 두 층 — 第1話")
        self.assertNotIn("/", slug)
        self.assertNotIn("\\", slug)
        self.assertTrue(slug)  # non-empty
        # a Windows reserved name is neutralized
        self.assertTrue(ac.safe_chapter_slug("CON").lower().startswith("_con"))
        # empty falls back
        self.assertEqual(ac.safe_chapter_slug("   "), "chapter")

    # Q) windows path safety (backslash/colon-free relative names)
    def test_windows_reserved_and_length(self):
        long_title = "A" * 400
        self.assertLessEqual(len(ac.safe_chapter_slug(long_title)), 80)

    # R) unrequested != unavailable; S) requested-but-unsupported -> UNAVAILABLE
    def test_manifest_status_semantics(self):
        # AUTOMATIC: nothing produced yet -> pdf/translated REQUESTED (supported), none unavailable
        m = ac.build_artifact_manifest(output_profile=OP.AUTOMATIC, output_root="C:/out/run1",
                                       logical_page_count=208, produced={AT.FINAL_PDF: True})
        self.assertEqual(m["contract_version"], 2)
        self.assertEqual(m["output_profile"], OP.AUTOMATIC)
        self.assertIn(AT.FINAL_PDF, m["produced_artifacts"])
        self.assertEqual(m["unavailable_artifacts"], [])  # translated is supported, just not produced
        self.assertEqual(m["artifact_status"][AT.TRANSLATED_IMAGE], ac.ArtifactStatus.REQUESTED)
        # a diagnostic type is not part of AUTOMATIC at all (unrequested != unavailable)
        self.assertNotIn(AT.OCR_PROVENANCE, m["requested_artifacts"])

    def test_professional_reports_editable_psd_produced_and_review_manifest_unavailable(self):
        # Since Mission 6 editable_psd is SUPPORTED: when produced it is PRODUCED; review_manifest
        # is still PLANNED, so it remains the requested-but-unavailable one.
        m = ac.build_artifact_manifest(output_profile=OP.PROFESSIONAL_QC, output_root="C:/out/run1",
                                       logical_page_count=208,
                                       produced={AT.FINAL_PDF: True, AT.TRANSLATED_IMAGE: True,
                                                 AT.CLEAN_IMAGE: True, AT.EDITABLE_PSD: True,
                                                 AT.QUALITY_REPORT: True})
        self.assertIn(AT.EDITABLE_PSD, m["requested_artifacts"])
        self.assertIn(AT.EDITABLE_PSD, m["produced_artifacts"])
        self.assertEqual(m["artifact_status"][AT.EDITABLE_PSD], ac.ArtifactStatus.PRODUCED)
        self.assertIn(AT.REVIEW_MANIFEST, m["unavailable_artifacts"])
        # editable_psd requested but NOT produced this run -> REQUESTED, never faked
        m2 = ac.build_artifact_manifest(output_profile=OP.PROFESSIONAL_QC, output_root="C:/out/run1",
                                        logical_page_count=208, produced={AT.FINAL_PDF: True})
        self.assertEqual(m2["artifact_status"][AT.EDITABLE_PSD], ac.ArtifactStatus.REQUESTED)
        self.assertNotIn(AT.EDITABLE_PSD, m2["unavailable_artifacts"])

    def test_user_vs_internal_artifacts_partitioned(self):
        self.assertTrue(ac.USER_ARTIFACTS.isdisjoint(ac.INTERNAL_DIAGNOSTIC_ARTIFACTS))
        self.assertIn(AT.FINAL_PDF, ac.USER_ARTIFACTS)
        self.assertIn(AT.RUN_MANIFEST, ac.INTERNAL_DIAGNOSTIC_ARTIFACTS)

    def test_unknown_type_and_missing_page_raise(self):
        with self.assertRaises(ValueError):
            ac.artifact_relative_path("nonsense_type")
        with self.assertRaises(ValueError):
            ac.artifact_relative_path(AT.TRANSLATED_IMAGE)  # missing page

    def test_paths_are_deterministic(self):
        a = ac.artifact_relative_path(AT.CLEAN_IMAGE, page=42, total_pages=208)
        b = ac.artifact_relative_path(AT.CLEAN_IMAGE, page=42, total_pages=208)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
