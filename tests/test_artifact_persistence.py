"""Mission 4: CLEAN / TRANSLATED persisted as canonical per-page artifacts.

Hermetic -- exercises the persistence helpers directly with synthetic PNGs. No pipeline,
no network, no OCR/translation, no DeepL, no Supabase. Proves the 203/208 clean gap is
closed (no-text pages now carry a clean artifact), the AUTOMATIC/PROFESSIONAL_QC profile
gate, atomic + deterministic writes, coverage accounting and byte totals.
"""
import os
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

import yomu.runtime.artifact_contract as ac
import yomu.pipeline.benchmark_pipeline as bp


def _png(path, w=800, h=200, value=127):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img = np.full((h, w, 3), value, dtype=np.uint8)
    assert cv2.imwrite(str(path), img)
    return str(path)


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class ArtifactPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.pages = self.root / "pages"
        self.src = self.root / "input"

    def tearDown(self):
        self._tmp.cleanup()

    def _state(self, idx, *, cleaned=True, source_value=127, out_value=90,
               short=False, bad_source=False, cache_source=None):
        """Build one completed page state with on-disk translated + optional cleaned + source.

        cache_source models the pipeline reality: a page that rendered is "fresh" (and normally
        has a cleaned buffer); a page without text is copied via "no_text_precheck" (no cleanup).
        Default: cleaned pages are "fresh", uncleaned pages are "no_text_precheck".
        """
        h = 32 if short else 200
        out = _png(self.pages / f"page_{idx:03}.png", h=h, value=out_value)
        image_path = self.src / f"src_{idx:03}.png"
        if bad_source:
            image_path = self.src / f"missing_{idx:03}.png"  # never created
        else:
            _png(image_path, h=h, value=source_value)
        state = {"index": idx, "output_path": out, "image_path": str(image_path)}
        state["cache_source"] = cache_source or ("fresh" if cleaned else "no_text_precheck")
        if cleaned:
            state["cleaned_path"] = _png(
                self.pages / f"page_{idx:03}_cleaned.png", h=h, value=200)
        return state

    # --- A) TRANSLATED persisted for every page under AUTOMATIC (default) -----------------
    def test_automatic_persists_translated_only(self):
        states = [self._state(i) for i in (1, 2, 3)]
        rep = bp._persist_canonical_artifacts(self.root, states, 3, ac.OutputProfile.AUTOMATIC)
        self.assertEqual(rep["translated_produced"], [1, 2, 3])
        self.assertFalse(rep["clean_requested"])
        self.assertEqual(rep["clean_produced"], [])
        for i in (1, 2, 3):
            self.assertTrue((self.root / "images" / "translated" / f"{i:03}.png").is_file())
        self.assertFalse((self.root / "images" / "clean").exists())

    # --- B) TRANSLATED canonical is the SAME buffer used for the PDF (byte-identical) -----
    def test_translated_is_same_buffer_as_output_path(self):
        st = self._state(5)
        bp._persist_canonical_artifacts(self.root, [st], 5, ac.OutputProfile.AUTOMATIC)
        canonical = self.root / "images" / "translated" / "005.png"
        self.assertEqual(Path(st["output_path"]).read_bytes(), canonical.read_bytes())

    # --- C) PROFESSIONAL_QC persists clean for a page WITH cleanup (case A) ---------------
    def test_professional_persists_clean_from_cleaned_buffer(self):
        st = self._state(1, cleaned=True)
        rep = bp._persist_canonical_artifacts(self.root, [st], 1, ac.OutputProfile.PROFESSIONAL_QC)
        self.assertEqual(rep["clean_produced"], [1])
        clean = self.root / "images" / "clean" / "001.png"
        self.assertEqual(Path(st["cleaned_path"]).read_bytes(), clean.read_bytes())

    # --- D) 203/208 GAP CLOSED: a no-text page (no cleaned buffer) still gets a clean ----
    def test_no_text_page_falls_back_to_source_for_clean(self):
        st = self._state(2, cleaned=False)  # cleanup was a no-op -> no *_cleaned.png
        rep = bp._persist_canonical_artifacts(self.root, [st], 2, ac.OutputProfile.PROFESSIONAL_QC)
        self.assertEqual(rep["clean_produced"], [2])
        clean = self.root / "images" / "clean" / "002.png"
        # for a no-text page the accepted source IS the clean art (case B)
        self.assertEqual(Path(st["image_path"]).read_bytes(), clean.read_bytes())

    def test_full_clean_coverage_with_mixed_text_and_no_text(self):
        # mirrors the old failure: some pages had text (cleaned), some did not (no cleaned)
        states = [self._state(i, cleaned=(i % 2 == 0)) for i in range(1, 9)]
        rep = bp._persist_canonical_artifacts(self.root, states, 8, ac.OutputProfile.PROFESSIONAL_QC)
        self.assertEqual(len(rep["clean_produced"]), 8)  # 8/8, not 4/8
        self.assertEqual(rep["clean_missing"], [])
        self.assertEqual(rep["clean_failed"], [])

    # --- E) blank / short pages are valid, not missing/failed (§11, no floor) -------------
    def test_blank_and_short_pages_are_produced(self):
        blank = self._state(1, cleaned=False, source_value=255, out_value=255)  # blank gutter
        short = self._state(2, cleaned=False, short=True)                        # 800x32 strip
        rep = bp._persist_canonical_artifacts(self.root, [blank, short], 2,
                                              ac.OutputProfile.PROFESSIONAL_QC)
        self.assertEqual(rep["translated_produced"], [1, 2])
        self.assertEqual(rep["clean_produced"], [1, 2])
        self.assertEqual(rep["clean_failed"], [])

    # --- F) a real failure is reported FAILED, never masked (§3 case E, §21) --------------
    def test_unreadable_source_is_failed_not_faked(self):
        good = self._state(1, cleaned=True)
        broken = self._state(2, cleaned=False, bad_source=True)  # no cleaned + missing source
        rep = bp._persist_canonical_artifacts(self.root, [good, broken], 2,
                                              ac.OutputProfile.PROFESSIONAL_QC)
        self.assertEqual(rep["clean_produced"], [1])
        self.assertEqual(rep["clean_failed"], [2])
        self.assertFalse((self.root / "images" / "clean" / "002.png").exists())

    # --- F2) §8: a RENDERED page whose cleanup FAILED must be FAILED, never source-masked --
    def test_rendered_page_failed_cleanup_is_not_source_masked(self):
        # "fresh" page (went through render) but its cleaned buffer is missing -> real failure.
        broken = self._state(1, cleaned=False, cache_source="fresh")
        rep = bp._persist_canonical_artifacts(self.root, [broken], 1,
                                              ac.OutputProfile.PROFESSIONAL_QC)
        self.assertEqual(rep["clean_produced"], [])
        self.assertEqual(rep["clean_failed"], [1])
        self.assertEqual(rep["provenance"][1]["clean_from"], "failed")
        # crucially: the source was NOT silently written as the clean artifact
        self.assertFalse((self.root / "images" / "clean" / "001.png").exists())
        # a non-rendered no-text page, by contrast, legitimately falls back to source
        ok = self._state(2, cleaned=False, cache_source="no_text_precheck")
        rep2 = bp._persist_canonical_artifacts(self.root, [ok], 2,
                                               ac.OutputProfile.PROFESSIONAL_QC)
        self.assertEqual(rep2["clean_produced"], [2])
        self.assertEqual(rep2["provenance"][2]["clean_from"], "source_fallback")

    # --- G) coverage: missing page (never completed) is reported missing ------------------
    def test_missing_logical_page_reported(self):
        states = [self._state(1), self._state(3)]  # page 2 never reached completion
        rep = bp._persist_canonical_artifacts(self.root, states, 3, ac.OutputProfile.AUTOMATIC)
        self.assertEqual(rep["translated_produced"], [1, 3])
        self.assertEqual(rep["translated_missing"], [2])

    # --- H) deterministic paths: a re-run overwrites in place, no *_v2 junk (§19) ---------
    def test_reexport_is_deterministic_and_overwrites(self):
        st = self._state(1)
        bp._persist_canonical_artifacts(self.root, [st], 1, ac.OutputProfile.AUTOMATIC)
        # change the rendered buffer, re-export -> same canonical path, updated content
        _png(st["output_path"], value=10)
        bp._persist_canonical_artifacts(self.root, [st], 1, ac.OutputProfile.AUTOMATIC)
        files = list((self.root / "images" / "translated").iterdir())
        self.assertEqual([f.name for f in files], ["001.png"])
        self.assertEqual(Path(st["output_path"]).read_bytes(),
                         (self.root / "images" / "translated" / "001.png").read_bytes())

    # --- I) atomic: no leftover temp files (§20) ------------------------------------------
    def test_no_temp_files_left_behind(self):
        states = [self._state(i) for i in (1, 2)]
        bp._persist_canonical_artifacts(self.root, states, 2, ac.OutputProfile.PROFESSIONAL_QC)
        leftovers = [p.name for p in (self.root / "images").rglob("*") if ".tmp" in p.name]
        self.assertEqual(leftovers, [])

    # --- J) byte totals recorded and match on-disk sizes (§28) ---------------------------
    def test_byte_totals(self):
        states = [self._state(i) for i in (1, 2)]
        rep = bp._persist_canonical_artifacts(self.root, states, 2, ac.OutputProfile.PROFESSIONAL_QC)
        t = sum((self.root / "images" / "translated" / f"{i:03}.png").stat().st_size for i in (1, 2))
        c = sum((self.root / "images" / "clean" / f"{i:03}.png").stat().st_size for i in (1, 2))
        self.assertEqual(rep["translated_total_bytes"], t)
        self.assertEqual(rep["clean_total_bytes"], c)
        self.assertGreater(rep["translated_total_bytes"], 0)

    # --- K) logical identity comes from the index, not filesystem enumeration (§12) ------
    def test_identity_is_logical_index_not_listing(self):
        # index 208 in a 208-page chapter -> "208.png", regardless of how many were written
        st = self._state(208)
        rep = bp._persist_canonical_artifacts(self.root, [st], 208, ac.OutputProfile.AUTOMATIC)
        self.assertEqual(rep["translated_produced"], [208])
        self.assertTrue((self.root / "images" / "translated" / "208.png").is_file())

    # --- K2) provenance chain distinguishes rendered-clean from source-fallback (§22) -----
    def test_provenance_chain(self):
        text_page = self._state(1, cleaned=True)       # rendered clean
        no_text = self._state(2, cleaned=False)        # source fallback
        rep = bp._persist_canonical_artifacts(self.root, [text_page, no_text], 2,
                                              ac.OutputProfile.PROFESSIONAL_QC)
        self.assertEqual(rep["provenance"][1]["clean_from"], "cleaned_buffer")
        self.assertEqual(rep["provenance"][2]["clean_from"], "source_fallback")
        self.assertEqual(rep["provenance"][1]["source"], text_page["image_path"])
        self.assertTrue(rep["provenance"][1]["translated"])

    def test_clean_verification_never_promotes_source_fallback(self):
        locally_verified = self._state(1, cleaned=True)
        locally_verified["clean_verified"] = True
        locally_verified["debug_data"] = {"items": [{
            "id": "BALAO_1", "region_id": "REGION_001", "clean_disposition": "LOCAL_SAFE",
            "clean_disposition_reason": "owned_source_text_removed_and_validated",
            "clean_local_attempted": True, "clean_artifact_validation": {"passed": True},
            "manual_review_required": False,
        }]}
        preserved_source = self._state(2, cleaned=False)
        rep = bp._persist_canonical_artifacts(
            self.root, [locally_verified, preserved_source], 2, ac.OutputProfile.PROFESSIONAL_QC)
        self.assertTrue(rep["provenance"][1]["clean_verified"])
        self.assertFalse(rep["provenance"][2]["clean_verified"])
        self.assertEqual(rep["provenance"][2]["clean_source"], "source_fallback")
        self.assertEqual(rep["provenance"][2]["residual_validation"]["status"], "NOT_RUN")

    # --- L) profile resolver: default AUTOMATIC, env + args override, junk falls back -----
    def test_profile_resolution(self):
        self.assertEqual(bp._resolve_output_profile(None), ac.OutputProfile.AUTOMATIC)
        self.assertEqual(
            bp._resolve_output_profile(_Args(output_profile="professional_qc")),
            ac.OutputProfile.PROFESSIONAL_QC)
        self.assertEqual(bp._resolve_output_profile(_Args(output_profile="nonsense")),
                         ac.OutputProfile.AUTOMATIC)
        old = os.environ.get("YOMU_OUTPUT_PROFILE")
        try:
            os.environ["YOMU_OUTPUT_PROFILE"] = "professional_qc"
            self.assertEqual(bp._resolve_output_profile(None), ac.OutputProfile.PROFESSIONAL_QC)
        finally:
            if old is None:
                os.environ.pop("YOMU_OUTPUT_PROFILE", None)
            else:
                os.environ["YOMU_OUTPUT_PROFILE"] = old


if __name__ == "__main__":
    unittest.main()
