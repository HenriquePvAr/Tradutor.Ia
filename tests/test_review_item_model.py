"""Mission 8: normalized linguistic-QC ReviewItem model + human warning labels.

Hermetic, no UI/network. Covers §36 A-I plus the warning-label humanization (§14/§15)."""
import unittest

import review_item as ri
from review_item import ReviewItem, ReviewStatus, from_quality_review_item as adapt, humanize_warning


class ReviewItemModelTests(unittest.TestCase):
    # A) complete item
    def test_complete_item(self):
        it = adapt({"page": 95, "region_id": "grp_2", "classification": "speech",
                    "source_text_effective": "THANK GOD", "target_text_effective": "AINDA BEM",
                    "confidence": 0.72, "state": "pending", "review_reason_code": "OCR_LOW_CONFIDENCE"})
        self.assertEqual(it.page, 95)
        self.assertEqual(it.region_id, "grp_2")
        self.assertEqual(it.semantic_type, "speech")
        self.assertEqual(it.source_text, "THANK GOD")
        self.assertEqual(it.translated_text, "AINDA BEM")
        self.assertEqual(it.confidence, 0.72)
        self.assertEqual(it.status, ReviewStatus.PENDING)
        self.assertIn("OCR_LOW_CONFIDENCE", it.warnings)
        self.assertIn("OCR com baixa confiança", it.warning_labels)

    # B) missing source
    def test_missing_source(self):
        it = adapt({"page": 1, "region_id": "g", "target_text_effective": "OI", "state": "pending"})
        self.assertEqual(it.source_text, "")
        self.assertFalse(it.source_text)

    # C) missing translation
    def test_missing_translation(self):
        it = adapt({"page": 1, "region_id": "g", "original": "HELLO", "state": "pending"})
        self.assertEqual(it.translated_text, "")
        self.assertFalse(it.has_translation)

    # D) multiline preserved
    def test_multiline(self):
        it = adapt({"page": 1, "region_id": "g", "target_text_effective": "LINHA 1\nLINHA 2"})
        self.assertEqual(it.translated_text, "LINHA 1\nLINHA 2")

    # E) Unicode PT-BR
    def test_unicode_ptbr(self):
        it = adapt({"page": 1, "region_id": "g", "target_text_effective": "VOCÊ ESTÁ AÍ? Ç ã õ"})
        self.assertEqual(it.translated_text, "VOCÊ ESTÁ AÍ? Ç ã õ")

    # F) multiple warnings, de-duplicated, all humanized
    def test_multiple_warnings(self):
        it = adapt({"page": 1, "region_id": "g", "review_reason_code": "ENGLISH_RESIDUAL",
                    "translation_stale": True, "visual_reason_code": "text_overlap_regression_detected",
                    "target_text_effective": "X"})
        self.assertEqual(it.warnings, ["ENGLISH_RESIDUAL", "SEMANTIC_VALIDATION", "VISUAL_REVIEW"])
        self.assertEqual(it.warning_labels,
                         ["Possível texto em inglês", "Tradução requer revisão",
                          "Revisão visual recomendada no PSD"])

    # G) pending -> approved
    def test_status_approved(self):
        self.assertEqual(adapt({"page": 1, "region_id": "g", "state": "reviewed"}).status,
                         ReviewStatus.APPROVED)

    # H) pending -> resolved
    def test_status_resolved(self):
        for s in ("edited", "rejected", "preserved_original"):
            self.assertEqual(adapt({"page": 1, "region_id": "g", "state": s}).status,
                             ReviewStatus.RESOLVED)

    # I) legacy item with missing fields still adapts with safe defaults
    def test_legacy_item(self):
        it = adapt({"region_id": "g"})
        self.assertEqual(it.page, 0)
        self.assertEqual(it.source_text, "")
        self.assertEqual(it.translated_text, "")
        self.assertEqual(it.warnings, [])
        self.assertIsNone(it.confidence)
        self.assertEqual(it.status, ReviewStatus.PENDING)
        self.assertEqual(it.semantic_type, "unknown")

    # warning label humanization: known code, human passthrough, unknown code, empty
    def test_humanize_warning(self):
        self.assertEqual(humanize_warning("SOURCE_FALLBACK"), "Texto original mantido")
        self.assertEqual(humanize_warning("Já revisado pelo tradutor"), "Já revisado pelo tradutor")
        self.assertEqual(humanize_warning("BRAND_NEW_CODE"),
                         "Este trecho precisa de atenção antes de confirmar o resultado.")
        self.assertTrue(humanize_warning(""))  # non-empty generic fallback

    def test_no_fabricated_warnings(self):
        # a clean, approved item with no signals -> no warnings invented
        it = adapt({"page": 3, "region_id": "g", "state": "reviewed",
                    "target_text_effective": "OK", "classification": "narration"})
        self.assertEqual(it.warnings, [])

    # §43 boundary: item-level QC status is separate from the job's terminal status. Mission 8
    # must not conflate them (that integration is Mission 9). Guards accidental overlap.
    def test_review_status_distinct_from_job_terminal_statuses(self):
        job_terminal = {"finished", "review_required", "review_completed", "failed", "cancelled"}
        qc = {ReviewStatus.PENDING, ReviewStatus.APPROVED, ReviewStatus.RESOLVED}
        self.assertTrue(qc.isdisjoint(job_terminal))

    def test_to_dict_roundtrips_labels(self):
        d = adapt({"page": 1, "region_id": "g", "review_reason_code": "TITLE",
                   "target_text_effective": "T"}).to_dict()
        self.assertEqual(d["warnings"], ["TITLE"])
        self.assertEqual(d["warning_labels"], ["Título"])


if __name__ == "__main__":
    unittest.main()
