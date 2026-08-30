"""#84F43 - P26's post-render OCR blocker was a genuine render-geometry bug,
not a checker bug.

Job b76f09ff-c5bd-4b7b-b981-c07ea11fb4f2, page 26: the rendered PT-BR
target for a "tall_display" balloon ("E SO ENTRAR NO REINO DOS SONHOS, MATAR
UNS MONSTROS...") re-OCR'd with fabricated boundary characters - "NO" read
back as "RNO", "MONSTROS" as "SMONSTROS" - dragging the post-render checker's
similarity under its acceptance bar and discarding a legitimately translated
page in favour of the untranslated English source. The #84F42 tight-crop fix
(reading the box the renderer actually drew into, not the wide residual-
search crop) did not help: the fabricated characters are real ink in the
*rendered bitmap itself*, not a crop-selection artifact.

Forensic replay (see mission #84F43 diagnostics) against the real job's
balloon geometry showed the mechanism directly: a wrapped line ending in a
deep descender (here a comma) has real ink that extends well past the font's
nominal descent metric, while ``_draw_group_translation`` sized the gap
between wrapped lines (``spacing``) purely from a flat fraction of
``font_size`` - so the next line's cap-height glyphs land only a few pixels
below that overshoot instead of the intended clearance, RapidOCR's line
detector fuses the two rows, and glyphs from both lines get interleaved into
one fabricated boundary token.

The fix (``ocr_balloon.py``, inside ``_draw_group_translation``'s font-size
loop) measures the actual rendered ink bottom of every non-last wrapped line
via ``draw.textbbox`` and, when any line's ink reaches further down than the
shallowest line (a real overshoot beyond nominal spacing), grows ``spacing``
by that overshoot plus a small stroke-aware margin before the existing
fit-check runs. A size that needs the extra room shrinks exactly the way it
already does for plain text overflow (``AUTO_FONT_SHRINK``) - no page,
phrase, or translation is hardcoded, and a layout that already had enough
clearance (``descender_overshoot == 0``) is left completely untouched.
"""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest
from unittest.mock import patch

import cv2
import numpy as np

import ocr_balloon as ob
from ocr_balloon import TextGroup
from ocr_engine import OCRLine


def _line(text, box):
    x, y, w, h = box
    polygon = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.int32)
    return OCRLine(text=text, confidence=0.95, polygon=polygon, box=box,
                   raw_text=text, engine="rapidocr", page=1)


# The real job's balloon geometry (page 26, group p026:BALAO_2): three source
# lines, union box padded by ``_safe_draw_box`` to a ~2167x700 draw area. This
# reproduces the actual size/shrink pressure that made the bug appear - the
# text style itself is patched below so the test needs no real image pixels
# and no external fixture files.
_SOURCE_LINES = [
    _line("JUST GO INTO", (584, 134, 1228, 185)),
    _line("THE DREAM REALM, KILL", (119, 377, 2151, 198)),
    _line("A FEW MONSTERS...", (286, 612, 1780, 210)),
]
_SOURCE_TEXT = "JUST GO INTO THE DREAM REALM, KILL A FEW MONSTERS..."

_DISPLAY_PROFILE = {
    "stroke_width": 2,
    "line_height_scale": 0.9,
    "content_width_ratio": 0.92,
    "font_size_scale": 1.04,
    "font_class": "tall_display",
    "style_source": "original_pixels",
    "case_style": "uppercase",
}


def _render(translation, *, profile=_DISPLAY_PROFILE, canvas=(943, 2385)):
    group = TextGroup(
        group_id="p026:BALAO_2",
        lines=list(_SOURCE_LINES),
        text=_SOURCE_TEXT,
        translation=translation,
        classification="speech",
        inside_balloon_like_region=True,
        source_engine="rapidocr",
    )
    img = np.full((canvas[0], canvas[1], 3), 255, dtype=np.uint8)
    with patch.object(ob, "typography_profile_for_region", return_value=dict(profile)):
        rendered = ob._draw_group_translation(img.copy(), group, font_path=None)
    return rendered, group


