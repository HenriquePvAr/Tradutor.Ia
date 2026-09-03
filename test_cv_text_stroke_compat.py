"""Regression: synthetic text fixtures need ``thickness`` to mean stroke width.

OpenCV 4 stroked Hershey glyphs ``thickness`` pixels wide at every fontScale.
OpenCV 5 rewrote the renderer: the stroke follows fontScale instead and
``thickness`` saturates at 2, so a heavy outline drawn under a lighter body is
covered exactly and disappears.  ``test_support.cv_text.putText`` restores the
old contract, and the fixtures across this suite depend on it.

These tests pin our contract -- the stroke width we asked for, connected
glyphs, outline survival -- not any particular OpenCV rendering.
"""

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest

import cv2
import numpy as np

from test_support import cv_text


def _stroke_width(mask):
    """Most common horizontal run length: the width of the glyph stems."""
    runs = []
    for row in mask > 127:
        edges = np.flatnonzero(
            np.diff(np.concatenate(([0], row.view(np.int8), [0]))))
        runs.extend((edges[1::2] - edges[0::2]).tolist())
    return int(np.bincount(runs).argmax())


def _draw(text="STORY TEXT", scale=1.0, thickness=1, shape=(400, 1200)):
    canvas = np.zeros(shape, dtype=np.uint8)
    cv_text.putText(canvas, text, (40, 300), cv2.FONT_HERSHEY_SIMPLEX, scale,
                    255, thickness, cv2.LINE_AA)
    return canvas


class StrokeWidthContract(unittest.TestCase):
    def test_thickness_is_the_stroke_width_at_every_font_scale(self):
        # The fixtures span roughly this range of sizes and weights.  Each
        # stroke has to stay narrow enough not to merge neighbouring strokes
        # into a blob, which has no measurable stem width -- OpenCV 4 did the
        # same thing to a thickness of 20 at fontScale 0.6.
        cases = [(0.6, 2), (0.6, 3),
                 (0.95, 2), (0.95, 3), (0.95, 7),
                 (2.6, 2), (2.6, 3), (2.6, 7), (2.6, 20)]
        for scale, thickness in cases:
            with self.subTest(scale=scale, thickness=thickness):
                width = _stroke_width(_draw(scale=scale, thickness=thickness))
                # OpenCV 4 rounded a polyline stroke to thickness + 1.
                self.assertAlmostEqual(width, thickness + 1, delta=1)

    def test_thickness_still_separates_weights_above_two(self):
        # The regression itself: OpenCV 5 renders every thickness >= 2 alike.
        widths = [_stroke_width(_draw(scale=2.6, thickness=t))
                  for t in (2, 5, 10, 20)]
        self.assertEqual(widths, sorted(widths))
        self.assertGreater(widths[-1] - widths[0], 10)

    def test_a_thick_outline_survives_under_a_thinner_body(self):
        # The P5/P6 lettering fixtures draw a white outline, then a dark body
        # over it, and assert on the light halo that is left around the glyphs.
        canvas = np.full((400, 1200, 3), 150, dtype=np.uint8)
        cv_text.putText(canvas, "STORY", (40, 300), cv2.FONT_HERSHEY_SIMPLEX,
                        2.6, (255, 255, 255), 20, cv2.LINE_AA)
        cv_text.putText(canvas, "STORY", (40, 300), cv2.FONT_HERSHEY_SIMPLEX,
                        2.6, (20, 20, 20), 7, cv2.LINE_AA)
        grey = cv2.cvtColor(canvas, cv2.COLOR_BGR2GRAY)
        self.assertGreater(int(np.count_nonzero(grey >= 245)), 500)
        self.assertGreater(int(np.count_nonzero(grey <= 60)), 500)

    def test_glyphs_stay_connected(self):
        # A skeleton that breaks up turns a glyph-scoped cleanup mask into a
        # region-sized one, so each letter has to stay a single component.
        drawn = _draw("HUGE", scale=9.0, thickness=2, shape=(900, 900))
        count, _, _, _ = cv2.connectedComponentsWithStats(
            (drawn > 127).astype(np.uint8), 8)
        self.assertEqual(count - 1, 4)

    def test_draws_on_grey_and_colour_canvases(self):
        grey = np.zeros((200, 600), dtype=np.uint8)
        cv_text.putText(grey, "HI", (20, 150), cv2.FONT_HERSHEY_SIMPLEX, 2.0,
                        255, 5, cv2.LINE_AA)
        self.assertTrue(np.any(grey > 127))

        colour = np.zeros((200, 600, 3), dtype=np.uint8)
        cv_text.putText(colour, "HI", (20, 150), cv2.FONT_HERSHEY_SIMPLEX, 2.0,
                        (40, 90, 200), 5, cv2.LINE_AA)
        painted = colour[colour.any(axis=2)]
        self.assertTrue(np.any(painted))
        # The requested colour is what lands on the canvas, per channel.
        self.assertEqual(tuple(colour.reshape(-1, 3).max(axis=0)),
                         (40, 90, 200))


if __name__ == "__main__":
    unittest.main()
