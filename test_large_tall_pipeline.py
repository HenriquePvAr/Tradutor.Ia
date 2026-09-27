from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from image_validation import validate_image_bytes
from local_folder_source import LocalFolderChapterAdapter, LocalFolderPolicy
from local_images_source import LocalImagesSelection
from ocr_engine import OCREngine, OCRLine
from pdf import MAX_LOGICAL_PAGE_HEIGHT, generate_pdf
from pdf_reader import parse_document
from png_output import export_final_pages_png


def _line(text: str, box: tuple[int, int, int, int], confidence: float = 0.9) -> OCRLine:
    x, y, width, height = box
    polygon = np.array(
        [[x, y], [x + width, y], [x + width, y + height], [x, y + height]],
        dtype=np.int32,
    )
    return OCRLine(text, confidence, polygon, box, text, engine="fake")


def test_large_page_is_tiled_as_one_logical_page_and_lines_are_global(monkeypatch):
    engine = OCREngine("en", engine="rapidocr")
    calls = []

    def fake_single(image, **_kwargs):
        calls.append(image.shape[:2])
        # The overlap sees the same line twice. A separate nearby line has distinct
        # lexical identity and must not be discarded by geometric proximity alone.
        if len(calls) == 1:
            return [_line("boundary text", (50, 3900, 180, 42))]
        return [
            _line("boundary text", (50, 64, 180, 42), 0.95),
            _line("other text", (50, 110, 180, 42)),
        ]

    monkeypatch.setattr(engine, "_detect_lines_single", fake_single)
    image = np.zeros((5000, 1080, 3), dtype=np.uint8)
    lines = engine.detect_lines(image, page=7)

    assert len(calls) == 2
    assert all(height <= engine.TALL_IMAGE_TILE_MAX_HEIGHT for height, _ in calls)
    assert [line.text for line in lines] == ["boundary text", "other text"]
    assert lines[0].box == (50, 3904, 180, 42)
    assert lines[0].polygon[:, 1].min() == 3904
    assert engine.last_run_metadata["tall_image_tiling"]["logical_page_count"] == 1
    assert engine.last_run_metadata["tall_image_tiling"]["duplicate_lines_removed"] == 1


def test_tile_ocr_failure_is_not_silently_returned_as_success(monkeypatch):
    engine = OCREngine("en", engine="rapidocr")
    calls = 0

    def fail_second(_image, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("controlled")
        return []

    monkeypatch.setattr(engine, "_detect_lines_single", fail_second)
    with pytest.raises(RuntimeError, match="tile 2"):
        engine.detect_lines(np.zeros((5000, 1080, 3), dtype=np.uint8), page=1)
    assert engine.last_run_metadata["tile_failure"] == {
        "tile_index": 2,
        "global_y_start": 3840,
        "global_y_end": 5000,
        "stage": "ocr",
        "exception_type": "OSError",
    }


def test_local_image_and_folder_intake_and_png_preserve_tall_logical_page(tmp_path):
    heights = (5000, 10000, 15000, 20000, 30000)
    local_images = tmp_path / "local-images"
    local_images.mkdir()
    paths = []
    for height in heights:
        path = local_images / f"page-{height}.png"
        image = Image.new("RGB", (1080, height), "white")
        for y, color in ((100, "red"), (height // 2, "green"), (height - 100, "blue")):
            image.paste(color, (0, y, 1080, y + 24))
        image.save(path, "PNG")
        image.close()
        paths.append(path)

    selection = LocalImagesSelection()
    selected = selection.add(paths)
    assert len(selected) == len(heights)
    for path, height in zip(paths, heights):
        validated = validate_image_bytes(path.read_bytes(), min_bytes=12)
        assert (validated.width, validated.height) == (1080, height)

    png_dir = export_final_pages_png(paths, tmp_path / "png-out", "tall")
    assert len(list(png_dir.glob("*.png"))) == len(heights)
    output_sizes = []
    for output_path in sorted(png_dir.glob("*.png")):
        with Image.open(output_path) as image:
            output_sizes.append(image.size)
    assert output_sizes == [(1080, height) for height in heights]

    folder_root = tmp_path / "allowed"
    chapter = folder_root / "chapter"
    chapter.mkdir(parents=True)
    for filename, source_path in (("page-10k.png", paths[1]), ("page-20k.png", paths[3])):
        with Image.open(source_path) as source:
            source.save(chapter / filename, "PNG")
    adapter = LocalFolderChapterAdapter(
        policy=LocalFolderPolicy([folder_root])
    )
    analysis = adapter.analyze(chapter)
    assert [(page.width, page.height) for page in analysis.pages] == [
        (1080, 10000), (1080, 20000)
    ]
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    snapshot = adapter.snapshot(chapter, snapshots, snapshot_id="tall-20k")
    assert snapshot.public()["accepted_page_count"] == 2


@pytest.mark.parametrize("height,expected_pages", [(15000, 2), (20000, 2), (30000, 3)])
def test_pdf_splits_only_physical_pages_at_format_limit(tmp_path: Path, height, expected_pages):
    source = tmp_path / f"source-{height}.png"
    pdf = tmp_path / f"source-{height}.pdf"
    # Three distinct horizontal bands allow an independent reader to verify the
    # PDF preserves the full top-to-bottom raster extent after segmentation.
    image = Image.new("RGB", (1080, height), "white")
    for y, color in ((100, "red"), (height // 2, "green"), (height - 100, "blue")):
        image.paste(color, (0, y, 1080, y + 40))
    image.save(source, "PNG")
    image.close()

    generate_pdf([source], pdf)
    document = parse_document(pdf)
    assert document.page_count == expected_pages
    pages = [document.page(index) for index in range(1, document.page_count + 1)]
    assert all(page.width == 1080 for page in pages)
    assert [page.height for page in pages] == [
        min(MAX_LOGICAL_PAGE_HEIGHT, height - (index * MAX_LOGICAL_PAGE_HEIGHT))
        for index in range(expected_pages)
    ]
    assert sum(page.height for page in pages) == height
    expected_bands = ((100, "red"), (height // 2, "green"), (height - 100, "blue"))
    for global_y, color in expected_bands:
        page_index = global_y // MAX_LOGICAL_PAGE_HEIGHT
        local_y = global_y % MAX_LOGICAL_PAGE_HEIGHT
        with Image.open(io.BytesIO(document.page_bytes(page_index + 1))) as rendered:
            pixel = rendered.convert("RGB").getpixel((540, local_y + 20))
        dominant = max(range(3), key=lambda channel: pixel[channel])
        expected_channel = {"red": 0, "green": 1, "blue": 2}[color]
        assert dominant == expected_channel, (height, global_y, color, pixel)
    assert pdf.stat().st_size > 0
