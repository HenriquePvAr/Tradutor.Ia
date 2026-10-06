import base64
import json
from pathlib import Path

import pytest
from PIL import Image

from research.gemini_hard_clean_fallback.transport import (
    copy_to_short_path,
    decode_data_url,
    decode_output_bytes,
    inspect_image,
    normalize_image_for_gemini,
)


def test_valid_png_and_jpeg_diagnostics(tmp_path):
    png = tmp_path / "with space.png"
    jpg = tmp_path / "unicode-ação.jpg"
    Image.new("RGBA", (8, 6), (1, 2, 3, 120)).save(png)
    Image.new("RGB", (8, 6), (4, 5, 6)).save(jpg)
    assert inspect_image(png).pil_decode == "PASS"
    assert inspect_image(png).alpha_present is True
    assert inspect_image(jpg).format == "JPEG"


def test_normalization_strips_alpha_and_is_stable(tmp_path):
    source = tmp_path / "source.webp"
    Image.new("RGBA", (9, 7), (20, 30, 40, 80)).save(source, format="WEBP")
    first = normalize_image_for_gemini(source, tmp_path / "normalized")
    second = inspect_image(first.path)
    assert first.pil_decode == second.pil_decode == "PASS"
    assert second.mode == "RGB"
    assert second.alpha_present is False
    assert first.sha256 == second.sha256


def test_corrupt_and_zero_byte_files_fail_closed(tmp_path):
    corrupt = tmp_path / "corrupt.png"
    corrupt.write_bytes(b"not an image")
    zero = tmp_path / "zero.png"
    zero.write_bytes(b"")
    assert inspect_image(corrupt).pil_decode == "FAIL"
    assert inspect_image(zero).pil_decode == "FAIL"
    with pytest.raises(ValueError):
        decode_output_bytes(b"", tmp_path / "out.png", source="DOWNLOAD")


def test_short_path_and_output_recovery(tmp_path):
    source = tmp_path / ("long-ish path " + "x" * 20 + ".png")
    Image.new("RGB", (5, 5), "white").save(source)
    short, diagnostics = copy_to_short_path(source, tmp_path / "short", "job-1")
    assert short.name == "job-1.png"
    assert diagnostics.pil_decode == "PASS"
    raw = (tmp_path / "raw.png")
    data = source.read_bytes()
    assert decode_output_bytes(data, raw, source="DOWNLOAD").pil_decode == "PASS"


def test_data_url_recovery(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (3, 4), "black").save(source)
    url = "data:image/png;base64," + base64.b64encode(source.read_bytes()).decode()
    assert decode_data_url(url, tmp_path / "data.png").width == 3

