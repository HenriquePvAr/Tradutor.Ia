"""Lazy reader scroll must stop on positive source stability, not on the round cap.

The reader exposes its pages lazily: a fixed scroll walk over a 200k px reader cannot reach
the document end inside the round budget, so the old loop always exhausted the cap and slept
once per wasted round. These tests pin the replacement: stop when the canonical source
population is stable *and* the reader is exhausted, without ever stopping while a virtualized
reader still has pages to hand out.
"""

from __future__ import annotations

import _test_bootstrap  # noqa: F401

import hashlib
import unittest

import down


EP51_SOURCES = [
    f"https://webtoon-phinf.pstatic.net/2024/ep51/{index:03d}.jpg" for index in range(171)
]


def manifest_hash(urls):
    joined = "\n".join(urls).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()


class FakeReader:
    """A scriptable stand-in for the Selenium driver: no browser, no network."""

    def __init__(self, *, height=5000, viewport=900, sources=None, images=None,
                 height_fn=None, stuck=False, error_after=None):
        self.viewport = viewport
        self._height = height
        self.height_fn = height_fn
        self.sources_fn = sources or (lambda reader: [])
        self.images_fn = images
        self.stuck = stuck
        self.error_after = error_after
        self.scroll_y = 0
        self.reads = 0
        self.scroll_targets = []

    @property
    def height(self):
        return int(self.height_fn(self) if self.height_fn else self._height)

    def current_sources(self):
        return [str(url) for url in self.sources_fn(self)]

    def image_count(self):
        if self.images_fn is not None:
            return int(self.images_fn(self))
        return len(self.current_sources())

    def _scroll(self, position):
        self.scroll_targets.append(int(position))
        if self.stuck:
            return
        self.scroll_y = max(0, min(int(position), max(self.height - self.viewport, 0)))

    def execute_script(self, script, *args):
        if self.error_after is not None and self.reads >= self.error_after:
            raise RuntimeError("browser session died")
        if "imageCount" in script:
            self.reads += 1
            return {"imageCount": self.image_count(), "urls": self.current_sources()}
        if "window.scrollTo" in script:
            if "document.body.scrollHeight" in script:
                self._scroll(self.height)
            else:
                self._scroll(args[0])
            return True
        if "scrollHeight" in script:
            return self.height
        if "document.images" in script:
            return self.image_count()
        if "innerHeight" in script:
            return self.viewport
        if "scrollY" in script:
            return self.scroll_y
        raise AssertionError(f"unexpected script: {script[:60]}")


def run_scroll(reader, **kwargs):
    slept = []
    kwargs.setdefault("sleep", slept.append)
    result = down._scroll_incrementally(reader, **kwargs)
    result = dict(result)
    result["_slept"] = slept
    return result


def fixed_sources(urls):
    return lambda reader: list(urls)


def revealed_by_round(urls, per_round, stop_at=None):
    """Lazy reveal: more identities appear as rounds pass, then the population freezes."""

    def sources(reader):
        rounds = reader.reads if stop_at is None else min(reader.reads, stop_at)
        return list(urls[: min(len(urls), per_round * (rounds + 1))])

    return sources


class ImmediateStableBottomTest(unittest.TestCase):
    def test_fully_loaded_reader_stops_after_bounded_confirmation(self):
        reader = FakeReader(height=3000, viewport=1000, sources=fixed_sources(EP51_SOURCES[:8]))
        result = run_scroll(reader, max_rounds=90, stable_rounds=3)

        self.assertLessEqual(result["rounds"], 8)
        self.assertEqual(result["stop_reason"], down.SCROLL_STOP_SOURCE_STABLE)
        self.assertTrue(result["stabilized"])
        self.assertEqual(result["final_source_count"], 8)


class Episode51FrozenReplayTest(unittest.TestCase):
    """The real reader geometry: the walk cannot reach the end inside the round budget."""

    def _reader(self):
        return FakeReader(
            height=224653,
            viewport=1080,
            sources=revealed_by_round(EP51_SOURCES, 30, stop_at=6),
        )

    def test_full_population_without_exhausting_the_round_cap(self):
        reader = self._reader()
        result = run_scroll(reader, max_rounds=90, stable_rounds=3)

        self.assertEqual(result["final_source_count"], len(EP51_SOURCES))
        self.assertLess(result["rounds"], 25)
        self.assertNotEqual(result["stop_reason"], down.SCROLL_STOP_HARD_CAP)
        self.assertLess(sum(result["_slept"]), 30.0)

    def test_manifest_identity_and_order_preserved(self):
        reader = self._reader()
        run_scroll(reader, max_rounds=90, stable_rounds=3)
        snapshot = down._viewer_image_snapshot(reader)

        self.assertEqual(snapshot["urls"], EP51_SOURCES)
        self.assertEqual(manifest_hash(snapshot["urls"]), manifest_hash(EP51_SOURCES))


