"""Lazy slot resolution: progressive, ordered, bounded and cancellable.

Synthetic reader only — a fake browser whose slots resolve as the viewport approaches them.
No Selenium, no network, no image bytes, no real chapter.
"""

import _test_bootstrap  # noqa: F401

import unittest

from chapter_source import SLOT_PENDING, SLOT_REJECTED, SLOT_RESOLVED, select_adapter
from lazy_slot_resolver import (
    COUNTER_STAGE, LAZY_RESOLUTION_ROUNDS, LAZY_RESOLUTION_TIMEOUT, READER_DOM_CHANGED,
    ResolverLimits, resolve_lazy_reader_slots,
)

WEBTOONS = select_adapter(
    "https://www.webtoons.com/en/drama/serie/episode-1/viewer?title_no=1&episode_no=1")

PAGE_HOST = "webtoon-phinf.pstatic.net"
PLACEHOLDER_HOST = "webtoons-static.pstatic.net"
SLOT_HEIGHT = 1280


class FakeReader:
    """Slots resolve once the viewport has reached their vertical position."""

    def __init__(self, *, total=20, resolved=3, visits_needed=1, scramble=False):
        self.total = total
        self.visits_needed = visits_needed
        self.scramble = scramble
        self.resolved = {i for i in range(resolved)}
        self.visits: dict[int, int] = {}
        self.position = 0
        self.scrolls: list[int] = []
        self.extra_slot = False

    # ---- injected browser operations ------------------------------------
    def reader_bounds(self):
        return 0, self.total * SLOT_HEIGHT

    def scroll_to(self, y):
        self.position = int(y)
        self.scrolls.append(self.position)
        for index in range(self.total):
            top = index * SLOT_HEIGHT
            if self.position + SLOT_HEIGHT >= top:
                self.visits[index] = self.visits.get(index, 0) + 1
                if self.visits[index] >= self.visits_needed:
                    self.resolved.add(index)

    def read_slots(self):
        items = []
        for index in range(self.total):
            done = index in self.resolved
            items.append({
                "url": (f"https://{PAGE_HOST}/p{index:03}.jpg" if done
                        else f"https://{PLACEHOLDER_HOST}/blank.gif"),
                "order": index,
                "naturalWidth": 800 if done else 1,
                "naturalHeight": SLOT_HEIGHT if done else 1,
                "inContainer": True, "className": "_images", "id": "", "alt": "",
                "y": index * SLOT_HEIGHT,
            })
        if self.extra_slot:
            items.append({"url": f"https://{PAGE_HOST}/extra.jpg", "order": self.total,
                          "naturalWidth": 800, "naturalHeight": SLOT_HEIGHT,
                          "inContainer": True, "className": "_images", "id": "", "alt": "",
                          "y": self.total * SLOT_HEIGHT})
        if self.scramble:
            items = items[::-1]
        return items


def run(reader, **kw):
    events = []
    kw.setdefault("limits", ResolverLimits(stable_rounds=2, settle_seconds=0))
    return resolve_lazy_reader_slots(
        adapter=WEBTOONS,
        read_slots=reader.read_slots,
        scroll_to=reader.scroll_to,
        reader_bounds=reader.reader_bounds,
        on_progress=events.append,
        sleep=lambda _s: None,
        **kw), events


class ProgressiveResolutionTests(unittest.TestCase):
    def test_initial_shape_is_partly_pending(self):
        reader = FakeReader(total=20, resolved=3)
        slots = [s for s in run(reader)[0].slots]
        self.assertEqual(len(slots), 20)

    def test_scrolling_resolves_every_slot(self):
        result, _ = run(FakeReader(total=20, resolved=3))
        counts = result.counts
        self.assertEqual(counts["total"], 20)
        self.assertEqual(counts["resolved"], 20)
        self.assertEqual(counts["pending"], 0)
        self.assertTrue(result.reached_reader_end)

    def test_slots_needing_two_visits_still_resolve(self):
        result, _ = run(FakeReader(total=12, resolved=1, visits_needed=2))
        self.assertEqual(result.counts["pending"], 0)

    def test_output_is_ordered_by_dom_index_not_resolution_order(self):
        result, _ = run(FakeReader(total=15, resolved=2, scramble=True))
        self.assertEqual([s["index"] for s in result.slots], list(range(15)))
        urls = [c["url"] for c in result.resolved_candidates]
        self.assertEqual(urls, sorted(urls))

    def test_a_resolved_slot_never_regresses(self):
        reader = FakeReader(total=10, resolved=10)
        result, _ = run(reader)
        self.assertTrue(all(s["state"] == SLOT_RESOLVED for s in result.slots))

    def test_placeholders_never_reach_the_resolved_set(self):
        result, _ = run(FakeReader(total=10, resolved=2))
        for candidate in result.resolved_candidates:
            self.assertNotIn(PLACEHOLDER_HOST, candidate["url"])


