"""Regression: cv2.HoughLinesP may return (N, 1, 4) or (N, 4).

OpenCV <5 returned an extra singleton axis per segment; OpenCV 5 dropped it.
Both encode the same geometry, so the pipeline must read them identically.
These tests are version-agnostic: the real HoughLinesP is patched out so both
representations are exercised on every OpenCV build.
"""

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest
from unittest import mock

import numpy as np

import ocr_balloon
from ocr_balloon import OCRLine, TextGroup, _classify_background_region


# One horizontal segment plus one ~45 deg diagonal, both far longer than any
# minimum-length threshold the classifier can derive from the region size.
SEGMENTS = [
    (5, 5, 300, 5),
    (5, 5, 300, 300),
]


def _region():
    image = np.full((240, 320, 3), 255, dtype=np.uint8)
    line = OCRLine(
        text="TEXT",
        confidence=0.95,
        polygon=np.array([[60, 80], [250, 80], [250, 140], [60, 140]]),
        box=(60, 80, 190, 60),
        raw_text="TEXT",
        engine="rapidocr",
    )
    group = TextGroup(
        group_id="T",
        lines=[line],
        text="TEXT",
        classification="speech",
        inside_balloon_like_region=True,
    )
    return image, group


def _line_metrics(hough_return):
    image, group = _region()
    with mock.patch.object(
        ocr_balloon.cv2, "HoughLinesP", return_value=hough_return
    ):
        _, metrics = _classify_background_region(image, group)
    return metrics["long_line_count"], metrics["diagonal_line_count"]


class HoughLinesShapeCompatTest(unittest.TestCase):
    def test_case_a_legacy_n_1_4_shape(self):
        legacy = np.array([[seg] for seg in SEGMENTS], dtype=np.int32)
        self.assertEqual(legacy.shape, (2, 1, 4))
        self.assertEqual(_line_metrics(legacy), (2, 1))

    def test_case_b_flat_n_4_shape_matches_legacy(self):
        legacy = np.array([[seg] for seg in SEGMENTS], dtype=np.int32)
        flat = np.array(SEGMENTS, dtype=np.int32)
        self.assertEqual(flat.shape, (2, 4))
        self.assertEqual(_line_metrics(flat), _line_metrics(legacy))

    def test_case_c_none_is_fail_safe(self):
        self.assertEqual(_line_metrics(None), (0, 0))

    def test_case_d_single_segment_both_shapes(self):
        legacy = np.array([[SEGMENTS[1]]], dtype=np.int32)
        flat = np.array([SEGMENTS[1]], dtype=np.int32)
        self.assertEqual(legacy.shape, (1, 1, 4))
        self.assertEqual(flat.shape, (1, 4))
        self.assertEqual(_line_metrics(flat), _line_metrics(legacy))
        self.assertEqual(_line_metrics(flat), (1, 1))

    def test_empty_result_is_fail_safe(self):
        self.assertEqual(_line_metrics(np.empty((0, 4), dtype=np.int32)), (0, 0))
        self.assertEqual(_line_metrics(np.empty((0, 1, 4), dtype=np.int32)), (0, 0))

    def test_incompatible_cardinality_is_not_reinterpreted(self):
        """A payload that is not a multiple of 4 must not be reshaped into
        fabricated coordinates; it degrades to 'no lines detected'."""
        self.assertEqual(_line_metrics(np.array([[1, 2, 3]], dtype=np.int32)), (0, 0))


if __name__ == "__main__":
    unittest.main()
