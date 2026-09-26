"""Hermetic tests for the missing-loop budget model and fail-closed classification.

The historical bug was a single ~30s deadline shared by ALL unresolved pages, so a slow
early page starved the later ones (Page 90 never reached) and the overall failure was
mislabeled ``no_reader_images``.  These tests pin the new contract without a browser.
"""
import unittest

import scrapling_reader_resolver as r

BASE = r.MISSING_CHAPTER_BASE_OVERHEAD_SECONDS
PER = r.MISSING_PAGE_BUDGET_SECONDS
CAP = r.MISSING_CHAPTER_MAX_BUDGET_SECONDS


class MissingBudgetModelTest(unittest.TestCase):
    # A) N unresolved pages do NOT share one fixed deadline: the budget scales with N.
    def test_budget_scales_with_unresolved_count_not_fixed_30s(self):
        self.assertEqual(r._missing_chapter_budget_seconds(2), BASE + 2 * PER)
        # Scales linearly with N, bounded by the conscious hard safety ceiling (CAP): a
        # 10-canvas chapter would be BASE + 10*PER but is clamped to CAP, and the whole
        # resolution is bounded by the global deadline anyway.
        self.assertEqual(r._missing_chapter_budget_seconds(10), min(CAP, BASE + 10 * PER))
        # No longer capped at the old shared 30s for a real 10-canvas chapter.
        self.assertGreater(r._missing_chapter_budget_seconds(10), 30.0)

    # B) Each page contributes its own full budget increment (not a shared pool).
    def test_each_page_adds_its_own_budget(self):
        self.assertEqual(
            r._missing_chapter_budget_seconds(3) - r._missing_chapter_budget_seconds(2), PER)
        self.assertEqual(
            r._missing_chapter_budget_seconds(2) - r._missing_chapter_budget_seconds(1), PER)

    # E) Zero unresolved -> only base overhead, no per-page waiting stacked on top.
    def test_zero_unresolved_is_base_overhead_only(self):
        self.assertEqual(r._missing_chapter_budget_seconds(0), BASE)
        self.assertEqual(r._missing_chapter_budget_seconds(-5), BASE)  # defensive

    def test_hard_safety_ceiling(self):
        self.assertEqual(r._missing_chapter_budget_seconds(10_000), CAP)


class ResolutionFailureCodeTest(unittest.TestCase):
    _OK = [{"logical_page_index": 1, "source": "browser_response"}]
    _DOM = [{"logical_page_index": 1, "source": "scrapling_dom"}]

    # C) A materialization timeout must NOT be reported as no_reader_images.
    def test_timeout_is_not_no_reader_images(self):
        code = r._resolution_failure_code(self._OK, final_missing=[10, 20])
        self.assertEqual(code, "canonical_materialization_timeout")
        self.assertNotEqual(code, "no_reader_images")

    # D) expected > canonical (missing pages present) stays fail-closed.
    def test_expected_gt_canonical_fails_closed(self):
        self.assertIsNotNone(r._resolution_failure_code(self._OK, final_missing=[90]))

    def test_empty_discovery_is_no_reader_images(self):
        self.assertEqual(r._resolution_failure_code([], final_missing=[]), "no_reader_images")

    def test_unmaterialized_dom_is_materialization_failed(self):
        self.assertEqual(
            r._resolution_failure_code(self._DOM, final_missing=[]),
            "canonical_materialization_failed")

    def test_complete_chapter_has_no_failure(self):
        self.assertIsNone(r._resolution_failure_code(self._OK, final_missing=[]))


if __name__ == "__main__":
    unittest.main()
