import unittest

from ocr_balloon import TextGroup
from translation_region_audit import (
    canonical_region_snapshot,
    classify_region_issue,
    classify_region_terminal,
    summarize_translation_region_states,
)


def _group(name, text="HELLO", *, sent=True, translation="OLA", state="translated", retry=0, overflow=0.0, ignored=False):
    return TextGroup(
        group_id=name,
        region_id=name,
        text=text,
        sent_to_translation=sent,
        translation=translation,
        translation_final_state=state,
        translation_retry_count=retry,
        text_overflow_ratio=overflow,
        ignored=ignored,
    )


class TranslationRegionAuditTests(unittest.TestCase):
    def test_happy_path_has_no_silent_missing(self):
        summary = summarize_translation_region_states([_group("a"), _group("b"), _group("c")])
        self.assertEqual(summary["TOTAL_REGIONS"], 3)
        self.assertEqual(summary["TRANSLATABLE_REGIONS"], 3)
        self.assertEqual(summary["TRANSLATED_TEXT_ASSIGNED_REGIONS"], 3)
        self.assertEqual(summary["FINAL_RENDER_TRANSLATED_REGIONS"], 3)
        self.assertEqual(summary["SILENT_MISSING_REGIONS"], 0)
        self.assertEqual(summary["TERMINAL_CLASSIFICATION_TOTAL"], 3)

    def test_legitimate_non_translatable_and_empty_ocr_are_explicit(self):
        summary = summarize_translation_region_states([
            _group("empty", text="", sent=False, translation="", state=""),
            _group("filtered", sent=False, translation="", state="", ignored=True),
        ])
        self.assertEqual(summary["OCR_EMPTY_REGIONS"], 1)
        self.assertEqual(summary["TRANSLATABLE_REGIONS"], 0)
        self.assertEqual(summary["SILENT_MISSING_REGIONS"], 0)
        self.assertEqual(summary["TERMINAL_NON_TRANSLATABLE"], 2)

    def test_negative_detector_catches_request_not_sent(self):
        group = _group("missing", sent=False, translation="", state="")
        self.assertEqual(classify_region_terminal(group), "non_translatable")
        # A valid OCR/translatable fixture with the request accidentally lost
               # is represented explicitly for the detector.
        group.sent_to_translation = True
        self.assertEqual(classify_region_terminal(group), "silent_missing")
        self.assertEqual(summarize_translation_region_states([group])["SILENT_MISSING_REGIONS"], 1)

    def test_empty_response_and_error_are_not_silent(self):
        empty = _group("empty", translation="", state="translation_failed")
        error = _group("error", translation="", state="translation_failed")
        summary = summarize_translation_region_states([empty, error])
        self.assertEqual(summary["TRANSLATION_ERROR_REGIONS"], 2)
        self.assertEqual(summary["EXPLICIT_FAILURE_REGIONS"], 2)
        self.assertEqual(summary["SILENT_MISSING_REGIONS"], 0)

    def test_taxonomy_codes_are_testable(self):
        self.assertEqual(classify_region_issue(_group("ocr", text="", sent=False, translation="", state="")), "UT1_OCR_NO_TEXT")
        self.assertEqual(classify_region_issue(_group("filtered", ignored=True, sent=False, translation="", state="")), "UT2_OCR_TEXT_FILTERED")
        self.assertEqual(classify_region_issue(_group("not-requested", sent=False, translation="", state="")), "UT4_TRANSLATION_NOT_REQUESTED")
        self.assertEqual(classify_region_issue(_group("empty", translation="", state="rejected")), "UT5_TRANSLATION_EMPTY_RESPONSE")
        self.assertEqual(classify_region_issue(_group("error", translation="", state="translation_failed")), "UT6_TRANSLATION_ERROR")
        self.assertEqual(classify_region_issue(_group("lost", translation="", state="")), "UT7_TRANSLATED_TEXT_NOT_ASSIGNED")
        self.assertEqual(classify_region_issue(_group("review", translation="ok", state="review_required")), "UT12_EXPLICIT_REVIEW")

    def test_overflow_retry_is_counted_once(self):
        summary = summarize_translation_region_states([
            _group("normal"),
            _group("overflow", retry=1, overflow=1.0),
        ])
        self.assertEqual(summary["OVERFLOW_REGIONS"], 1)
        self.assertEqual(summary["OVERFLOW_RETRIED_REGIONS"], 1)
        self.assertEqual(summary["SILENT_MISSING_REGIONS"], 0)

    def test_snapshot_is_semantic_and_order_independent(self):
        a = _group("a"); a.page_index = 1
        b = _group("b"); b.page_index = 2
        self.assertEqual(canonical_region_snapshot([a, b]), canonical_region_snapshot([b, a]))


if __name__ == "__main__":
    unittest.main()
