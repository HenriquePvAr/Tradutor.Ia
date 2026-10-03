from pathlib import Path

import pytest
from PIL import Image

from png_output import PngExportError, export_final_pages_png


def _page(path: Path, color: str) -> Path:
    Image.new("RGB", (17, 11), color).save(path, format="PNG")
    return path


def test_exports_final_pages_in_stable_order_and_validates(tmp_path):
    first = _page(tmp_path / "render-b.png", "red")
    second = _page(tmp_path / "render-a.png", "blue")
    result = export_final_pages_png([first, second], tmp_path / "out", "chapter 1")
    assert [p.name for p in sorted(result.iterdir())] == ["001.png", "002.png"]
    assert Image.open(result / "001.png").getpixel((0, 0))[:3] == (255, 0, 0)
    assert all(p.stat().st_size > 0 for p in result.iterdir())


def test_export_collision_gets_safe_suffix(tmp_path):
    page = _page(tmp_path / "page.png", "green")
    first = export_final_pages_png([page], tmp_path / "out", "chapter")
    second = export_final_pages_png([page], tmp_path / "out", "chapter")
    assert first.name == "chapter"
    assert second.name == "chapter_2"


def test_failure_and_cancel_leave_no_partial_directory(tmp_path):
    page = _page(tmp_path / "page.png", "green")
    with pytest.raises(PngExportError, match="cancelled"):
        export_final_pages_png([page], tmp_path / "out", "chapter", cancel=lambda: True)
    assert not (tmp_path / "out" / "chapter").exists()
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    with pytest.raises(PngExportError):
        export_final_pages_png([bad], tmp_path / "out", "other")
    assert not (tmp_path / "out" / "other").exists()

