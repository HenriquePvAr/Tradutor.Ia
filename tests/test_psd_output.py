from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from psd_tools import PSDImage

import psd_exporter
from psd_exporter import PsdExportError, export_final_pages_psd


def _image(path: Path, size=(24, 18), color="white", mode="RGB") -> None:
    Image.new(mode, size, color).save(path)


def test_real_psd_has_two_pixel_layers_and_pixel_identity(tmp_path):
    original = tmp_path / "original.png"
    translated = tmp_path / "translated.png"
    _image(original, color=(240, 240, 240))
    _image(translated, color=(20, 80, 210))
    result = export_final_pages_psd([(original, translated)], tmp_path / "out", "chapter")
    psd = PSDImage.open(result / "001.psd")
    assert psd.size == (24, 18)
    layers = list(psd)
    assert [layer.name for layer in layers] == ["Original", "Translated"]
    assert all(layer.kind == "pixel" for layer in layers)
    assert layers[0].topil().convert("RGBA").tobytes() == Image.open(original).convert("RGBA").tobytes()
    assert layers[1].topil().convert("RGBA").tobytes() == Image.open(translated).convert("RGBA").tobytes()
    assert psd.composite().convert("RGBA").tobytes() == Image.open(translated).convert("RGBA").tobytes()
    assert layers[0].visible and layers[1].visible


def test_bw_color_alpha_and_multipage_mapping(tmp_path):
    pairs = []
    for index, color in enumerate(("black", "#3366cc", (20, 30, 40, 180)), start=1):
        original = tmp_path / f"o{index}.png"
        translated = tmp_path / f"t{index}.png"
        _image(original, size=(32 + index, 20 + index), color="white", mode="RGB")
        _image(translated, size=(32 + index, 20 + index), color=color, mode="RGBA" if isinstance(color, tuple) else "RGB")
        pairs.append((original, translated))
    result = export_final_pages_psd(pairs, tmp_path / "out", "multi")
    assert [p.name for p in sorted(result.glob("*.psd"))] == ["001.psd", "002.psd", "003.psd"]
    assert [PSDImage.open(result / f"{i:03}.psd").size for i in (1, 2, 3)] == [(33, 21), (34, 22), (35, 23)]


def test_dimension_mismatch_is_rejected_without_final_directory(tmp_path):
    original = tmp_path / "original.png"
    translated = tmp_path / "translated.png"
    _image(original, size=(20, 20))
    _image(translated, size=(21, 20), color="red")
    with pytest.raises(PsdExportError, match="dimension"):
        export_final_pages_psd([(original, translated)], tmp_path / "out", "chapter")
    assert not (tmp_path / "out" / "chapter").exists()


def test_failure_on_page_two_does_not_promote_partial_directory(tmp_path, monkeypatch):
    pairs = []
    for index in range(3):
        original = tmp_path / f"o{index}.png"
        translated = tmp_path / f"t{index}.png"
        _image(original, color="white")
        _image(translated, color=(index * 50, 20, 20))
        pairs.append((original, translated))
    original_writer = psd_exporter._write_one
    calls = {"count": 0}

    def fail_second(*args):
        calls["count"] += 1
        if calls["count"] == 2:
            raise PsdExportError("page_2_failed")
        return original_writer(*args)

    monkeypatch.setattr(psd_exporter, "_write_one", fail_second)
    with pytest.raises(PsdExportError, match="page_2_failed"):
        export_final_pages_psd(pairs, tmp_path / "out", "chapter")
    assert not (tmp_path / "out" / "chapter").exists()
    assert not list((tmp_path / "out").glob(".chapter.*"))


def test_cancel_and_collision_never_overwrite_previous_result(tmp_path):
    original = tmp_path / "original.png"
    translated = tmp_path / "translated.png"
    _image(original, color="white")
    _image(translated, color="red")
    root = tmp_path / "out"
    first = export_final_pages_psd([(original, translated)], root, "chapter")
    before = (first / "001.psd").read_bytes()
    cancelled = lambda: True
    with pytest.raises(PsdExportError, match="cancelled"):
        export_final_pages_psd([(original, translated)], root, "chapter", cancel=cancelled)
    second = export_final_pages_psd([(original, translated)], root, "chapter")
    assert first != second
    assert (first / "001.psd").read_bytes() == before