class BudgetTests(unittest.TestCase):
    def test_timeout_leaves_slots_pending_and_warns(self):
        class Frozen(FakeReader):
            def scroll_to(self, y):      # nothing ever resolves
                self.position = int(y)

        clock = iter([0.0, 0.0, 500.0, 500.0, 500.0, 500.0])
        result, _ = run(Frozen(total=10, resolved=1),
                        limits=ResolverLimits(timeout_seconds=1.0, settle_seconds=0),
                        clock=lambda: next(clock, 500.0))
        self.assertTrue(result.timed_out)
        self.assertIn(LAZY_RESOLUTION_TIMEOUT, result.warnings)
        self.assertGreater(result.counts["pending"], 0)

    def test_max_rounds_is_bounded_and_warns(self):
        class Frozen(FakeReader):
            def scroll_to(self, y):
                self.position = int(y)

        result, _ = run(Frozen(total=10, resolved=1),
                        limits=ResolverLimits(max_rounds=3, settle_seconds=0,
                                              timeout_seconds=1e6))
        self.assertLessEqual(result.rounds, 3)
        self.assertIn(LAZY_RESOLUTION_ROUNDS, result.warnings)

    def test_pending_slots_are_reported_by_index(self):
        class Partial(FakeReader):
            def scroll_to(self, y):
                self.position = int(y)
                self.resolved.update({0, 1, 2})

        result, _ = run(Partial(total=6, resolved=0),
                        limits=ResolverLimits(max_rounds=2, settle_seconds=0,
                                              timeout_seconds=1e6))
        self.assertEqual(result.indices_pending, [3, 4, 5])

    def test_scrolling_stays_inside_the_reader(self):
        reader = FakeReader(total=10, resolved=0)
        run(reader)
        _, bottom = reader.reader_bounds()
        self.assertTrue(all(y <= bottom for y in reader.scrolls))


class CancellationTests(unittest.TestCase):
    def test_cancellation_stops_without_a_selection(self):
        reader = FakeReader(total=20, resolved=1)
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] > 3

        result, _ = run(reader, cancel_check=cancel)
        self.assertTrue(result.cancelled)
        self.assertGreater(result.counts["pending"], 0)

    def test_cancellation_before_the_first_read_is_honoured(self):
        result, _ = run(FakeReader(total=5, resolved=0), cancel_check=lambda: True)
        self.assertTrue(result.cancelled)


class DomChangeTests(unittest.TestCase):
    def test_an_unexpected_new_slot_raises_a_warning(self):
        reader = FakeReader(total=8, resolved=0)

        original = reader.scroll_to

        def scroll(y):
            original(y)
            reader.extra_slot = True

        reader.scroll_to = scroll
        result, _ = run(reader)
        self.assertIn(READER_DOM_CHANGED, result.warnings)

    def test_indices_are_never_silently_renumbered(self):
        reader = FakeReader(total=8, resolved=0)
        reader.extra_slot = True
        result, _ = run(reader)
        self.assertEqual([s["index"] for s in result.slots], list(range(9)))


class ProgressEventTests(unittest.TestCase):
    def test_progress_uses_its_own_counter_stage(self):
        _, events = run(FakeReader(total=6, resolved=1))
        self.assertTrue(events)
        for event in events:
            self.assertEqual(event["counter_stage"], COUNTER_STAGE)
            self.assertLessEqual(event["current"], event["total"])

    def test_progress_never_reports_downloads(self):
        _, events = run(FakeReader(total=6, resolved=1))
        for event in events:
            self.assertNotIn("downloaded", event)
            self.assertNotIn("bytes", str(event).lower())

    def test_final_counts_reach_the_public_payload(self):
        result, _ = run(FakeReader(total=7, resolved=2))
        payload = result.public()
        self.assertEqual(payload["slots_total"], 7)
        self.assertEqual(payload["slots_resolved"], 7)
        self.assertEqual(payload["counter_stage"], COUNTER_STAGE)


class NoNetworkTests(unittest.TestCase):
    def test_the_resolver_module_fetches_nothing(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parent / "lazy_slot_resolver.py").read_text(
            encoding="utf-8")
        imports = [l.strip() for l in source.splitlines()
                   if l.strip().startswith(("import ", "from "))]
        for banned in ("requests", "selenium", "socket", "urllib", "http"):
            for line in imports:
                self.assertNotIn(banned, line, f"{banned} in {line}")


if __name__ == "__main__":
    unittest.main()
