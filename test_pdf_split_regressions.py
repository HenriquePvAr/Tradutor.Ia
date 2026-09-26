from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np
from PIL import Image, ImageDraw

import benchmark_pipeline
from benchmark_pipeline import _resolve_download_max_images, _select_image_entries
import pdf
from pdf import (
    MAX_LOGICAL_PAGE_HEIGHT,
    PROTECTED_REGION_PADDING,
    ProtectedRegionDetectionError,
    generate_smart_webtoon_pdf,
    prepare_smart_webtoon_pages,
    protected_vertical_intervals,
    smart_split_audit,
)


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
            # A full (unbounded) run of an unconfirmed remote smart split collects the whole
            # chapter, so split boundaries are computed from the complete source.
            self.assertIsNone(
                _resolve_download_max_images(
                    None,
                    selected_page_indices=[],
                    local_manifest_path="",
                    source_candidate_ids=[],
                )
            )
            # An explicit bounded request is an execution contract and stays bounded even for
            # an unconfirmed smart split (partial scope wins over full-chapter collection).
            self.assertEqual(
                _resolve_download_max_images(
                    2,
                    selected_page_indices=[],
                    local_manifest_path="",
                    source_candidate_ids=[],
                ),
                2,
            )

    def test_local_manifest_page_selection_preserves_required_source_extent(self):
        with mock.patch.object(benchmark_pipeline.config, "SMART_WEBTOON_PDF_SPLIT", True):
            # An unbounded run with a local-manifest page selection extends the source
            # download to cover the highest selected page.
            self.assertEqual(
                _resolve_download_max_images(
                    None,
                    selected_page_indices=[4],
                    local_manifest_path="snapshot.json",
                    source_candidate_ids=[],
                ),
                4,
            )
            # An explicit bounded request stays bounded even with a manifest selection.
            self.assertEqual(
                _resolve_download_max_images(
                    2,
                    selected_page_indices=[4],
                    local_manifest_path="snapshot.json",
                    source_candidate_ids=[],
                ),
                2,
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

    def test_art_without_a_gutter_is_cut_automatically_after_the_hard_limit(self):
        # Busy artwork with no gutter is not a reason to ask a human anything: no
        # balloon, text box or translation group crosses the seam, so the cut is
        # applied and the chapter stays publishable.
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

            self.assertEqual(report["unsafe_split_count"], 0)
            self.assertEqual(report["splits"][0]["reason"], "semantic_safe")
            self.assertFalse(report["splits"][0]["gutter"])

    def test_expands_past_hard_limit_when_next_safe_gutter_is_nearby(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stream = Image.new("RGB", (240, 5200), (76, 92, 118))
            draw = ImageDraw.Draw(stream)
            for y in range(0, 5200, 80):
                draw.line((0, y, 239, min(5199, y + 70)), fill=(225, 105, 75), width=11)
            draw.rectangle((0, 4590, 239, 4634), fill="white")
            sources = []
            for index in range(6):
                path = root / f"source_{index + 1:03}.png"
                stream.crop((0, index * 900, 240, min(5200, (index + 1) * 900))).save(path)
                sources.append(str(path))
            stream.close()

            _, report = prepare_smart_webtoon_pages(
                sources,
                root / "logical",
                target_height=1800,
                min_height=1050,
                max_height=2400,
            )

            self.assertEqual(report["unsafe_split_count"], 0)
            self.assertTrue(report["splits"][0]["safe_band"])
            self.assertEqual(report["splits"][0]["reason"], "white_gutter")
            self.assertGreater(report["splits"][0]["height"], 4200)
            self.assertLess(abs(report["splits"][0]["height"] - 4612), 35)

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


def _noise_art(height, width=800, seed=7):
    """Continuous artwork with no flat area anywhere - never a safe visual gutter."""
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, (height, width, 3), dtype=np.uint8)


def _paint_balloon(canvas, top, bottom, left=280, right=600):
    """Paint a speech balloon: flat interior inside a high contrast border."""
    canvas[top:bottom, left:right] = 20
    canvas[top : top + 6, left:right] = 255
    canvas[bottom - 6 : bottom, left:right] = 255
    canvas[top:bottom, left : left + 6] = 255
    canvas[top:bottom, right - 6 : right] = 255


def _paint_textured_balloon(canvas, top, bottom, left=280, right=600, seed=3):
    """A balloon whose interior is not flat: a gradient plus texture inside the outline.

    Nothing here is uniform enough for the flat-blob detector, but a reader sees a
    balloon: a closed high contrast outline around a calm fill.
    """
    rng = np.random.default_rng(seed)
    height, width = bottom - top, right - left
    gradient = np.linspace(150.0, 232.0, height)[:, None]
    interior = np.clip(gradient + rng.normal(0, 11, (height, width)), 0, 255)
    canvas[top:bottom, left:right] = interior.astype(np.uint8)[:, :, None]
    canvas[top : top + 6, left:right] = 0
    canvas[bottom - 6 : bottom, left:right] = 0
    canvas[top:bottom, left : left + 6] = 0
    canvas[top:bottom, right - 6 : right] = 0


def _paint_lettered_balloon(canvas, top, bottom, left=280, right=600, seed=5):
    """A balloon whose interior is broken into strips by its own lettering."""
    rng = np.random.default_rng(seed)
    height, width = bottom - top, right - left
    interior = np.clip(rng.normal(238, 7, (height, width)), 0, 255)
    canvas[top:bottom, left:right] = interior.astype(np.uint8)[:, :, None]
    canvas[top : top + 5, left:right] = 0
    canvas[bottom - 5 : bottom, left:right] = 0
    canvas[top:bottom, left : left + 5] = 0
    canvas[top:bottom, right - 5 : right] = 0
    for row in range(top + 30, bottom - 30, 46):
        x = left + 24
        while x < right - 40:
            glyph = int(rng.integers(10, 24))
            canvas[row : row + 26, x : x + glyph] = 12
            x += glyph + int(rng.integers(6, 14))


def _paint_textured_art(canvas, top, bottom, left=200, right=640, seed=11):
    """Bright, busy artwork inside a border - balloon-shaped but not a balloon.

    Screentone and crossing strokes, so the fill never reads as a container. This is
    the false positive a stronger detector must not invent.
    """
    rng = np.random.default_rng(seed)
    height, width = bottom - top, right - left
    patch = np.clip(rng.normal(212, 15, (height, width)), 0, 255)
    rows, columns = np.mgrid[0:height, 0:width]
    patch[(rows % 6 < 2) & (columns % 6 < 2)] = 55
    canvas[top:bottom, left:right] = patch.astype(np.uint8)[:, :, None]
    for _ in range(45):
        x = int(rng.integers(left, right))
        y = int(rng.integers(top, bottom))
        cv2.line(
            canvas,
            (x, y),
            (x + int(rng.integers(-180, 180)), y + int(rng.integers(-180, 180))),
            (10, 10, 10),
            5,
        )
    cv2.rectangle(canvas, (left, top), (right - 1, bottom - 1), (0, 0, 0), 6)


def _paint_white_gutter(canvas, top, bottom):
    canvas[top:bottom, :] = 255


def _covering_interval(canvas, top, bottom):
    """The protected interval containing ``top..bottom``, or ``None``."""
    image = Image.fromarray(canvas)
    try:
        intervals = protected_vertical_intervals(image)
    finally:
        image.close()
    return next(
        (pair for pair in intervals if pair[0] <= top and pair[1] >= bottom), None
    )


def _slice_stream(canvas, root, slice_height=900):
    stream = Image.fromarray(canvas)
    paths = []
    for index, top in enumerate(range(0, stream.height, slice_height), start=1):
        path = root / f"source_{index:03}.png"
        stream.crop((0, top, stream.width, min(stream.height, top + slice_height))).save(path)
        paths.append(str(path))
    stream.close()
    return paths


class SmartSplitProtectedRegionTests(unittest.TestCase):
    """Hard constraint: a cut may cross artwork, never a balloon/text/group."""

    def _split(self, canvas, root, **kwargs):
        options = {"target_height": 1800, "min_height": 1050, "max_height": 2400}
        options.update(kwargs)
        return prepare_smart_webtoon_pages(
            _slice_stream(canvas, root),
            root / "logical",
            **options,
        )

    def _boundaries(self, report):
        """Absolute stream coordinates of every applied cut."""
        offsets = []
        running = 0
        for record in report["splits"][:-1]:
            running += int(record["height"])
            offsets.append(running)
        return offsets

    def test_interval_merging_unions_touching_protected_regions(self):
        image = Image.fromarray(_noise_art(2000))
        merged = protected_vertical_intervals(
            image,
            extra_regions=[(1000, 1300), (1250, 1500)],
            padding=0,
        )
        image.close()
        self.assertIn((1000, 1500), merged)

    def test_protected_interval_edges_follow_the_documented_inequality(self):
        image = Image.fromarray(_noise_art(2000))
        merged = protected_vertical_intervals(
            image, extra_regions=[(1000, 1300)], padding=PROTECTED_REGION_PADDING
        )
        image.close()
        interval = next(pair for pair in merged if pair[0] <= 1000 <= pair[1])
        self.assertEqual(interval, (1000 - PROTECTED_REGION_PADDING, 1300 + PROTECTED_REGION_PADDING))

    def test_overlapping_balloons_are_protected_as_one_union(self):
        canvas = _noise_art(3000)
        _paint_balloon(canvas, 1200, 1500, left=200, right=520)
        _paint_balloon(canvas, 1450, 1760, left=380, right=700)
        image = Image.fromarray(canvas)
        merged = protected_vertical_intervals(image)
        image.close()
        covering = [pair for pair in merged if pair[0] <= 1300 and pair[1] >= 1700]
        self.assertEqual(len(covering), 1, merged)

    def test_long_balloon_pushes_the_cut_out_of_its_interval(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canvas = _noise_art(9000)
            _paint_balloon(canvas, 2800, 3500)
            _, report = self._split(canvas, root, target_height=3000, min_height=1800, max_height=3600)

            for cut in self._boundaries(report):
                self.assertFalse(2800 <= cut <= 3500, f"cut {cut} bisects the balloon")

    def test_real_56_57_geometry_no_longer_bisects_the_balloon(self):
        # Geometry taken from the real chapter: the applied cut was y=1671 and the
        # balloon measured y=1583..1833, so the seam ran through the middle of it.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canvas = _noise_art(5391)
            _paint_balloon(canvas, 1583, 1833, left=329, right=649)
            _paint_white_gutter(canvas, 5105, 5391)
            _, report = self._split(canvas, root)

            self.assertEqual(report["unsafe_split_count"], 0)
            cuts = self._boundaries(report)
            self.assertTrue(cuts)
            for cut in cuts:
                self.assertFalse(1583 <= cut <= 1833, f"cut {cut} bisects the balloon")

    def test_art_only_high_band_score_is_resolved_without_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, report = self._split(_noise_art(9000), root)

            self.assertEqual(report["unsafe_split_count"], 0)
            self.assertGreater(report["pdf_pages"], 1)
            self.assertTrue(all(item["safe_band"] for item in report["splits"]))

    def test_white_gutter_is_still_preferred_over_a_nearer_art_cut(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canvas = _noise_art(5400)
            _paint_white_gutter(canvas, 2180, 2240)
            _, report = self._split(canvas, root)

            self.assertEqual(report["splits"][0]["reason"], "white_gutter")
            self.assertLess(abs(report["splits"][0]["height"] - 2210), 40)

    def test_injected_text_region_moves_the_cut_without_a_balloon_detector(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canvas = _noise_art(5400)
            # No painted balloon at all: only an OCR/text box is known.
            _, report = self._split(canvas, root, protected_regions=[(1700, 2100)])

            for cut in self._boundaries(report):
                self.assertFalse(1700 <= cut <= 2100, f"cut {cut} splits the text box")

    def test_injected_translation_group_stays_inside_one_logical_page(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canvas = _noise_art(5400)
            _, report = self._split(canvas, root, protected_regions=[(1500, 2600)])

            for cut in self._boundaries(report):
                self.assertFalse(1500 <= cut <= 2600, f"cut {cut} splits the group")

    def test_keeps_segments_together_when_the_whole_window_is_protected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canvas = _noise_art(6000)
            _paint_white_gutter(canvas, 5400, 5460)
            # One balloon spanning the entire normal and expanded search window.
            _, report = self._split(
                canvas, root, protected_regions=[(1000, 5350)]
            )

            self.assertEqual(report["unsafe_split_count"], 0)
            for cut in self._boundaries(report):
                self.assertFalse(1000 <= cut <= 5350, f"cut {cut} splits the balloon")

    def test_variable_logical_page_height_is_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canvas = _noise_art(9000)
            _paint_balloon(canvas, 1700, 2300)
            _, report = self._split(canvas, root)

            heights = [int(item["height"]) for item in report["splits"]]
            self.assertGreater(len(set(heights)), 1, heights)
            self.assertEqual(sum(heights), 9000)

    def test_selection_is_deterministic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canvas = _noise_art(7000)
            _paint_balloon(canvas, 1700, 2100)
            first = self._split(canvas, root)[1]
            second = self._split(canvas, root)[1]

            self.assertEqual(first["splits"], second["splits"])

    def test_maximum_logical_page_height_matches_the_pdf_unit_limit(self):
        # A PDF page is written at 72 dpi, so one pixel is one PDF unit and the
        # format caps a page at 14400 units.
        self.assertEqual(MAX_LOGICAL_PAGE_HEIGHT, 14400)

    def test_trace_records_the_decision_for_every_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canvas = _noise_art(6000)
            _paint_balloon(canvas, 1700, 2100)
            _, report = self._split(canvas, root)

            boundary = report["splits"][0]
            for field in ("target_y", "selected_y", "delta", "hard_collision_count", "decision"):
                self.assertIn(field, boundary)
            self.assertGreater(boundary["hard_collision_count"], 0)

    def test_pdf_keeps_every_variable_height_page_uncropped_and_in_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canvas = _noise_art(9000)
            _paint_balloon(canvas, 1700, 2300)
            _paint_white_gutter(canvas, 5000, 5060)
            pdf_path = root / "chapter.pdf"
            pages, report = generate_smart_webtoon_pdf(
                _slice_stream(canvas, root),
                pdf_path,
                root / "logical",
                target_height=1800,
                min_height=1050,
                max_height=2400,
            )

            self.assertTrue(pdf_path.is_file())
            self.assertEqual(len(pages), len(report["splits"]))
            rendered = 0
            for page, record in zip(pages, report["splits"]):
                with Image.open(page) as image:
                    self.assertEqual(image.height, int(record["height"]))
                    rendered += image.height
            self.assertEqual(rendered, 9000)


class SmartSplitPngEncodingTests(unittest.TestCase):
    """Logical pages are lossless PNG, but never pay for Pillow's optimizer.

    ``optimize=True`` runs an exhaustive filter/Huffman search that cost ~32s on a
    real chapter to save ~1.3% of the bytes. PNG is lossless either way, so the
    decoded pixels - the only thing OCR and the PDF see - are unaffected.
    """

    def _split(self, root, **kwargs):
        options = {"target_height": 1800, "min_height": 1050, "max_height": 2400}
        options.update(kwargs)
        canvas = _noise_art(6000)
        _paint_balloon(canvas, 1700, 2100)
        return prepare_smart_webtoon_pages(
            _slice_stream(canvas, root), root / "logical", **options
        )

    def test_logical_pages_are_written_without_the_expensive_optimizer(self):
        saves = []
        original = Image.Image.save

        def record(image, target, *args, **kwargs):
            saves.append((args, kwargs))
            return original(image, target, *args, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(Image.Image, "save", record):
                pages, _ = self._split(Path(temporary))

        page_saves = [item for item in saves if item[0][:1] == ("PNG",)]
        self.assertEqual(len(page_saves), len(pages))
        for _, kwargs in page_saves:
            self.assertFalse(kwargs.get("optimize"), kwargs)

    def test_encoding_preserves_decoded_pixels_dimensions_and_mode(self):
        import io

        with tempfile.TemporaryDirectory() as temporary:
            pages, report = self._split(Path(temporary))
            self.assertEqual(len(pages), len(report["splits"]))
            for page, record in zip(pages, report["splits"]):
                with Image.open(page) as image:
                    image.load()
                    reference = image.copy()
                buffer = io.BytesIO()
                reference.save(buffer, "PNG", optimize=True)
                buffer.seek(0)
                with Image.open(buffer) as optimized:
                    self.assertEqual(optimized.size, reference.size)
                    self.assertEqual(optimized.mode, reference.mode)
                    self.assertEqual(optimized.tobytes(), reference.tobytes())
                self.assertEqual(reference.height, int(record["height"]))
                reference.close()

    def test_encoded_output_is_deterministic_across_runs(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            pages_a, _ = self._split(Path(first))
            pages_b, _ = self._split(Path(second))

            self.assertEqual(len(pages_a), len(pages_b))
            for left, right in zip(pages_a, pages_b):
                self.assertEqual(Path(left).name, Path(right).name)
                self.assertEqual(Path(left).read_bytes(), Path(right).read_bytes())


class BalloonDetectorRecallTests(unittest.TestCase):
    """A balloon is protected because it is a balloon, not because it is flat.

    The flat-blob detector only recognised a balloon whose interior was one uniform
    fill. Give the same balloon a gradient, a light texture or its own lettering and it
    became invisible: no protected interval, so the seam was free to run through it and
    the cut was still reported as semantically safe. These are that class of page.
    """

    def _split(self, canvas, root, **kwargs):
        options = {"target_height": 1800, "min_height": 1050, "max_height": 2400}
        options.update(kwargs)
        return prepare_smart_webtoon_pages(
            _slice_stream(canvas, root), root / "logical", **options
        )

    def _boundaries(self, report):
        offsets = []
        running = 0
        for record in report["splits"][:-1]:
            running += int(record["height"])
            offsets.append(running)
        return offsets

    def test_flat_balloon_crossing_the_target_is_protected(self):
        canvas = _noise_art(5400)
        _paint_balloon(canvas, 1700, 2000)

        self.assertIsNotNone(_covering_interval(canvas, 1700, 2000))

    def test_textured_balloon_crossing_the_target_is_protected(self):
        canvas = _noise_art(5400)
        _paint_textured_balloon(canvas, 1700, 2000)

        self.assertIsNotNone(_covering_interval(canvas, 1700, 2000))

    def test_balloon_with_lettering_inside_is_protected(self):
        canvas = _noise_art(5400)
        _paint_lettered_balloon(canvas, 1650, 2060)

        self.assertIsNotNone(_covering_interval(canvas, 1650, 2060))

    def test_no_cut_lands_inside_a_textured_balloon(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canvas = _noise_art(5400)
            _paint_textured_balloon(canvas, 1700, 2000)
            _, report = self._split(canvas, root)

            self.assertEqual(report["unsafe_split_count"], 0)
            for cut in self._boundaries(report):
                self.assertFalse(1700 <= cut <= 2000, f"cut {cut} bisects the balloon")

    def test_no_cut_lands_inside_a_lettered_balloon(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canvas = _noise_art(5400)
            _paint_lettered_balloon(canvas, 1650, 2060)
            _, report = self._split(canvas, root)

            self.assertEqual(report["unsafe_split_count"], 0)
            for cut in self._boundaries(report):
                self.assertFalse(1650 <= cut <= 2060, f"cut {cut} bisects the balloon")

    def test_textured_artwork_is_not_protected_as_a_balloon(self):
        # Recall for balloons may not be bought by calling every bordered bright shape
        # a balloon: the page would stop being splittable at all.
        canvas = _noise_art(5400)
        _paint_textured_art(canvas, 1500, 2300)

        self.assertIsNone(_covering_interval(canvas, 1500, 2300))

    def test_a_cut_through_textured_artwork_is_still_applied(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canvas = _noise_art(6000)
            # The artwork covers the whole search window, so the only way out of the
            # first page is a cut straight through it.
            _paint_textured_art(canvas, 900, 4300)
            _, report = self._split(canvas, root)

            self.assertEqual(report["unsafe_split_count"], 0)
            first = report["splits"][0]
            self.assertTrue(first["semantic_safe"])
            self.assertFalse(first["gutter_safe"])
            self.assertTrue(900 <= int(first["height"]) <= 4300, first["height"])

    def test_white_gutter_still_wins_over_a_nearby_textured_balloon(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canvas = _noise_art(5400)
            _paint_textured_balloon(canvas, 1500, 1900)
            _paint_white_gutter(canvas, 2180, 2240)
            _, report = self._split(canvas, root)

            self.assertEqual(report["splits"][0]["reason"], "white_gutter")
            self.assertTrue(report["splits"][0]["gutter_safe"])
            self.assertLess(abs(report["splits"][0]["height"] - 2210), 40)

    def test_detector_failure_is_never_read_as_zero_balloons(self):
        # An exploded detector knows nothing about the page. Reporting no protected
        # region would turn every seam into a semantically safe cut.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canvas = _noise_art(5400)
            _paint_textured_balloon(canvas, 1700, 2000)
            with mock.patch.object(
                pdf.cv2, "Canny", side_effect=RuntimeError("opencv build has no Canny")
            ):
                with self.assertRaises(ProtectedRegionDetectionError) as caught:
                    self._split(canvas, root)

            self.assertIn("opencv build has no Canny", str(caught.exception))

    def test_report_separates_gutter_cuts_from_semantic_cuts(self):
        # ``safe_band`` alone cannot tell a white gutter from a deliberate cut through
        # artwork, and the two are not the same evidence.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canvas = _noise_art(5400)
            _paint_white_gutter(canvas, 2180, 2240)
            _, report = self._split(canvas, root)

            first = report["splits"][0]
            self.assertTrue(first["semantic_safe"])
            self.assertTrue(first["gutter_safe"])
            self.assertEqual(report["balloon_detector"], "ran")
            self.assertEqual(
                report["gutter_split_count"] + report["semantic_only_split_count"],
                len(report["splits"]) - report["unsafe_split_count"],
            )

    def test_art_only_cut_is_reported_as_semantic_not_as_a_gutter(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, report = self._split(_noise_art(9000), root)

            first = report["splits"][0]
            self.assertTrue(first["semantic_safe"])
            self.assertFalse(first["gutter_safe"])
            self.assertEqual(first["reason"], "semantic_safe")
            self.assertGreaterEqual(report["semantic_only_split_count"], 1)

    def test_impossible_page_stays_fail_closed_and_requires_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canvas = _noise_art(16000, width=200)
            # Nothing anywhere in the stream may be cut.
            _, report = self._split(canvas, root, protected_regions=[(0, 16000)])

            self.assertGreaterEqual(report["unsafe_split_count"], 1)
            unsafe = next(
                record for record in report["splits"] if not record["safe_band"]
            )
            self.assertFalse(unsafe["semantic_safe"])
            self.assertFalse(unsafe["gutter_safe"])
            self.assertEqual(unsafe["reason"], "no_semantic_safe_band")
            # The cut only happens once waiting would breach the PDF page limit.
            self.assertGreaterEqual(
                sum(int(record["height"]) for record in report["splits"]),
                MAX_LOGICAL_PAGE_HEIGHT,
            )
            audit = smart_split_audit(report)
            self.assertFalse(audit["safe"])
            self.assertTrue(all(item["requires_review"] for item in audit["details"]))


if __name__ == "__main__":
    unittest.main()