class VirtualizedReaderSafetyTest(unittest.TestCase):
    def test_temporary_mid_reader_stability_does_not_stop_discovery(self):
        # 101 of 171 pages are available, then the reader pauses, then the rest appear.
        def sources(reader):
            if reader.reads < 4:
                return EP51_SOURCES[:101]
            return EP51_SOURCES

        reader = FakeReader(height=224653, viewport=1080, sources=sources)
        result = run_scroll(reader, max_rounds=90, stable_rounds=3)

        self.assertEqual(result["final_source_count"], len(EP51_SOURCES))

    def test_delayed_lazy_load_tolerated(self):
        def sources(reader):
            if reader.reads < 3:
                return EP51_SOURCES[:20]
            return EP51_SOURCES[:40]

        reader = FakeReader(height=40000, viewport=1000, sources=sources)
        result = run_scroll(reader, max_rounds=90, stable_rounds=3)

        self.assertEqual(result["final_source_count"], 40)

    def test_new_identity_resets_the_stable_streak(self):
        def sources(reader):
            return EP51_SOURCES[: 10 + reader.reads]

        reader = FakeReader(height=60000, viewport=1000, sources=sources)
        result = run_scroll(reader, max_rounds=12, stable_rounds=3)

        self.assertEqual(result["stop_reason"], down.SCROLL_STOP_HARD_CAP)
        self.assertEqual(result["stable_rounds_observed"], 0)

    def test_recycled_dom_slot_does_not_inflate_or_reset(self):
        # Constant DOM node count, identical identities: recycling is not progress.
        reader = FakeReader(
            height=3000, viewport=1000,
            sources=fixed_sources(EP51_SOURCES[:5]),
            images=lambda reader: 5,
        )
        result = run_scroll(reader, max_rounds=90, stable_rounds=3)

        self.assertEqual(result["final_source_count"], 5)
        self.assertLessEqual(result["rounds"], 8)

    def test_virtualized_nodes_reused_while_identities_change(self):
        # DOM node count never changes, but the identities behind them do: that is progress.
        def sources(reader):
            start = min(reader.reads, 6) * 10
            return EP51_SOURCES[start:start + 10]

        reader = FakeReader(
            height=224653, viewport=1080, sources=sources, images=lambda reader: 10,
        )
        result = run_scroll(reader, max_rounds=90, stable_rounds=3)

        # Six distinct windows of ten pages were exposed while the node count stayed at ten.
        self.assertEqual(result["final_source_count"], 60)
        self.assertGreaterEqual(result["rounds_with_new_source"], 6)

    def test_growing_scroll_height_keeps_discovery_running(self):
        def height_fn(reader):
            return 20000 + 5000 * min(reader.reads, 8)

        reader = FakeReader(
            viewport=1000, sources=fixed_sources(EP51_SOURCES[:12]), height_fn=height_fn,
        )
        result = run_scroll(reader, max_rounds=90, stable_rounds=3)

        self.assertGreater(result["rounds"], 8)
        self.assertEqual(result["final_source_count"], 12)


class ExhaustionSignalTest(unittest.TestCase):
    def test_scroll_that_cannot_advance_stops_once_sources_are_stable(self):
        reader = FakeReader(
            height=224653, viewport=1080,
            sources=fixed_sources(EP51_SOURCES[:30]), stuck=True,
        )
        result = run_scroll(reader, max_rounds=90, stable_rounds=3)

        self.assertEqual(result["stop_reason"], down.SCROLL_STOP_NO_PROGRESS)
        self.assertLessEqual(result["rounds"], 8)

    def test_pathological_reader_still_hits_the_hard_cap(self):
        def sources(reader):
            return [f"https://example.test/{reader.reads}/{index}.jpg" for index in range(5)]

        reader = FakeReader(
            height=224653, viewport=1080, sources=sources,
            height_fn=lambda reader: 224653 + 1000 * reader.reads,
        )
        result = run_scroll(reader, max_rounds=11, stable_rounds=3)

        self.assertEqual(result["rounds"], 11)
        self.assertEqual(result["stop_reason"], down.SCROLL_STOP_HARD_CAP)
        self.assertFalse(result["stabilized"])

    def test_browser_error_is_not_a_stable_completion(self):
        reader = FakeReader(
            height=224653, viewport=1080,
            sources=fixed_sources(EP51_SOURCES[:20]), error_after=2,
        )
        with self.assertRaises(RuntimeError):
            run_scroll(reader, max_rounds=90, stable_rounds=3)

    def test_cancellation_still_fails_closed(self):
        from chapter_source import SourceError

        reader = FakeReader(height=224653, viewport=1080,
                            sources=fixed_sources(EP51_SOURCES[:20]))
        calls = {"n": 0}

        def cancel_check():
            calls["n"] += 1
            return calls["n"] > 2

        with self.assertRaises(SourceError):
            run_scroll(reader, max_rounds=90, stable_rounds=3, cancel_check=cancel_check)


class DeterminismTest(unittest.TestCase):
    def test_same_population_different_timing_gives_the_same_manifest(self):
        def slow(reader):
            return EP51_SOURCES[: min(len(EP51_SOURCES), 20 * (reader.reads + 1))]

        def fast(reader):
            return EP51_SOURCES[: min(len(EP51_SOURCES), 90 * (reader.reads + 1))]

        manifests = []
        for reveal in (slow, fast):
            reader = FakeReader(height=224653, viewport=1080, sources=reveal)
            run_scroll(reader, max_rounds=90, stable_rounds=3)
            manifests.append(down._viewer_image_snapshot(reader)["urls"])

        self.assertEqual(manifests[0], manifests[1])
        self.assertEqual(manifests[0], EP51_SOURCES)


if __name__ == "__main__":
    unittest.main()
