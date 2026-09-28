"""Reader-indexed short pages must survive the small-image heuristic.

Physical bug (comix.to between-two-floors ch.1, 208 reader page controls): pages 48, 118
and 143 are legitimate short WebP strips (800x32, 800x83, 800x56) bound to
``button[aria-label="Go to page N"]``, yet the generic classifier dropped them for
``small_dimensions`` (height < 120) -> ACCEPTED=205 and incomplete coverage. The coverage
gate correctly expects 1..208 and is unchanged; the fix is in the classifier only.

A resource explicitly bound to a reader page control carries ``logical_index > 0`` (from
the resolver's ``logical_page_index``). That is the structural signal that it is a declared
page, so ``small_dimensions`` no longer rejects it on size alone -- while every other
protection, including the <=4x4 tracking-pixel floor, still applies.
"""
import unittest

from universal_chapter_adapter import ImageCandidate, _negative_reason


def _cand(*, w, h, logical_index=0, url="https://cdn.example/p.webp", visible=True,
          class_name="", alt="", context="", element_id=""):
    return ImageCandidate(
        id=f"c-{url}-{logical_index}-{w}x{h}", url=url, source="dom", order=logical_index or 1,
        width=w, height=h, logical_index=logical_index, visible=visible,
        class_name=class_name, alt=alt, context=context, element_id=element_id,
    )


class ReaderIndexedSmallPageTests(unittest.TestCase):
    # A) generic short strip, no reader association -> rejected (old behavior preserved)
    def test_generic_small_image_rejected(self):
        self.assertEqual(_negative_reason(_cand(w=800, h=32)), "small_dimensions")

    # B/C/D) reader-indexed short strips (the three real pages) -> accepted
    def test_reader_indexed_800x32_accepted(self):
        self.assertEqual(_negative_reason(_cand(w=800, h=32, logical_index=48)), "")

    def test_reader_indexed_800x83_accepted(self):
        self.assertEqual(_negative_reason(_cand(w=800, h=83, logical_index=118)), "")

    def test_reader_indexed_800x56_accepted(self):
        self.assertEqual(_negative_reason(_cand(w=800, h=56, logical_index=143)), "")

    # F) reader-indexed tracking pixel / 1x1 -> still rejected (floor unaffected)
    def test_reader_indexed_tracking_pixel_rejected(self):
        self.assertEqual(_negative_reason(_cand(w=1, h=1, logical_index=5)), "tracking_pixel")

    # F') reader-indexed but a proven interface/ad element -> still rejected (by token,
    # never bypassed just because it is reader-indexed) and NOT via small_dimensions.
    def test_reader_indexed_negative_token_still_rejected(self):
        reason = _negative_reason(_cand(w=800, h=40, logical_index=7, class_name="advertisement"))
        self.assertNotEqual(reason, "")
        self.assertNotEqual(reason, "small_dimensions")

    # hidden reader-indexed -> still rejected
    def test_reader_indexed_hidden_rejected(self):
        self.assertEqual(_negative_reason(_cand(w=800, h=40, logical_index=9, visible=False)),
                         "hidden_candidate")

    # G) generic tiny asset -> old behavior preserved
    def test_generic_tiny_rejected(self):
        self.assertEqual(_negative_reason(_cand(w=60, h=60)), "small_dimensions")

    # H) reader-indexed normal page -> accepted
    def test_reader_indexed_normal_accepted(self):
        self.assertEqual(_negative_reason(_cand(w=800, h=1000, logical_index=1)), "")

    def test_208_coverage_no_page_dropped_by_classifier(self):
        # 205 normal pages + the 3 short reader-indexed pages = 208 declared pages.
        small = {48: 32, 118: 83, 143: 56}
        candidates = []
        for n in range(1, 209):
            h = small.get(n, 1000)
            candidates.append(_cand(w=800, h=h, logical_index=n,
                                    url=f"https://cdn.example/{n:03}.webp"))
        rejected = [c.logical_index for c in candidates if _negative_reason(c)]
        self.assertEqual(rejected, [])                       # classifier drops none
        accepted = sum(1 for c in candidates if not _negative_reason(c))
        self.assertEqual(accepted, 208)                      # was 205 before the fix
        # And the three specific pages survive.
        for n, h in small.items():
            self.assertEqual(_negative_reason(_cand(w=800, h=h, logical_index=n)), "",
                             f"page {n} ({800}x{h}) must survive")


if __name__ == "__main__":
    unittest.main()
