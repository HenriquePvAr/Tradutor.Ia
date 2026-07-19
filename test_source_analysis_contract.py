"""source_analysis reports analysis coverage; only the downloader reports download coverage.

The bug: a live Webtoons submit accepted 167 candidates and then failed with
reason_code=incomplete_download at stage=source_analysis — blaming a download the runner had
not started. The collector's coverage warning is a real signal, but it is about what the
collector could see, not about bytes that were never fetched.

Hermetic: synthetic analyses only. No network, no browser, no provider, no chapter content.
"""

import _test_bootstrap  # noqa: F401

import unittest
from pathlib import Path

from chapter_source import INCOMPLETE_DOWNLOAD, INCOMPLETE_SOURCE_COVERAGE
from ui_bridge import UiBridge

REPO = Path(__file__).resolve().parent
ANALYSIS_COVERAGE_WARNINGS = ("page_limit_exceeded", "scroll_incomplete", "pagination_incomplete")


def analysis(*, warnings=(), accepted=167):
    # A bare string stays a string: the predicate tolerates that shape deliberately, and
    # list("scroll_incomplete") would silently become a list of characters.
    payload = warnings if isinstance(warnings, (str, bytes)) else list(warnings)
    return {"adapter": "webtoons", "accepted": [{"index": i} for i in range(accepted)],
            "warnings": payload}


class AnalysisCompletenessTests(unittest.TestCase):
    """What counts as an incomplete *analysis* — never a download verdict."""

    def test_many_accepted_candidates_and_no_downloads_is_complete(self):
        # The exact live shape: 167 accepted, runner not started, nothing downloaded.
        self.assertFalse(UiBridge._source_analysis_is_incomplete(analysis()))

    def test_each_collector_coverage_warning_marks_it_incomplete(self):
        for warning in ANALYSIS_COVERAGE_WARNINGS:
            self.assertTrue(
                UiBridge._source_analysis_is_incomplete(analysis(warnings=(warning,))), warning)

    def test_unrelated_warnings_do_not_mark_it_incomplete(self):
        self.assertFalse(
            UiBridge._source_analysis_is_incomplete(analysis(warnings=("slow_reader",))))

    def test_a_lone_warning_string_is_tolerated(self):
        self.assertTrue(
            UiBridge._source_analysis_is_incomplete(analysis(warnings="scroll_incomplete")))

    def test_a_malformed_analysis_is_not_treated_as_incomplete_coverage(self):
        for payload in (None, "", [], 0):
            self.assertFalse(UiBridge._source_analysis_is_incomplete(payload), repr(payload))

    def test_download_counters_are_absent_from_the_predicate(self):
        # Analysis must not consult fields that only exist after a download attempt.
        source = (REPO / "ui_bridge.py").read_text(encoding="utf-8")
        body = source[source.index("def _source_analysis_is_incomplete"):]
        body = body[:body.index("\n    async def ")]
        for field in ("downloaded_count", "validated_count", "output_manifest",
                      "sha256", "pdf_path"):
            self.assertNotIn(field, body, field)


class ReasonCodeOwnershipTests(unittest.TestCase):
    """Which layer may emit which code."""

    def test_the_two_codes_are_distinct(self):
        self.assertNotEqual(INCOMPLETE_SOURCE_COVERAGE, INCOMPLETE_DOWNLOAD)

    def test_ui_bridge_never_emits_a_download_verdict(self):
        # The invariant the bug violated: stage=source_analysis + incomplete_download.
        source = (REPO / "ui_bridge.py").read_text(encoding="utf-8")
        emitting = [line for line in source.splitlines()
                    if "incomplete_download" in line and not line.strip().startswith("#")]
        self.assertEqual(emitting, [], emitting)

    def test_analysis_failure_uses_the_coverage_code(self):
        source = (REPO / "ui_bridge.py").read_text(encoding="utf-8")
        block = source[source.index("if self._source_analysis_is_incomplete("):]
        block = block[:block.index("selected_ids =")]
        self.assertIn("INCOMPLETE_SOURCE_COVERAGE", block)
        self.assertIn('stage="source_analysis"', block)

    def test_the_downloader_still_owns_incomplete_download(self):
        source = (REPO / "down.py").read_text(encoding="utf-8")
        self.assertIn("INCOMPLETE_DOWNLOAD", source)

    def test_the_ui_explains_both_codes_differently(self):
        source = (REPO / "app_ui.py").read_text(encoding="utf-8")
        self.assertIn("incomplete_source_coverage", source)
        self.assertIn("incomplete_download", source)


if __name__ == "__main__":
    unittest.main()
