"""Coverage strategy per adapter: reader stability vs. document end.

Reproduces the measured live shape without any real content: a reader that arrives fully
loaded, shows no growth across stable rounds, and ends well before the document bottom
because recommendations and a footer follow it. The document-end rule called that truncated.

Hermetic: plain dictionaries, no browser, no network.
"""

import _test_bootstrap  # noqa: F401

import unittest

from chapter_source import UniversalChapterAdapter, select_adapter
from down import _scroll_coverage_warnings

WEBTOONS = select_adapter(
    "https://www.webtoons.com/en/drama/serie/episode-1/viewer?title_no=1&episode_no=1")
UNIVERSAL = UniversalChapterAdapter("https://exemplo.test/serie/x/cap-1")


def diagnostics(*, stabilized, reached_document_end):
    return {"stabilized": stabilized, "reached_document_end": reached_document_end}


# The measured live shape: reader bottom 212530, document bottom 218644, five stable rounds.
LIVE_SHAPE = diagnostics(stabilized=True, reached_document_end=False)


class WebtoonsCoverageTests(unittest.TestCase):
    """Case A — complete from the first observation."""

    def test_fully_loaded_reader_without_growth_is_complete(self):
        self.assertEqual(_scroll_coverage_warnings(LIVE_SHAPE, WEBTOONS), ())

    def test_reader_that_also_reached_the_document_end_is_complete(self):
        self.assertEqual(
            _scroll_coverage_warnings(
                diagnostics(stabilized=True, reached_document_end=True), WEBTOONS), ())

    def test_run_that_never_stabilised_still_warns(self):
        """Case B — stopped early: exhausted rounds or time while still changing."""
        self.assertEqual(
            _scroll_coverage_warnings(
                diagnostics(stabilized=False, reached_document_end=False), WEBTOONS),
            ("scroll_incomplete",))

    def test_document_end_alone_is_not_enough_without_stability(self):
        self.assertEqual(
            _scroll_coverage_warnings(
                diagnostics(stabilized=False, reached_document_end=True), WEBTOONS),
            ("scroll_incomplete",))

    def test_missing_or_malformed_diagnostics_fail_closed(self):
        for payload in (None, {}, "", []):
            self.assertEqual(
                _scroll_coverage_warnings(payload, WEBTOONS), ("scroll_incomplete",), repr(payload))


class UniversalCoverageTests(unittest.TestCase):
    """The permissive rule must not leak to an unknown page."""

    def test_universal_still_requires_the_document_end(self):
        # Exactly the shape Webtoons is now allowed to pass on.
        self.assertEqual(
            _scroll_coverage_warnings(LIVE_SHAPE, UNIVERSAL), ("scroll_incomplete",))

    def test_universal_accepts_only_stable_and_document_end(self):
        self.assertEqual(
            _scroll_coverage_warnings(
                diagnostics(stabilized=True, reached_document_end=True), UNIVERSAL), ())

    def test_no_adapter_keeps_the_conservative_default(self):
        self.assertEqual(_scroll_coverage_warnings(LIVE_SHAPE), ("scroll_incomplete",))
        self.assertEqual(_scroll_coverage_warnings(LIVE_SHAPE, None), ("scroll_incomplete",))

    def test_an_unknown_strategy_falls_back_to_conservative(self):
        class Odd:
            coverage_strategy = "something_new"

        self.assertEqual(_scroll_coverage_warnings(LIVE_SHAPE, Odd()), ("scroll_incomplete",))


class StrategyOwnershipTests(unittest.TestCase):
    def test_each_adapter_declares_its_own_strategy(self):
        self.assertEqual(WEBTOONS.coverage_strategy, "reader_container")
        self.assertEqual(UNIVERSAL.coverage_strategy, "generic_document")

    def test_the_downloader_decides_by_strategy_not_by_host(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parent / "down.py").read_text(encoding="utf-8")
        body = source[source.index("def _scroll_coverage_warnings"):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn("coverage_strategy", body)
        for hardcoded in ("webtoons.com", "webtoon.com", "pstatic"):
            self.assertNotIn(hardcoded, body, hardcoded)


if __name__ == "__main__":
    unittest.main()
