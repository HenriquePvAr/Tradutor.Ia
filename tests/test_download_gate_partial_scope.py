"""A bounded/partial run (max_images, or an explicit page selection) must be graded
against the pages it was asked to fetch -- not the whole canonical chapter.

Physically observed (job b0a1f250, a 5-page PSD test): the download materialised 5/5 but
``_build_download_gate`` used ``reader.page_control_count`` (105) as the expected logical
count, so pages 6..105 were flagged ``incomplete_logical_pages`` and the gate raised
``incomplete_download``.  The gate now derives the expected logical set from the selected
candidate ids (or caps by ``requested_max_images``), while a genuinely missing SELECTED
page still fails closed.
"""
import unittest

import down


def _gate(downloaded_indices, selected_ids, *, page_control=105, requested_max=None):
    accepted = [{"id": f"c{i}", "logical_index": i} for i in range(1, 106)]
    downloaded = [
        {"candidate_id": f"c{i}", "logical_index": i, "order": i, "is_chapter_candidate": True,
         "path": f"C:\\tmp\\{i:03d}.png", "canonical": True,
         "canonical_local_path": f"C:\\tmp\\{i:03d}.png"}
        for i in downloaded_indices
    ]
    report = {
        "source_analysis": {"accepted": accepted,
                            "reader_diagnostics": {"page_control_count": page_control}},
        "downloaded": downloaded,
        "expected_chapter_candidate_ids": selected_ids,
        "requested_max_images": requested_max,
    }
    return down._build_download_gate(report)


class PartialScopeDownloadGateTests(unittest.TestCase):
    def test_five_page_partial_passes_when_all_selected_present(self):
        gate = _gate([1, 2, 3, 4, 5], [f"c{i}" for i in (1, 2, 3, 4, 5)], requested_max=5)
        self.assertTrue(gate["passed"], gate["reasons"])
        self.assertEqual(gate["expected_logical_count"], 5)
        self.assertNotIn("incomplete_logical_pages", gate["reasons"])

    def test_scattered_partial_selection_passes(self):
        # A manual, non-contiguous selection is graded against exactly those pages.
        gate = _gate([2, 40, 77], [f"c{i}" for i in (2, 40, 77)], requested_max=None)
        self.assertTrue(gate["passed"], gate["reasons"])
        self.assertEqual(gate["expected_logical_count"], 3)

    def test_full_run_still_requires_every_page(self):
        gate = _gate(list(range(1, 106)), [f"c{i}" for i in range(1, 106)])
        self.assertTrue(gate["passed"], gate["reasons"])

    def test_full_run_with_a_gap_fails_closed(self):
        missing_one = [i for i in range(1, 106) if i != 42]
        gate = _gate(missing_one, [f"c{i}" for i in range(1, 106)])
        self.assertFalse(gate["passed"])
        self.assertIn("incomplete_logical_pages", gate["reasons"])

    def test_partial_missing_a_selected_page_fails_closed(self):
        gate = _gate([1, 2, 4, 5], [f"c{i}" for i in (1, 2, 3, 4, 5)], requested_max=5)
        self.assertFalse(gate["passed"])  # page 3 was selected but not downloaded
        self.assertIn("incomplete_logical_pages", gate["reasons"])

    def test_requested_max_cap_without_explicit_selection(self):
        # No candidate-id selection, but requested_max bounds the expectation.
        gate = _gate([1, 2, 3], [], requested_max=3)
        self.assertNotIn("incomplete_logical_pages", gate["reasons"])


if __name__ == "__main__":
    unittest.main()
