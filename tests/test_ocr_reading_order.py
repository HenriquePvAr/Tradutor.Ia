"""Contract: a recovery may not reorder the sentence it just finished saving.

TDD #35 stopped a region re-read from erasing its predecessor's lexical tail.
It did not stop the pipeline from putting the tail back in the wrong place: a
predecessor line split by the re-read into two fragments on the same printed row
was ordered by a lexicographic ``(bbox.y, bbox.x)`` sort, so four pixels of
vertical jitter between "KILLED" and "EVERYONE..." was enough to emit the suffix
before the prefix.  Every token present, sentence scrambled.

Reading order here means: rows top to bottom, and within a row the page's
horizontal direction.  A bbox ``y`` alone is not a reading order.

Every fixture is synthetic geometry.  No recogniser, no network, no chapter.
"""

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest

import numpy as np

import ocr_balloon
from ocr_balloon import RECOVERY_RECONCILE, RECOVERY_REJECT, reconcile_recovery_lines
from ocr_engine import OCRLine


def _line(text, box, confidence=0.95):
    x, y, width, height = box
    polygon = np.array(
        [[x, y], [x + width, y], [x + width, y + height], [x, y + height]],
        dtype=np.int32,
    )
    return OCRLine(
        text=text,
        confidence=confidence,
        polygon=polygon,
        box=tuple(box),
        raw_text=text,
        engine="rapidocr",
        page=2,
    )


def _ordered_text(lines):
    return " ".join(line.text for line in ocr_balloon.reading_order(lines))


def _grouped_text(lines):
    groups = ocr_balloon._group_lines(lines)
    return [group.text for group in groups]


# The p002 BALAO_3 geometry, straight out of the Full #7 provenance artifact.
P002_PREDECESSORS = [
    ("IT'SNOT JUST", (325, 2028, 244, 37)),
    ("THAT I LOSTCONTROL", (260, 2075, 376, 33)),
    ("AGAINANDALMOST", (282, 2121, 333, 33)),
    ("KILLEDEVERYONE...", (288, 2165, 319, 32)),
]
P002_RECOVERY = [
    ("IT'S NOT JUST", (328, 2030, 240, 34)),
    ("THAT I LOST", (260, 2075, 212, 32)),
    ("AGAIN AND ALMOST", (282, 2120, 332, 32)),
    ("EVERYONE...", (404, 2162, 202, 38)),
    ("KILLED", (288, 2166, 110, 30)),
]


class ReadingOrderTests(unittest.TestCase):
    def test_simple_multiline_keeps_its_order(self):
        lines = [
            _line("LINE ONE", (100, 100, 200, 30)),
            _line("LINE TWO", (100, 145, 200, 30)),
            _line("LINE THREE", (100, 190, 220, 30)),
        ]
        self.assertEqual(_ordered_text(lines), "LINE ONE LINE TWO LINE THREE")

    def test_indented_line_stays_below_not_before(self):
        lines = [
            _line("LINE ONE", (100, 100, 200, 30)),
            _line("LINE TWO", (160, 145, 140, 30)),
        ]
        self.assertEqual(_ordered_text(lines), "LINE ONE LINE TWO")

    def test_input_order_does_not_freeze_a_wrong_sequence(self):
        # Geometry, not list order, is authoritative: a recogniser that emits
        # its lines bottom-up is corrected rather than obeyed.
        lines = [
            _line("LINE THREE", (100, 190, 220, 30)),
            _line("LINE TWO", (100, 145, 200, 30)),
            _line("LINE ONE", (100, 100, 200, 30)),
        ]
        self.assertEqual(_ordered_text(lines), "LINE ONE LINE TWO LINE THREE")

    def test_small_vertical_jitter_does_not_flip_a_row(self):
        lines = [
            _line("SECOND", (400, 200, 150, 38)),
            _line("FIRST", (200, 204, 130, 30)),
        ]
        self.assertEqual(_ordered_text(lines), "FIRST SECOND")

    def test_split_fragment_keeps_prefix_before_suffix(self):
        # One predecessor row read back as two fragments: no suffix first.
        lines = [
            _line("ALMOST", (100, 100, 200, 30)),
            _line("EVERYONE...", (404, 2162, 202, 38)),
            _line("KILLED", (288, 2166, 110, 30)),
        ]
        self.assertEqual(_ordered_text(lines), "ALMOST KILLED EVERYONE...")

    def test_rows_stay_separate_when_they_do_not_overlap(self):
        lines = [
            _line("BELOW", (100, 200, 150, 30)),
            _line("ABOVE", (400, 100, 150, 30)),
        ]
        self.assertEqual(_ordered_text(lines), "ABOVE BELOW")

    def test_order_is_stable_for_identical_geometry(self):
        first = _line("ALPHA", (100, 100, 150, 30))
        second = _line("BETA", (100, 100, 150, 30))
        self.assertEqual(_ordered_text([first, second]), "ALPHA BETA")
        self.assertEqual(_ordered_text([second, first]), "BETA ALPHA")


