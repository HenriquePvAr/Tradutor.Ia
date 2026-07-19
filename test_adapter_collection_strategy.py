"""A registered reader collects from its own container; the generic sweep is for unknowns.

Reproduces the measured discrepancy: a live run reported 823 candidates and 90 accepted with
cross_origin_iframe / network_log_limit / network_json_limit, on a page whose reader holds
167 images and zero iframes. The adapter was selected correctly, but BaseAdapter.analyze ran
the universal collector for every adapter, so recommendations, footer and cross-origin
network noise entered the candidate set.

Hermetic: a fake driver answering the reader script. No browser, no network.
"""

import _test_bootstrap  # noqa: F401

import unittest

from chapter_source import (
    NO_CHAPTER_IMAGES, SourceError, UniversalChapterAdapter, VortexScansAdapter,
    select_adapter,
)

WEBTOONS = select_adapter(
    "https://www.webtoons.com/en/drama/serie/episode-1/viewer?title_no=1&episode_no=1")
PAGE = "https://www.webtoons.com/en/drama/serie/episode-1/viewer?title_no=1&episode_no=1"


class FakeDriver:
    """Answers the reader query; records any other script as a generic sweep."""

    def __init__(self, *, reader_images=167, found=True):
        self.reader_images = reader_images
        self.found = found
        self.scripts = 0

    def execute_script(self, script, *args):
        self.scripts += 1
        if "CONTAINER" not in script:
            raise AssertionError("generic collector ran for a specific adapter")
        if not self.found:
            return {"found": False, "candidates": []}
        return {"found": True, "candidates": [
            {"tag": "img", "url": f"https://webtoon-phinf.pstatic.net/p{i:03}.jpg", "source": "currentSrc",
             "order": i, "width": 800, "height": 1280, "naturalWidth": 800,
             "naturalHeight": 1280, "inContainer": True, "isChapterCandidate": True,
             "y": i * 1280, "className": "_images", "id": "", "alt": ""}
            for i in range(self.reader_images)]}


class StrategyDeclarationTests(unittest.TestCase):
    def test_each_adapter_declares_its_collection_strategy(self):
        self.assertEqual(WEBTOONS.collection_strategy, "adapter_specific")
        self.assertEqual(VortexScansAdapter().collection_strategy, "adapter_specific")
        self.assertEqual(
            UniversalChapterAdapter("https://ex.test/x").collection_strategy,
            "generic_multisource")

    def test_local_folder_never_uses_a_browser_collector(self):
        from local_folder_source import LocalFolderChapterAdapter

        adapter = LocalFolderChapterAdapter()
        self.assertEqual(adapter.source_type, "local_folder")
        self.assertFalse(hasattr(adapter, "collect_reader_payload"))


class ReaderCollectionTests(unittest.TestCase):
    def test_only_reader_images_are_collected(self):
        driver = FakeDriver(reader_images=167)
        payload = WEBTOONS.collect_reader_payload(driver, PAGE)
        self.assertEqual(len(payload["dom_candidates"]), 167)

    def test_network_and_json_evidence_is_absent(self):
        payload = WEBTOONS.collect_reader_payload(FakeDriver(), PAGE)
        self.assertEqual(payload["network_candidates"], [])
        self.assertEqual(payload["json_candidates"], [])

    def test_no_generic_limit_warnings_are_produced(self):
        payload = WEBTOONS.collect_reader_payload(FakeDriver(), PAGE)
        for noise in ("cross_origin_iframe", "network_log_limit", "network_json_limit"):
            self.assertNotIn(noise, payload["warnings"], noise)

    def test_the_collector_is_named_after_the_adapter(self):
        payload = WEBTOONS.collect_reader_payload(FakeDriver(), PAGE)
        self.assertEqual(payload["collector"], "webtoons_reader")

    def test_dom_order_is_preserved(self):
        payload = WEBTOONS.collect_reader_payload(FakeDriver(reader_images=5), PAGE)
        # dom_candidates now carries resolved slots only; order still comes from the reader.
        self.assertEqual([c["order"] for c in payload["dom_candidates"]], [0, 1, 2, 3, 4])
        self.assertEqual(payload["slot_counts"]["pending"], 0)

    def test_a_missing_reader_container_fails_closed(self):
        with self.assertRaises(SourceError) as ctx:
            WEBTOONS.collect_reader_payload(FakeDriver(found=False), PAGE)
        self.assertEqual(ctx.exception.code, NO_CHAPTER_IMAGES)

    def test_a_broken_browser_is_reported_sanitized(self):
        class Broken:
            def execute_script(self, *_a):
                raise RuntimeError("cookie=secret; Authorization: Bearer xyz")

        with self.assertRaises(SourceError) as ctx:
            WEBTOONS.collect_reader_payload(Broken(), PAGE)
        self.assertNotIn("secret", str(ctx.exception))
        self.assertNotIn("Bearer", str(ctx.exception))


class CollectorBoundaryTests(unittest.TestCase):
    """The spy: the universal collectors must not run for a registered reader."""

    def test_specific_adapter_never_invokes_the_generic_sweep(self):
        driver = FakeDriver()
        # FakeDriver raises if any script other than the reader query is executed.
        WEBTOONS.collect_reader_payload(driver, PAGE)
        self.assertEqual(driver.scripts, 1)

    def test_analyze_routes_by_strategy_not_by_host(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parent / "chapter_source.py").read_text(
            encoding="utf-8")
        # Anchor on the implementation, not the Protocol stub of the same name earlier in
        # the file, or the slice swallows half the module.
        start = source.index("Analyse a driver/context without fetching")
        body = source[start:source.index("    def collect_reader_payload", start)]
        self.assertIn("collection_strategy", body)
        for hardcoded in ("webtoons.com", "vortexscans.org", "pstatic"):
            self.assertNotIn(hardcoded, body, hardcoded)

    def test_universal_keeps_the_generic_multisource_path(self):
        universal = UniversalChapterAdapter("https://ex.test/serie/cap-1")
        self.assertEqual(universal.collection_strategy, "generic_multisource")
        # It must not have been switched to the reader-only path.
        self.assertNotEqual(universal.collection_strategy, "adapter_specific")


if __name__ == "__main__":
    unittest.main()
