from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

import benchmark_pipeline
from benchmark_pipeline import _resolve_download_max_images, _select_image_entries
from pdf import generate_smart_webtoon_pdf, prepare_smart_webtoon_pages


class SmartWebtoonSplitTests(unittest.TestCase):
    def test_confirmed_reader_selection_keeps_limited_download_scope(self):
        with mock.patch.object(benchmark_pipeline.config, "SMART_WEBTOON_PDF_SPLIT", True):
            self.assertEqual(
                _resolve_download_max_images(
                    2,
                    selected_page_indices=[],
                    local_manifest_path="",
                    source_candidate_ids=["slot-1", "slot-2", "slot-3"],
                ),
                2,
            )

    def test_unconfirmed_remote_smart_split_still_collects_full_chapter(self):
        with mock.patch.object(benchmark_pipeline.config, "SMART_WEBTOON_PDF_SPLIT", True):
            self.assertIsNone(
                _resolve_download_max_images(
                    2,
                    selected_page_indices=[],
                    local_manifest_path="",
                    source_candidate_ids=[],
                )
            )

    def test_local_manifest_page_selection_preserves_required_source_extent(self):
        with mock.patch.object(benchmark_pipeline.config, "SMART_WEBTOON_PDF_SPLIT", True):
            self.assertEqual(
                _resolve_download_max_images(
                    2,
                    selected_page_indices=[4],
                    local_manifest_path="snapshot.json",
                    source_candidate_ids=[],
                ),
                4,
            )

    def test_logical_page_indices_are_selected_after_split(self):
        logical_pages = [f"page_{index:03d}.png" for index in range(1, 8)]
        selected, missing = _select_image_entries(logical_pages, [2, 6])

        self.assertEqual(
            [entry["path"] for entry in selected],
            [logical_pages[1], logical_pages[5]],
        )
        self.assertEqual([entry["index"] for entry in selected], [2, 6])
        self.assertEqual(missing, [])

    def _write_source_slices(self, root, *, with_safe_gutter=True, count=3):
        total_height = count * 1000
        stream = Image.new("RGB", (240, total_height), (76, 92, 118))
        draw = ImageDraw.Draw(stream)
        for y in range(0, total_height, 90):
            draw.line(
                (0, y, 239, min(total_height - 1, y + 70)),
                fill=(225, 105, 75),
                width=9,
            )
        if with_safe_gutter:
            draw.rectangle((0, 1778, 239, 1822), fill="white")
        paths = []
        for index in range(count):
            path = root / f"source_{index + 1:03}.png"
            stream.crop((0, index * 1000, 240, (index + 1) * 1000)).save(path)
            paths.append(str(path))
        stream.close()
        return paths

    def test_rebuilds_transport_slices_at_white_low_texture_gutter(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = self._write_source_slices(root)
            pages, report = prepare_smart_webtoon_pages(
                sources,
                root / "logical",
                target_height=1800,
                min_height=1050,
                max_height=2400,
            )

            self.assertEqual(len(pages), 2)
            self.assertEqual(report["source_images"], 3)
            self.assertEqual(report["source_total_height"], 3000)
            self.assertEqual(report["unsafe_split_count"], 0)
            self.assertEqual(sum(item["height"] for item in report["splits"]), 3000)
            self.assertTrue(report["splits"][0]["safe_band"])
            self.assertLess(abs(report["splits"][0]["height"] - 1800), 35)

    def test_keeps_taller_final_page_instead_of_forcing_unsafe_cut(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = self._write_source_slices(root, with_safe_gutter=False)
            _, report = prepare_smart_webtoon_pages(
                sources,
                root / "logical",
                target_height=1800,
                min_height=1050,
                max_height=2400,
            )

            self.assertEqual(report["unsafe_split_count"], 0)
            self.assertEqual(report["pdf_pages"], 1)
            self.assertEqual(report["splits"][0]["height"], 3000)

    def test_forces_low_risk_cut_only_after_hard_height_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = self._write_source_slices(
                root,
                with_safe_gutter=False,
                count=5,
            )
            _, report = prepare_smart_webtoon_pages(
                sources,
                root / "logical",
                target_height=1800,
                min_height=1050,
                max_height=2400,
            )

            self.assertGreaterEqual(report["unsafe_split_count"], 1)
            self.assertEqual(report["splits"][0]["reason"], "lowest_risk_band")

    def test_generates_pdf_from_rebuilt_logical_pages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = self._write_source_slices(root)
            pdf_path = root / "chapter.pdf"
            pages, report = generate_smart_webtoon_pdf(
                sources,
                pdf_path,
                root / "logical",
                target_height=1800,
                min_height=1050,
                max_height=2400,
            )

            self.assertTrue(pdf_path.is_file())
            self.assertGreater(pdf_path.stat().st_size, 1000)
            self.assertEqual(len(pages), report["pdf_pages"])
            for page in pages:
                with Image.open(page) as image:
                    image.verify()


if __name__ == "__main__":
    unittest.main()
