"""Contract: an OCR recovery may not silently discard predecessor evidence.

A region re-read (``rapidocr_region_recovery``) used to replace the lines it was
derived from unconditionally, on the strength of the new read's quality score
alone.  A narrower second read therefore erased both the lexical tail of the
line it superseded and the geometry that tail lived in, and nothing downstream
could tell the difference.

Every engine here is a stub.  No RapidOCR/Paddle inference, no network, no
chapter: the fixtures are synthetic tokens chosen only to exercise the policy.
"""

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest
from unittest.mock import patch

import numpy as np

import config
import ocr_balloon
from ocr_balloon import (
    RECOVERY_RECONCILE,
    RECOVERY_REJECT,
    RECOVERY_REPLACE,
    TextGroup,
    apply_rapidocr_region_recovery,
    reconcile_recovery_lines,
    score_group_ocr_quality,
)
from ocr_engine import OCRLine
from source_completeness import compact, meaningful_tokens


def _line(text, box, confidence=0.92):
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
        page=1,
    )


def _decide(predecessors, candidates):
    return reconcile_recovery_lines(predecessors, candidates)


def _text_of(lines):
    return " ".join(line.text for line in lines)


def _union(lines):
    x0 = min(line.box[0] for line in lines)
    y0 = min(line.box[1] for line in lines)
    x1 = max(line.box[0] + line.box[2] for line in lines)
    y1 = max(line.box[1] + line.box[3] for line in lines)
    return (x0, y0, x1 - x0, y1 - y0)


class SafeReplacementTests(unittest.TestCase):
    def test_spacing_repair_replaces_without_duplicating(self):
        predecessor = [_line("THAT I LOSTCONTROL", (260, 2075, 376, 33), 0.96)]
        candidate = [_line("THAT I LOST CONTROL", (260, 2075, 372, 32), 0.99)]
        decision, lines, _reason = _decide(predecessor, candidate)
        self.assertEqual(decision, RECOVERY_REPLACE)
        self.assertEqual(compact(_text_of(lines)), "THATILOSTCONTROL")

    def test_case_only_difference_is_not_loss(self):
        predecessor = [_line("KillIng", (10, 10, 120, 30))]
        candidate = [_line("killing", (10, 10, 118, 30))]
        decision, lines, _reason = _decide(predecessor, candidate)
        self.assertEqual(decision, RECOVERY_REPLACE)
        self.assertEqual(compact(_text_of(lines)), "KILLING")

    def test_punctuation_only_difference_is_not_loss(self):
        predecessor = [_line("NO...", (10, 10, 90, 30))]
        candidate = [_line("NO", (10, 10, 60, 30))]
        decision, _lines, _reason = _decide(predecessor, candidate)
        self.assertEqual(decision, RECOVERY_REPLACE)

    def test_recovery_that_expands_the_read_replaces(self):
        predecessor = [_line("I LOST", (260, 2075, 180, 33))]
        candidate = [_line("I LOST CONTROL", (260, 2075, 376, 33), 0.99)]
        decision, lines, _reason = _decide(predecessor, candidate)
        self.assertEqual(decision, RECOVERY_REPLACE)
        self.assertIn("CONTROL", compact(_text_of(lines)))

    def test_split_into_two_lines_keeps_every_token(self):
        predecessor = [_line("KILLEDEVERYONE...", (288, 2165, 319, 32))]
        candidate = [
            _line("KILLED", (288, 2166, 110, 30)),
            _line("EVERYONE...", (404, 2162, 202, 38)),
        ]
        decision, lines, _reason = _decide(predecessor, candidate)
        self.assertEqual(decision, RECOVERY_REPLACE)
        self.assertEqual(compact(_text_of(lines)), "KILLEDEVERYONE")


