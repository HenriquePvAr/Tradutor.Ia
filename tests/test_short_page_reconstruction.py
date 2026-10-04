"""Reconstruction must not drop pages the downloader accepted.

Physical run (comix.to between-two-floors ch.1) downloaded 208/208 but the PDF had 204:
the reconstruction validity guard used an absolute floor (``valid_image`` >=100x100 and
variance>5), so it dropped legitimately short pages (page 48 = 800x32 with content) and
faithfully blank pages (118/143 short-blank, 207 = 800x1000 blank source). The fix makes
the guard source-relative: a render is valid when it mirrors its accepted source, and only
a genuine failure (unreadable/degenerate output, or a content source rendered blank) is
rejected. No accepted page disappears silently and nothing is masked with a fake page.
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import cv2

import yomu.pipeline.benchmark_pipeline as bp


def _content(w, h):
    # High-variance image (real page content).
    rng = np.random.default_rng(1234)
    return rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)


def _blank(w, h):
    # Uniform image (variance 0) -- a genuinely blank source/render.
    return np.full((h, w, 3), 200, dtype=np.uint8)


class ShortPageReconstructionTests(unittest.TestCase):
    def _write(self, tmp, name, arr):
        p = str(Path(tmp) / name)
        assert cv2.imwrite(p, arr)
        return p

    def test_short_content_pages_are_valid(self):  # A/B/C: 800x32/83/56 with content
        with TemporaryDirectory() as tmp:
            for h in (32, 83, 56):
                arr = _content(800, h)
                src = self._write(tmp, f"src_{h}.png", arr)
                out = self._write(tmp, f"out_{h}.png", arr)
                self.assertTrue(bp._reconstructed_page_is_valid(out, src),
                                f"800x{h} content page must be valid")

    def test_normal_page_unchanged(self):  # D
        with TemporaryDirectory() as tmp:
            arr = _content(800, 1000)
            src = self._write(tmp, "s.png", arr); out = self._write(tmp, "o.png", arr)
            self.assertTrue(bp._reconstructed_page_is_valid(out, src))

    def test_content_source_rendered_blank_is_invalid(self):  # E: real render failure
        with TemporaryDirectory() as tmp:
            src = self._write(tmp, "s.png", _content(800, 1000))
            out = self._write(tmp, "o.png", _blank(800, 1000))
            self.assertFalse(bp._reconstructed_page_is_valid(out, src))

    def test_blank_source_blank_render_is_valid(self):  # G: faithfully blank page kept
        with TemporaryDirectory() as tmp:
            for w, h in ((800, 1000), (800, 83), (800, 56)):
                src = self._write(tmp, f"s_{h}.png", _blank(w, h))
                out = self._write(tmp, f"o_{h}.png", _blank(w, h))
                self.assertTrue(bp._reconstructed_page_is_valid(out, src),
                                f"blank {w}x{h} source must be represented, not dropped")

    def test_unreadable_output_is_invalid(self):  # F/H
        with TemporaryDirectory() as tmp:
            src = self._write(tmp, "s.png", _content(800, 1000))
            self.assertFalse(bp._reconstructed_page_is_valid(str(Path(tmp) / "missing.png"), src))
            bad = str(Path(tmp) / "bad.png")
            Path(bad).write_bytes(b"not a png")
            self.assertFalse(bp._reconstructed_page_is_valid(bad, src))
            self.assertFalse(bp._reconstructed_page_is_valid("", src))

    def test_208_logical_coverage_none_dropped(self):
        # 205 normal + 3 short (48/118/143) + treat 207 as blank source: all represented.
        with TemporaryDirectory() as tmp:
            short = {48: 32, 118: 83, 143: 56}
            blank_pages = {118, 143, 207}
            kept = 0
            for n in range(1, 209):
                h = short.get(n, 1000)
                arr = _blank(800, h) if n in blank_pages else _content(800, h)
                src = self._write(tmp, f"s{n}.png", arr)
                out = self._write(tmp, f"o{n}.png", arr)
                if bp._reconstructed_page_is_valid(out, src):
                    kept += 1
            self.assertEqual(kept, 208)  # every accepted logical page survives


if __name__ == "__main__":
    unittest.main()
