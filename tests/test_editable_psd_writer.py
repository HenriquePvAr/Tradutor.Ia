"""Mission 6: editable Professional PSD (v2) writer -- native Type Layers.

Hermetic: builds real PSDs from synthetic regions/images and re-parses them. No pipeline,
network, OCR, DeepL. Covers the writer contract in §43 (A-T, X)."""
import hashlib
import tempfile
import unittest
from pathlib import Path

import numpy as np
from psd_tools import PSDImage
from psd_tools.constants import Resource

import yomu.runtime.artifact_contract as ac
import yomu.export.editable_psd_writer as ew
from yomu.export.editable_psd_writer import EditableTextRegion as R, EditablePsdError


def _img(w, h, v=200):
    return np.full((h, w, 3), v, np.uint8)


def _groups(psd):
    return {l.name: l for l in psd if l.is_group()}


def _type_layers(psd):
    g = _groups(psd)["TEXT"]
    return {l.name: l for l in g if l.kind == "type"}


class EditablePsdWriterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, regions, w=800, h=1000, page=1, **kw):
        dest = ac.artifact_path(self.root, ac.ArtifactType.EDITABLE_PSD, page=page, total_pages=208)
        ew.write_editable_psd(dest, _img(w, h, 200), _img(w, h, 235), regions, (w, h), **kw)
        return dest

    # A) 1 page, 3 translated regions -> 3 native Type Layers
    def test_three_regions_three_type_layers(self):
        regions = [
            R("a", "speech", "OLA", (50, 50, 200, 60), font_size=24),
            R("b", "speech", "MUNDO", (300, 300, 200, 60), font_size=24),
            R("c", "narration", "FIM", (50, 800, 200, 60), font_size=24),
        ]
        psd = PSDImage.open(self._write(regions))
        tl = _type_layers(psd)
        self.assertEqual(set(tl), {"BALAO_001", "BALAO_002", "NARRACAO_001"})
        self.assertTrue(all(l.kind == "type" for l in tl.values()))

    # B) Unicode PT-BR
    def test_unicode_ptbr(self):
        regions = [R("a", "speech", "Á À Â Ã É Ê Í Ó Ô Õ Ú Ç ç", (40, 40, 700, 60), font_size=26)]
        psd = PSDImage.open(self._write(regions))
        self.assertEqual(_type_layers(psd)["BALAO_001"].text, "Á À Â Ã É Ê Í Ó Ô Õ Ú Ç ç")

    # C) multiline
    def test_multiline(self):
        regions = [R("a", "speech", "LINHA 1\nLINHA 2", (40, 40, 300, 120), font_size=24)]
        psd = PSDImage.open(self._write(regions))
        self.assertIn("\r", _type_layers(psd)["BALAO_001"].text)

    # D) alignment left/center/right
    def test_alignment(self):
        regions = [
            R("a", "narration", "L", (10, 10, 200, 50), alignment="left"),
            R("b", "narration", "C", (10, 100, 200, 50), alignment="center"),
            R("c", "narration", "Rr", (10, 200, 200, 50), alignment="right"),
        ]
        psd = PSDImage.open(self._write(regions))
        tl = _type_layers(psd)
        def just(layer):
            return int(layer.engine_dict["ParagraphRun"]["RunArray"][0]
                       ["ParagraphSheet"]["Properties"]["Justification"])
        self.assertEqual(just(tl["NARRACAO_001"]), 0)
        self.assertEqual(just(tl["NARRACAO_002"]), 2)
        self.assertEqual(just(tl["NARRACAO_003"]), 1)

    # E) different font sizes
    def test_font_sizes(self):
        regions = [R("a", "speech", "A", (10, 10, 100, 40), font_size=18),
                   R("b", "speech", "B", (10, 100, 100, 80), font_size=48)]
        psd = PSDImage.open(self._write(regions))
        tl = _type_layers(psd)
        def size(l):
            return float(l.engine_dict["StyleRun"]["RunArray"][0]["StyleSheet"]["StyleSheetData"]["FontSize"])
        self.assertEqual(size(tl["BALAO_001"]), 18.0)
        self.assertEqual(size(tl["BALAO_002"]), 48.0)

    # F) color
    def test_color(self):
        regions = [R("a", "speech", "X", (10, 10, 100, 40), color=(200, 10, 20))]
        psd = PSDImage.open(self._write(regions))
        vals = _type_layers(psd)["BALAO_001"].engine_dict["StyleRun"]["RunArray"][0]["StyleSheet"]["StyleSheetData"]["FillColor"]["Values"]
        rgb = [round(float(vals[i]) * 255) for i in (1, 2, 3)]
        self.assertEqual(rgb, [200, 10, 20])

    # G) region names deterministic by type
    def test_region_names_deterministic(self):
        regions = [
            R("a", "speech", "1", (0, 0, 50, 30)), R("b", "narration", "2", (0, 40, 50, 30)),
            R("c", "caption", "3", (0, 80, 50, 30)), R("d", "footnote", "4", (0, 120, 50, 30)),
            R("e", "title", "5", (0, 160, 50, 30)), R("f", "sfx", "6", (0, 200, 50, 30)),
            R("g", "whatever", "7", (0, 240, 50, 30)),
        ]
        names = ew.assign_layer_names(regions)
        self.assertEqual(names, ["BALAO_001", "NARRACAO_001", "CAPTION_001",
                                 "FOOTNOTE_001", "TITLE_001", "SFX_001", "TEXT_001"])

    # H) source fallback region -> *_REVIEW type layer, still native + carries source text
    def test_source_fallback_review_layer(self):
        regions = [R("a", "speech", "HELLO", (10, 10, 200, 60),
                     translation_status="source_fallback", source_text="HELLO")]
        psd = PSDImage.open(self._write(regions))
        tl = _type_layers(psd)
        self.assertIn("BALAO_001_REVIEW", tl)
        self.assertEqual(tl["BALAO_001_REVIEW"].text, "HELLO")

    # I) zero-text page -> valid PSD, empty TEXT group
    def test_zero_text_page(self):
        psd = PSDImage.open(self._write([]))
        self.assertIn("TEXT", _groups(psd))
        self.assertEqual(len(_type_layers(psd)), 0)

    # J-N) short/blank/tall real canvas sizes (no minimum-height fudging)
    def test_short_and_blank_and_tall_pages(self):
        for w, h, page in [(800, 32, 48), (800, 83, 118), (800, 56, 143),
                           (800, 1000, 207), (800, 336, 208)]:
            psd = PSDImage.open(self._write([], w=w, h=h, page=page))
            self.assertEqual((psd.width, psd.height), (w, h))
            self.assertEqual(set(_groups(psd)), {"ORIGINAL", "CLEAN", "TEXT"})

    # O) original independent (separate group, hidden by default, raster)
    def test_original_independent_hidden(self):
        psd = PSDImage.open(self._write([R("a", "speech", "X", (10, 10, 100, 40))]))
        g = _groups(psd)
        self.assertFalse(g["ORIGINAL"].visible)
        self.assertTrue(all(l.kind == "pixel" for l in g["ORIGINAL"]))

    # P) clean independent (visible base, raster, no type layers)
    def test_clean_independent_visible(self):
        psd = PSDImage.open(self._write([R("a", "speech", "X", (10, 10, 100, 40))]))
        g = _groups(psd)
        self.assertTrue(g["CLEAN"].visible)
        self.assertTrue(all(l.kind != "type" for l in g["CLEAN"]))

    # Q) TEXT toggle semantics structural: TEXT visible, text only in TEXT group
    def test_text_toggle_structural(self):
        psd = PSDImage.open(self._write([R("a", "speech", "X", (10, 10, 100, 40))]))
        g = _groups(psd)
        self.assertTrue(g["TEXT"].visible)
        self.assertTrue(all(l.kind != "type" for l in g["ORIGINAL"]))
        self.assertTrue(all(l.kind != "type" for l in g["CLEAN"]))

    # R) invalid/corrupt clean fails PSD (no silent success)
    def test_corrupt_clean_fails(self):
        dest = self.root / "psd" / "001.psd"
        with self.assertRaises(Exception):
            ew.write_editable_psd(dest, _img(800, 1000), "C:/does/not/exist.png",
                                  [], (800, 1000))
        self.assertFalse(dest.exists())

    # S) missing original fails appropriately
    def test_missing_original_fails(self):
        dest = self.root / "psd" / "001.psd"
        with self.assertRaises(Exception):
            ew.write_editable_psd(dest, "C:/nope/original.png", _img(800, 1000),
                                  [], (800, 1000))
        self.assertFalse(dest.exists())

    # T) partial write never marked produced: a validation failure leaves no dest + no temp
    def test_atomic_no_partial_on_validation_failure(self):
        import unittest.mock as mock
        dest = self.root / "psd" / "001.psd"
        with mock.patch.object(ew, "_validate_structure", side_effect=EditablePsdError("boom")):
            with self.assertRaises(EditablePsdError):
                ew.write_editable_psd(dest, _img(800, 1000), _img(800, 1000),
                                      [R("a", "speech", "X", (10, 10, 100, 40))], (800, 1000))
        self.assertFalse(dest.exists())
        leftovers = list((self.root / "psd").glob("*.tmp")) if (self.root / "psd").exists() else []
        self.assertEqual(leftovers, [])

    def test_dimension_mismatch_fails_without_overwriting_prior_psd(self):
        dest = self.root / "psd" / "001.psd"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"previous validated output")
        with self.assertRaisesRegex(EditablePsdError, "image_dimension_mismatch"):
            ew.write_editable_psd(dest, _img(64, 48), _img(32, 24), [], (64, 48))
        self.assertEqual(dest.read_bytes(), b"previous validated output")
        self.assertEqual(list(dest.parent.glob("*.tmp")), [])

    # X) editable PSD path is the contract's psd/<page>.psd; distinct from legacy raster type
    def test_canonical_path_and_type_distinct(self):
        p = ac.artifact_relative_path(ac.ArtifactType.EDITABLE_PSD, page=1, total_pages=208)
        self.assertEqual(p, "psd/001.png".replace(".png", ".psd"))
        self.assertNotEqual(ac.ArtifactType.EDITABLE_PSD, ac.ArtifactType.LEGACY_RASTER_PSD)

    # color profile embedded (sRGB) -> no missing-profile warning expectation
    def test_srgb_color_profile_embedded(self):
        psd = PSDImage.open(self._write([R("a", "speech", "X", (10, 10, 100, 40))]))
        icc = psd.image_resources.get_data(Resource.ICC_PROFILE)
        self.assertTrue(icc)  # non-empty ICC blob present

    # no ACCENTS_DIAG ever emitted by the writer (that was M5-spike only)
    def test_no_accents_diag(self):
        psd = PSDImage.open(self._write([R("a", "speech", "X", (10, 10, 100, 40))]))
        self.assertNotIn("ACCENTS_DIAG", _type_layers(psd))


if __name__ == "__main__":
    unittest.main()