class LexicalLossTests(unittest.TestCase):
    def test_lost_suffix_is_reconciled_not_replaced(self):
        predecessor = [_line("THAT I LOSTCONTROL", (260, 2075, 376, 33), 0.9676)]
        candidate = [_line("THAT I LOST", (260, 2075, 212, 32), 0.9778)]
        decision, lines, reason = _decide(predecessor, candidate)
        self.assertEqual(decision, RECOVERY_RECONCILE)
        self.assertIn("CONTROL", meaningful_tokens(_text_of(lines)))
        self.assertEqual(compact(_text_of(lines)), "THATILOSTCONTROL")
        self.assertTrue(reason)

    def test_reconciled_geometry_covers_the_predecessor_source(self):
        predecessor = [_line("THAT I LOSTCONTROL", (260, 2075, 376, 33))]
        candidate = [_line("THAT I LOST", (260, 2075, 212, 32))]
        _decision, lines, _reason = _decide(predecessor, candidate)
        x, y, width, height = _union(lines)
        self.assertLessEqual(x, 260)
        self.assertGreaterEqual(x + width, 260 + 376)
        self.assertLessEqual(y, 2075)
        self.assertGreaterEqual(y + height, 2075 + 33)

    def test_lost_prefix_is_reconciled(self):
        predecessor = [_line("I REALLY LOST CONTROL", (100, 10, 400, 30))]
        candidate = [_line("LOST CONTROL", (240, 10, 260, 30), 0.99)]
        decision, lines, _reason = _decide(predecessor, candidate)
        self.assertEqual(decision, RECOVERY_RECONCILE)
        self.assertEqual(compact(_text_of(lines)), "IREALLYLOSTCONTROL")
        self.assertLessEqual(_union(lines)[0], 100)

    def test_reconciliation_never_duplicates_the_shared_overlap(self):
        predecessor = [_line("THAT I LOSTCONTROL", (260, 2075, 376, 33))]
        candidate = [_line("THAT I LOST", (260, 2075, 212, 32))]
        _decision, lines, _reason = _decide(predecessor, candidate)
        tokens = [token for token in compact(_text_of(lines))]
        self.assertEqual("".join(tokens).count("THATILOST"), 1)
        self.assertEqual(len(lines), 1)

    def test_higher_confidence_does_not_authorize_dropping_a_token(self):
        predecessor = [_line("THAT I LOST CONTROL", (260, 2075, 376, 33), 0.96)]
        candidate = [_line("THAT I LOST", (260, 2075, 212, 32), 0.999)]
        decision, lines, _reason = _decide(predecessor, candidate)
        self.assertNotEqual(decision, RECOVERY_REPLACE)
        self.assertIn("CONTROL", meaningful_tokens(_text_of(lines)))


class ConflictTests(unittest.TestCase):
    def test_disagreeing_reads_fail_closed_instead_of_merging(self):
        predecessor = [_line("I WILL KILL HIM", (10, 10, 300, 30), 0.94)]
        candidate = [_line("I WILL HELP HIM", (10, 10, 300, 30), 0.99)]
        decision, lines, reason = _decide(predecessor, candidate)
        self.assertEqual(decision, RECOVERY_REJECT)
        self.assertNotIn("HELP", compact(_text_of(lines)))
        self.assertIn("KILL", compact(_text_of(lines)))
        self.assertTrue(reason)

    def test_a_predecessor_line_the_retry_never_saw_is_retained(self):
        predecessor = [
            _line("I WILL GO NOW", (10, 10, 240, 30)),
            _line("AND NEVER RETURN", (10, 60, 260, 30)),
        ]
        candidate = [_line("I WILL GO NOW", (10, 10, 238, 30), 0.99)]
        decision, lines, _reason = _decide(predecessor, candidate)
        self.assertNotEqual(decision, RECOVERY_REPLACE)
        self.assertIn("NEVER", compact(_text_of(lines)))
        self.assertIn("RETURN", compact(_text_of(lines)))


class GarbageRemovalTests(unittest.TestCase):
    def test_noise_suffix_removal_is_allowed_but_must_be_explicit(self):
        predecessor = [_line("I WILL GO XQZ@@@", (10, 10, 320, 30))]
        candidate = [_line("I WILL GO", (10, 10, 200, 30), 0.99)]
        decision, lines, reason = _decide(predecessor, candidate)
        self.assertEqual(decision, RECOVERY_REPLACE)
        self.assertNotIn("XQZ", compact(_text_of(lines)))
        self.assertIn("noise", reason)
        self.assertEqual(
            predecessor[0].metadata.get("ocr_discard_reason"),
            "recovery_noise_removed",
        )

    def test_reconciliation_does_not_fabricate_words_from_unintelligible_source(self):
        predecessor = [_line("iHon @#~ \\|/", (10, 10, 200, 30), 0.5)]
        candidate = [_line("IHON", (10, 10, 120, 30), 0.99)]
        decision, lines, _reason = _decide(predecessor, candidate)
        self.assertIn(decision, {RECOVERY_REPLACE, RECOVERY_REJECT})
        self.assertNotIn("HON@", compact(_text_of(lines)))


