"""Local pages must survive the pipeline uncut.

Smart Split exists because webtoon assets are transport slices, not pages: it joins them and
re-cuts on safe horizontal bands. Applied to a local folder — where each file already is a
complete page — that would destroy the author's page boundaries. These tests pin the skip.
"""

import _test_bootstrap  # noqa: F401

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from pdf import prepare_smart_webtoon_pages


def write_page(folder: Path, name: str, *, width: int = 800, height: int = 1200,
               colour: tuple[int, int, int] = (30, 60, 90)) -> Path:
    path = folder / name
    Image.new("RGB", (width, height), colour).save(path)
    return path


class LogicalPagePassthroughTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.split = self.tmp / "split"

    def pages(self, count=3, **kw):
        return [write_page(self.tmp, f"{i}.png", colour=(i * 20, 60, 90), **kw)
                for i in range(1, count + 1)]

    def test_complete_pages_are_not_recut(self):
        sources = self.pages(3)
        out, report = prepare_smart_webtoon_pages(sources, self.split, logical_pages=True)
        self.assertEqual(len(out), 3)                      # 3 in, 3 out — never merged
        self.assertTrue(report["smart_split_skipped"])
        self.assertEqual(report["strategy"], "logical_pages_passthrough")
        self.assertEqual(report["splits"], [])

    def test_page_dimensions_are_preserved_exactly(self):
        sources = self.pages(2, width=760, height=2600)     # taller than max_height
        out, _ = prepare_smart_webtoon_pages(sources, self.split, logical_pages=True)
        for path in out:
            with Image.open(path) as image:
                # Without the skip this page would have been cut into two.
                self.assertEqual(image.size, (760, 2600))

    def test_order_is_preserved(self):
        sources = self.pages(4)
        out, _ = prepare_smart_webtoon_pages(sources, self.split, logical_pages=True)
        originals = [Image.open(p).convert("RGB").getpixel((5, 5)) for p in sources]
        produced = [Image.open(p).convert("RGB").getpixel((5, 5)) for p in out]
        self.assertEqual(produced, originals)

    def test_natural_order_not_lexicographic(self):
        # 10.png must land last, not between 1.png and 2.png.
        names = ["1.png", "2.png", "10.png"]
        sources = [write_page(self.tmp, n, colour=(i * 40, 10, 10))
                   for i, n in enumerate(names, start=1)]
        out, _ = prepare_smart_webtoon_pages(sources, self.split, logical_pages=True)
        produced = [Image.open(p).convert("RGB").getpixel((5, 5))[0] for p in out]
        self.assertEqual(produced, [40, 80, 120])

    def test_skip_is_recorded_in_the_report_file(self):
        prepare_smart_webtoon_pages(self.pages(2), self.split, logical_pages=True)
        payload = json.loads((self.split / "smart_split_report.json").read_text(encoding="utf-8"))
        self.assertTrue(payload["smart_split_skipped"])
        self.assertEqual(payload["skip_reason"], "inputs_are_complete_pages")

    def test_originals_are_never_modified(self):
        sources = self.pages(2)
        before = [(p.stat().st_size, p.read_bytes()[:32]) for p in sources]
        prepare_smart_webtoon_pages(sources, self.split, logical_pages=True)
        after = [(p.stat().st_size, p.read_bytes()[:32]) for p in sources]
        self.assertEqual(before, after)

    def test_slices_still_get_rebuilt_when_not_logical(self):
        # The default path must keep working: short slices are joined into pages.
        slices = [write_page(self.tmp, f"s{i}.png", width=800, height=400) for i in range(1, 7)]
        out, report = prepare_smart_webtoon_pages(slices, self.split)
        self.assertNotEqual(len(out), len(slices))          # actually rebuilt
        self.assertNotIn("smart_split_skipped", report)

    def test_empty_input_is_refused_in_both_modes(self):
        for logical in (True, False):
            with self.assertRaises(ValueError):
                prepare_smart_webtoon_pages([], self.split, logical_pages=logical)


class ManifestContractTests(unittest.TestCase):
    def test_local_manifest_marks_logical_pages(self):
        import local_folder_input

        source = Path(local_folder_input.__file__).read_text(encoding="utf-8")
        self.assertIn('"logical_page": True', source)
        self.assertIn('"logical_pages": True', source)


if __name__ == "__main__":
    unittest.main()