class P002ReadingOrderTests(unittest.TestCase):
    """The exact Full #7 defect, replayed offline."""

    def test_reconciled_group_reads_in_source_order(self):
        predecessors = [_line(text, box) for text, box in P002_PREDECESSORS]
        candidates = [_line(text, box) for text, box in P002_RECOVERY]
        decision, lines, _reason = reconcile_recovery_lines(predecessors, candidates)
        self.assertEqual(decision, RECOVERY_RECONCILE)
        self.assertEqual(
            _grouped_text(lines),
            ["IT'S NOT JUST THAT I LOST CONTROL AGAIN AND ALMOST KILLED EVERYONE..."],
        )

    def test_control_and_geometry_survive_the_ordering_fix(self):
        # TDD #35 regression: the reconciled tail and the geometry it lived in.
        predecessors = [_line(text, box) for text, box in P002_PREDECESSORS]
        candidates = [_line(text, box) for text, box in P002_RECOVERY]
        _decision, lines, _reason = reconcile_recovery_lines(predecessors, candidates)
        ordered = ocr_balloon.reading_order(lines)
        self.assertIn("CONTROL", " ".join(line.text for line in ordered))
        self.assertEqual(
            " ".join(line.text for line in ordered).count("CONTROL"), 1
        )
        widest = max(line.box[2] for line in ordered if "CONTROL" in line.text)
        self.assertEqual(widest, 376)


class RecoveryOrderingSafetyTests(unittest.TestCase):
    def test_neighbouring_balloons_are_not_merged_by_ordering(self):
        lines = [
            _line("LEFT ONE", (100, 100, 200, 30)),
            _line("LEFT TWO", (100, 145, 200, 30)),
            _line("RIGHT ONE", (1400, 102, 200, 30)),
            _line("RIGHT TWO", (1400, 147, 200, 30)),
        ]
        texts = _grouped_text(lines)
        self.assertEqual(len(texts), 2)
        self.assertIn("LEFT ONE LEFT TWO", texts)
        self.assertIn("RIGHT ONE RIGHT TWO", texts)

    def test_semantic_conflict_still_fails_closed(self):
        predecessors = [_line("I WILL KILL HIM", (100, 100, 300, 30))]
        candidates = [_line("I WILL HELP HIM", (100, 100, 300, 30))]
        decision, lines, reason = reconcile_recovery_lines(predecessors, candidates)
        self.assertEqual(decision, RECOVERY_REJECT)
        self.assertIn("conflict", reason)
        self.assertEqual(" ".join(line.text for line in lines), "I WILL KILL HIM")

    def test_expansion_does_not_duplicate_or_reorder(self):
        predecessors = [_line("I LOST", (100, 100, 120, 30))]
        candidates = [_line("I LOST CONTROL", (100, 100, 260, 30))]
        _decision, lines, _reason = reconcile_recovery_lines(predecessors, candidates)
        self.assertEqual(_ordered_text(lines), "I LOST CONTROL")

    def test_merged_predecessors_keep_their_lexical_sequence(self):
        predecessors = [
            _line("I LOST", (100, 100, 120, 30)),
            _line("CONTROL", (100, 140, 140, 30)),
        ]
        candidates = [_line("I LOST CONTROL", (100, 100, 260, 72))]
        _decision, lines, _reason = reconcile_recovery_lines(predecessors, candidates)
        self.assertEqual(_ordered_text(lines), "I LOST CONTROL")


if __name__ == "__main__":
    unittest.main()
