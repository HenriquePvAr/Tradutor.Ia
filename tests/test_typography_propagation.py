"""Mission 7: real renderer typography propagates into the editable PSD regions.

Hermetic: exercises benchmark_pipeline._editable_psd_regions (the adapter that turns a page's
render items into EditableTextRegion) with synthetic debug items. The renderer is the source of
truth; bbox/black/semantic defaults are fallbacks, and every property records its source."""
import unittest

import benchmark_pipeline as bp


def _state(items):
    return {"index": 1, "debug_data": {"items": items}}


def _one(item):
    regions = bp._editable_psd_regions(_state([item]))
    assert len(regions) == 1, regions
    return regions[0]


BASE = {
    "region_id": "grp_1", "classification": "speech", "translation": "OLA",
    "redrawn": True, "translation_final_state": "translated",
    "draw_box": [100, 120, 300, 80],
}


class TypographyPropagationTests(unittest.TestCase):
    # §25 renderer font size is used exactly (not bbox-derived)
    def test_font_size_from_renderer(self):
        r = _one({**BASE, "font_size": 26})
        self.assertEqual(r.font_size, 26)
        self.assertEqual(r.sources["font_size_source"], "RENDERER")

    # §26 no renderer font size -> bbox fallback, no crash
    def test_font_size_bbox_fallback(self):
        r = _one({**BASE, "font_size": 0})
        self.assertEqual(r.sources["font_size_source"], "BBOX_FALLBACK")
        self.assertGreaterEqual(r.font_size, int(bp.config.MIN_FONT_SIZE))

    # §27 renderer wrapped lines are preserved verbatim
    def test_multiline_from_renderer(self):
        r = _one({**BASE, "translation": "VOCE CONSEGUE ME VER",
                  "rendered_lines": ["VOCÊ CONSEGUE", "ME VER?"], "font_size": 24})
        self.assertEqual(r.text, "VOCÊ CONSEGUE\nME VER?")
        self.assertEqual(r.sources["wrap_source"], "RENDERER")

    def test_multiline_raw_fallback(self):
        r = _one({**BASE, "translation": "UMA LINHA", "font_size": 24})
        self.assertEqual(r.text, "UMA LINHA")
        self.assertEqual(r.sources["wrap_source"], "RAW_TEXT_FALLBACK")

    # §28 alignment: speech -> center, narration -> left (semantic default, reported honestly)
    def test_alignment_semantic_default(self):
        self.assertEqual(_one({**BASE, "classification": "speech", "font_size": 24}).alignment, "center")
        r = _one({**BASE, "classification": "narration", "font_size": 24})
        self.assertEqual(r.alignment, "left")
        self.assertEqual(r.sources["alignment_source"], "SEMANTIC_DEFAULT")

    # §29 colour maps from the renderer colour name; unknown -> black default
    def test_color_from_renderer(self):
        r = _one({**BASE, "text_color": "white", "font_size": 24})
        self.assertEqual(r.color, (245, 245, 245))
        self.assertEqual(r.sources["color_source"], "RENDERER")

    def test_color_black_default(self):
        r = _one({**BASE, "text_color": "", "font_size": 24})
        self.assertEqual(r.color, (0, 0, 0))
        self.assertEqual(r.sources["color_source"], "BLACK_DEFAULT")

    # §30 refined position (translation_box) wins over the raw bounding_box
    def test_position_from_renderer_refined_box(self):
        r = _one({"region_id": "g", "classification": "speech", "translation": "X",
                  "redrawn": True, "translation_final_state": "translated",
                  "bounding_box": [0, 0, 50, 50], "translation_box": [200, 210, 120, 60],
                  "font_size": 24})
        self.assertEqual(tuple(r.bbox), (200, 210, 120, 60))
        self.assertEqual(r.sources["position_source"], "RENDERER")

    def test_position_bbox_fallback(self):
        r = _one({"region_id": "g", "classification": "speech", "translation": "X",
                  "redrawn": True, "translation_final_state": "translated",
                  "bounding_box": [5, 6, 70, 40], "font_size": 24})
        self.assertEqual(tuple(r.bbox), (5, 6, 70, 40))
        self.assertEqual(r.sources["position_source"], "BBOX_FALLBACK")

    # §14 source-fallback region kept for QC (untranslated but has source text)
    def test_source_fallback_region_kept(self):
        r = _one({"region_id": "g", "classification": "speech", "translation": "",
                  "clean_text": "HELLO", "preserved_original": True,
                  "bounding_box": [0, 0, 100, 40]})
        self.assertEqual(r.translation_status, "source_fallback")
        self.assertEqual(r.text, "HELLO")
        self.assertTrue(r.review_required)

    # §32 legacy item with no typography metadata still yields a region via fallbacks
    def test_legacy_item_all_fallbacks(self):
        r = _one({"region_id": "g", "classification": "narration", "translation": "OI",
                  "redrawn": True, "translation_final_state": "translated",
                  "bounding_box": [10, 10, 200, 60]})
        self.assertEqual(r.sources, {
            "font_size_source": "BBOX_FALLBACK", "position_source": "BBOX_FALLBACK",
            "wrap_source": "RAW_TEXT_FALLBACK", "color_source": "BLACK_DEFAULT",
            "alignment_source": "SEMANTIC_DEFAULT"})

    # region with neither translation nor source text -> no layer invented
    def test_empty_region_skipped(self):
        self.assertEqual(bp._editable_psd_regions(_state([
            {"region_id": "g", "classification": "sfx", "bounding_box": [0, 0, 30, 30]}])), [])


if __name__ == "__main__":
    unittest.main()