class RecoveryPipelineTests(unittest.TestCase):
    """The policy has to hold through ``apply_rapidocr_region_recovery`` itself."""

    def _group(self, lines, text, group_id="p002:BALAO_3"):
        group = TextGroup(
            group_id=group_id,
            lines=list(lines),
            text=text,
            classification="speech",
            inside_balloon_like_region=True,
            source_engine="rapidocr",
        )
        group.quality_score, group.quality_reasons = score_group_ocr_quality(group)
        return group

    def _run(self, raw_lines, groups, crop_lines):
        class _Stub:
            def __call__(self, lang, engine=None, fallback_engine=None):
                return self

            def detect_lines(self, crop, page=None, **kwargs):
                return [
                    OCRLine(
                        text=line.text,
                        confidence=line.confidence,
                        polygon=line.polygon.copy(),
                        box=line.box,
                        raw_text=line.raw_text,
                        engine="rapidocr",
                        page=page,
                    )
                    for line in crop_lines
                ]

        image = np.zeros((2400, 900, 3), dtype=np.uint8)
        image[:] = 255
        with patch.object(config, "OCR_ENGINE", "rapidocr"), patch.object(
            config, "RAPIDOCR_REGION_RECOVERY", True
        ), patch.object(ocr_balloon, "OCREngine", _Stub()), patch.object(
            # The stub already reports page coordinates, so the crop offset that
            # a real region read needs would only move them off the predecessor.
            ocr_balloon,
            "_offset_line",
            lambda line, dx, dy, engine, group_id: line,
        ), patch.object(
            ocr_balloon,
            "_best_candidate_group",
            lambda groups, box: groups[0] if groups else None,
        ):
            return apply_rapidocr_region_recovery(image, raw_lines, groups, "eng", 2)

    def _full7_shape(self):
        raw = [
            _line("IT'SNOT JUST", (325, 228, 244, 37), 0.9636),
            _line("THAT I LOSTCONTROL", (260, 275, 376, 33), 0.9676),
            _line("AGAINANDALMOST", (282, 321, 333, 33), 0.9931),
            _line("KILLEDEVERYONE...", (288, 365, 319, 32), 0.984),
        ]
        crop = [
            _line("IT'S NOT JUST", (328, 230, 240, 34), 0.9722),
            _line("THAT I LOST", (260, 275, 212, 32), 0.9778),
            _line("AGAIN AND ALMOST", (282, 320, 332, 32), 0.9977),
            _line("KILLED", (288, 366, 110, 30), 0.9967),
            _line("EVERYONE...", (404, 362, 202, 38), 0.9924),
        ]
        return raw, crop

    def test_truncating_retry_does_not_erase_the_tail_from_the_line_list(self):
        raw, crop = self._full7_shape()
        text = " ".join(line.text for line in raw)
        lines, records = self._run(list(raw), [self._group(raw, text)], crop)
        self.assertTrue(records)
        self.assertEqual(records[0]["decision"], "reconcile")
        joined = compact(" ".join(line.text for line in lines))
        self.assertIn("CONTROL", joined)
        # The spacing repair the retry was run for is kept, and kept once.
        self.assertEqual(joined.count("THATILOSTCONTROL"), 1)

    def test_reconciled_geometry_reaches_the_predecessor_tail(self):
        raw, crop = self._full7_shape()
        text = " ".join(line.text for line in raw)
        lines, _records = self._run(list(raw), [self._group(raw, text)], crop)
        owner = [line for line in lines if "CONTROL" in line.text.upper()]
        self.assertTrue(owner)
        x, _y, width, _height = owner[0].box
        self.assertGreaterEqual(x + width, 260 + 376)


if __name__ == "__main__":
    unittest.main()