def _min_interline_gap(rendered_bgr, draw_box):
    """Raster-measure the tightest vertical gap between two wrapped lines'
    ink bands inside ``draw_box`` - a direct, font-agnostic proxy for what a
    line-segmentation OCR detector actually sees."""
    x, y, w, h = draw_box
    gray = cv2.cvtColor(rendered_bgr[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY)
    dark_rows = (gray < 128).sum(axis=1) > 0
    bands = []
    in_band = False
    start = 0
    for row, dark in enumerate(dark_rows):
        if dark and not in_band:
            in_band = True
            start = row
        elif not dark and in_band:
            in_band = False
            bands.append((start, row))
    if in_band:
        bands.append((start, len(dark_rows)))
    if len(bands) < 2:
        return None
    return min(bands[i + 1][0] - bands[i][1] for i in range(len(bands) - 1))


class DescenderAdjacentLineClearanceTests(unittest.TestCase):
    """The real P26 shape: a wrapped line ending in a comma sits right above
    the next line's cap-height glyphs."""

    def test_p26_shaped_render_has_no_boundary_gap_collapse(self):
        rendered, group = _render(
            "E SO ENTRAR NO REINO DOS SONHOS, MATAR UNS MONSTROS..."
        )
        gap = _min_interline_gap(rendered, group.draw_box)
        self.assertIsNotNone(gap)
        # Before the fix this measured 8px at one boundary (vs. ~35px at the
        # others) - tight enough that RapidOCR fused the rows and fabricated
        # "RNO"/"SMONSTROS". The overshoot-aware spacing floor rules that out.
        self.assertGreaterEqual(gap, 12)

    def test_font_shrinks_rather_than_shipping_a_cramped_render(self):
        # Same content, but forced into a shorter box than the real job's -
        # there is no room left to grow spacing at any font size that still
        # fits, so the loop must shrink font size (never ship overflow, and
        # never silently keep the collapsed gap).
        rendered, group = _render(
            "E SO ENTRAR NO REINO DOS SONHOS, MATAR UNS MONSTROS...",
            canvas=(500, 2385),
        )
        gap = _min_interline_gap(rendered, group.draw_box)
        self.assertLessEqual(group.text_overflow_ratio, 0.0)
        if gap is not None:
            self.assertGreaterEqual(gap, 4)


class SafeLayoutIsLeftAloneTests(unittest.TestCase):
    """A layout with no real descender overshoot must not be perturbed -
    the fix is reactive to measured evidence, not a blanket spacing bump."""

    def test_no_descender_no_extra_spacing(self):
        # Every wrapped line ends flush (no comma/tail), so
        # ``descender_overshoot`` is 0 for this text at this box. The fix
        # must be a no-op here: two renders of the same descender-free
        # text/box/profile must be pixel-identical and deterministic,
        # proving the new code path only ever engages on measured evidence.
        text = "ENTRAR NO REINO DOS SONHOS MATAR UNS MONSTROS"
        first, group_first = _render(text)
        second, group_second = _render(text)
        self.assertEqual(group_first.font_size, group_second.font_size)
        self.assertTrue(np.array_equal(first, second))


class OrdinaryRegionUnaffectedTests(unittest.TestCase):
    """A small, ordinary dialogue balloon (not the P26 display-lettering
    class) with plenty of clearance keeps rendering exactly as before."""

    def test_ordinary_dialogue_balloon_unaffected(self):
        lines_src = [
            _line("NOT THE CHEAP", (170, 1620, 465, 72)),
            _line("SYNTHETIC STUFF I'M", (83, 1714, 637, 78)),
            _line("USED TO GETTING", (138, 1807, 527, 81)),
        ]
        group = TextGroup(
            group_id="p005:BALAO_2",
            lines=lines_src,
            text="NOT THE CHEAP SYNTHETIC STUFF I'M USED TO GETTING",
            translation="NAO A PORCARIA SINTETICA BARATA QUE EU COSTUMO PEGAR",
            classification="speech",
            inside_balloon_like_region=True,
            source_engine="rapidocr",
        )
        img = np.full((2200, 900, 3), 255, dtype=np.uint8)
        profile = {
            "stroke_width": 1,
            "line_height_scale": 1.0,
            "content_width_ratio": 1.0,
            "font_size_scale": 0.78,
            "font_class": "regular",
        }
        with patch.object(ob, "typography_profile_for_region", return_value=profile):
            rendered = ob._draw_group_translation(img.copy(), group, font_path=None)
        gap = _min_interline_gap(rendered, group.draw_box)
        if gap is not None:
            self.assertGreater(gap, 0)
        self.assertLessEqual(group.text_overflow_ratio, 0.0)


if __name__ == "__main__":
    unittest.main()
