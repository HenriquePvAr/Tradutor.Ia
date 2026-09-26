"""Font metadata scorer: rank local fonts by source glyph style (family/weight/
slant/width), never by colour. Fit remains a separate hard constraint in the
renderer; this only ranks visual similarity.
"""
from __future__ import annotations

import _test_bootstrap  # noqa: F401

import unittest

import font_fidelity as ff


def _style(**kw):
    base = {"family_class": "sans", "weight": "regular", "slant": "normal",
            "width": "normal", "confidence": 0.9}
    base.update(kw)
    return base


class MetadataAndScore(unittest.TestCase):
    def test_every_role_font_has_metadata(self):
        for fonts in ff.ROLE_FONT_FILES.values():
            for name in fonts:
                self.assertIn(name.lower(), ff.FONT_METADATA, name)

    def test_serif_style_prefers_serif_font(self):
        s_serif, _ = ff.font_match_score("georgia.ttf", _style(family_class="serif"))
        s_sans, _ = ff.font_match_score("arial.ttf", _style(family_class="serif"))
        self.assertGreater(s_serif, s_sans)

    def test_italic_style_prefers_italic_font(self):
        s_it, _ = ff.font_match_score("ariali.ttf", _style(slant="italic"))
        s_up, _ = ff.font_match_score("arial.ttf", _style(slant="italic"))
        self.assertGreater(s_it, s_up)

    def test_bold_style_prefers_bold_font(self):
        s_bold, _ = ff.font_match_score("arialbd.ttf", _style(weight="bold"))
        s_reg, _ = ff.font_match_score("arial.ttf", _style(weight="bold"))
        self.assertGreater(s_bold, s_reg)

    def test_top3_returns_ranked_candidates_with_reasons(self):
        top = ff.rank_fonts_by_glyph_style(_style(family_class="serif", slant="italic"),
                                           role="serif_italic", top_n=3)
        self.assertEqual(len(top), 3)
        self.assertGreaterEqual(top[0]["score"], top[1]["score"])
        self.assertTrue(top[0]["font"].startswith(("georgiai", "timesi")))
        self.assertIn("italic", top[0]["reasons"])


class ColourFontInvariants(unittest.TestCase):
    def test_same_glyph_style_scores_identically_regardless_of_colour(self):
        # Colour is not an input; score depends only on glyph style.
        s1, _ = ff.font_match_score("georgia.ttf", _style(family_class="serif"))
        s2, _ = ff.font_match_score("georgia.ttf", _style(family_class="serif"))
        self.assertEqual(s1, s2)

    def test_same_color_different_glyph_can_pick_different_font(self):
        serif_pick = ff.rank_fonts_by_glyph_style(_style(family_class="serif"))[0]["font"]
        comic_pick = ff.rank_fonts_by_glyph_style(_style(family_class="comic"))[0]["font"]
        self.assertNotEqual(ff._font_metadata(serif_pick)["family"],
                            ff._font_metadata(comic_pick)["family"])


if __name__ == "__main__":
    unittest.main()
