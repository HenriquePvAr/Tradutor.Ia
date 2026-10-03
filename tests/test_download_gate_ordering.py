"""Hermetic tests for the download gate's ordering contract.

Regression: parallel download completes pages out of order (data-URL canvas crops finish
instantly and cluster ahead of the network-fetched images).  The chapter manifest is now
sorted by logical ``order`` before the gate, so a complete-but-out-of-completion-order
download passes, while a genuinely incomplete download still fails closed.
"""
import unittest

import down


def _report(downloaded):
    ids = ["a", "b", "c"]
    return {
        "source_analysis": {
            "reader_diagnostics": {"page_control_count": 3},
            "accepted": [{"id": i, "logical_index": n} for n, i in enumerate(ids, 1)],
        },
        "expected_chapter_candidate_ids": ids,
        "downloaded": downloaded,
    }


def _item(cid, logical):
    return {"candidate_id": cid, "logical_index": logical, "order": logical - 1,
            "is_chapter_candidate": True, "canonical": True}


COMPLETE_IN_ORDER = [_item("a", 1), _item("b", 2), _item("c", 3)]
# Parallel completion order: canvas-like page finishes first, images after (out of order).
COMPLETE_OUT_OF_ORDER = [_item("c", 3), _item("a", 1), _item("b", 2)]


class DownloadGateOrderingTest(unittest.TestCase):
    def test_complete_in_order_passes(self):
        gate = down._build_download_gate(_report(list(COMPLETE_IN_ORDER)))
        self.assertTrue(gate["passed"], gate.get("reasons"))
        self.assertEqual(gate["actual_logical_count"], 3)

    def test_out_of_completion_order_before_sort_trips_monotonic(self):
        # Documents exactly why the pipeline sorts before the gate.
        gate = down._build_download_gate(_report(list(COMPLETE_OUT_OF_ORDER)))
        self.assertFalse(gate["passed"])
        self.assertIn("chapter_order_not_monotonic", gate["reasons"])
        self.assertEqual(gate["missing_logical_indices"], [])  # complete, only order tripped

    def test_sorting_by_order_makes_complete_download_pass(self):
        # This mirrors the production fix in _download_candidates.
        downloaded = sorted(list(COMPLETE_OUT_OF_ORDER), key=lambda it: int(it.get("order") or 0))
        gate = down._build_download_gate(_report(downloaded))
        self.assertTrue(gate["passed"], gate.get("reasons"))
        self.assertTrue(gate["order_monotonic"])

    def test_incomplete_download_fails_closed(self):
        # Drop logical page 3 entirely — must never pass, regardless of order.
        gate = down._build_download_gate(_report([_item("a", 1), _item("b", 2)]))
        self.assertFalse(gate["passed"])
        self.assertIn("incomplete_logical_pages", gate["reasons"])
        self.assertEqual(gate["missing_logical_indices"], [3])


if __name__ == "__main__":
    unittest.main()
