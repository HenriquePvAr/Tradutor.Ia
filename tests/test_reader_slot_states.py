"""Lazy reader slots: pending until they resolve, never rejected on the first read.

A reader hands out placeholder elements first. Measured live, 164 of 167 reader elements
were 1x1 placeholders on a separate host moments after load, and only 3 held real pages.
Treating a placeholder as furniture on the first read silently shrinks the chapter; treating
its host as a page host would authorize the placeholder CDN. Neither is acceptable.

Hermetic: plain dictionaries, no browser, no network.
"""

import _test_bootstrap  # noqa: F401

import unittest

from chapter_source import (
    SLOT_PENDING, SLOT_REJECTED, SLOT_RESOLVED, classify_reader_slots, pending_indices,
    reader_coverage_complete, select_adapter, slot_counts, slot_state,
)

WEBTOONS = select_adapter(
    "https://www.webtoons.com/en/drama/serie/episode-1/viewer?title_no=1&episode_no=1")

PAGE_HOST = "webtoon-phinf.pstatic.net"
PLACEHOLDER_HOST = "webtoons-static.pstatic.net"


def slot(order, *, host=PAGE_HOST, w=800, h=1280, cls="_images", alt=""):
    return {"url": f"https://{host}/p{order:03}.jpg", "order": order,
            "naturalWidth": w, "naturalHeight": h, "inContainer": True,
            "className": cls, "id": "", "alt": alt}


def placeholder(order):
    return slot(order, host=PLACEHOLDER_HOST, w=1, h=1)


class SlotStateTests(unittest.TestCase):
    def test_a_resolved_page_is_resolved(self):
        self.assertEqual(slot_state(slot(0), adapter=WEBTOONS), SLOT_RESOLVED)

    def test_a_one_by_one_placeholder_is_pending_not_rejected(self):
        # The whole point: it trips the tracking-pixel rule by size, but it is a slot that
        # has not loaded, not furniture.
        self.assertEqual(slot_state(placeholder(0), adapter=WEBTOONS), SLOT_PENDING)

    def test_a_slot_without_a_url_is_pending(self):
        empty = slot(0)
        empty["url"] = ""
        self.assertEqual(slot_state(empty, adapter=WEBTOONS), SLOT_PENDING)

    def test_interface_furniture_is_rejected_immediately(self):
        for marker, field in (("logo", "className"), ("advert", "alt"), ("footer", "id")):
            item = slot(0)
            item[field] = f"x-{marker}-y"
            self.assertEqual(slot_state(item, adapter=WEBTOONS), SLOT_REJECTED, marker)

    def test_a_resolved_image_on_an_unauthorized_host_is_rejected(self):
        # A placeholder that "resolved" to real dimensions on the placeholder CDN is still
        # not a page: the host was never authorized.
        item = slot(0, host=PLACEHOLDER_HOST, w=800, h=1280)
        self.assertEqual(slot_state(item, adapter=WEBTOONS), SLOT_REJECTED)

    def test_a_too_small_resolved_image_is_rejected(self):
        self.assertEqual(slot_state(slot(0, w=64, h=64), adapter=WEBTOONS), SLOT_REJECTED)


class SlotOrderingTests(unittest.TestCase):
    def test_dom_order_survives_out_of_order_resolution(self):
        # Slots resolve in a scrambled order; the manifest must not follow that order.
        candidates = [placeholder(0), slot(1), placeholder(2), slot(3), slot(4)]
        scrambled = [candidates[3], candidates[0], candidates[4], candidates[2], candidates[1]]
        slots = classify_reader_slots(scrambled, adapter=WEBTOONS)
        self.assertEqual([s["index"] for s in slots], [0, 1, 2, 3, 4])

    def test_index_is_taken_from_the_reader_position(self):
        slots = classify_reader_slots([slot(7), slot(2)], adapter=WEBTOONS)
        self.assertEqual([s["index"] for s in slots], [2, 7])


class CoverageTests(unittest.TestCase):
    def measured_shape(self):
        """3 resolved, 164 pending — the live observation shortly after load."""
        return [slot(i) if i < 3 else placeholder(i) for i in range(167)]

    def test_the_measured_initial_shape_is_not_complete(self):
        slots = classify_reader_slots(self.measured_shape(), adapter=WEBTOONS)
        counts = slot_counts(slots)
        self.assertEqual(counts["total"], 167)
        self.assertEqual(counts["resolved"], 3)
        self.assertEqual(counts["pending"], 164)
        self.assertEqual(counts["rejected"], 0)
        self.assertFalse(reader_coverage_complete(slots))

    def test_pending_indices_are_reported_for_diagnosis(self):
        slots = classify_reader_slots(self.measured_shape(), adapter=WEBTOONS)
        self.assertEqual(pending_indices(slots)[:3], [3, 4, 5])
        self.assertEqual(len(pending_indices(slots)), 164)

    def test_fully_resolved_reader_is_complete(self):
        slots = classify_reader_slots([slot(i) for i in range(20)], adapter=WEBTOONS)
        self.assertTrue(reader_coverage_complete(slots))
        self.assertEqual(slot_counts(slots)["pending"], 0)

    def test_one_pending_slot_blocks_completion(self):
        candidates = [slot(i) for i in range(20)]
        candidates[9] = placeholder(9)
        slots = classify_reader_slots(candidates, adapter=WEBTOONS)
        self.assertFalse(reader_coverage_complete(slots))
        self.assertEqual(pending_indices(slots), [9])

    def test_rejected_furniture_does_not_block_completion(self):
        candidates = [slot(i) for i in range(10)]
        candidates[4]["className"] = "recommend"
        slots = classify_reader_slots(candidates, adapter=WEBTOONS)
        self.assertTrue(reader_coverage_complete(slots))
        self.assertEqual(slot_counts(slots)["rejected"], 1)

    def test_an_empty_reader_is_never_complete(self):
        self.assertFalse(reader_coverage_complete([]))

    def test_a_reader_of_only_placeholders_is_never_complete(self):
        slots = classify_reader_slots([placeholder(i) for i in range(5)], adapter=WEBTOONS)
        self.assertFalse(reader_coverage_complete(slots))


class ManifestPurityTests(unittest.TestCase):
    def test_the_placeholder_host_is_not_authorized_as_a_page_host(self):
        from chapter_source import SourceError

        with self.assertRaises(SourceError):
            WEBTOONS.authorize_related_url(f"https://{PLACEHOLDER_HOST}/x.jpg")
        WEBTOONS.authorize_related_url(f"https://{PAGE_HOST}/x.jpg")

    def test_the_placeholder_host_is_absent_from_resource_hosts(self):
        self.assertNotIn(PLACEHOLDER_HOST, WEBTOONS.resource_hosts)
        self.assertIn(PAGE_HOST, WEBTOONS.resource_hosts)

    def test_only_resolved_slots_would_reach_a_manifest(self):
        slots = classify_reader_slots(
            [slot(0), placeholder(1), slot(2)], adapter=WEBTOONS)
        resolved = [s["candidate"] for s in slots if s["state"] == SLOT_RESOLVED]
        self.assertEqual(len(resolved), 2)
        for candidate in resolved:
            self.assertNotIn(PLACEHOLDER_HOST, candidate["url"])


if __name__ == "__main__":
    unittest.main()
