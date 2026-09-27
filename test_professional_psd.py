"""P1/P2 professional layered PSD: Cleaned canonical + per-region raster layers."""
from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

from pathlib import Path

import cv2
import numpy as np
from psd_tools import PSDImage

from professional_psd import export_professional_psd, write_professional_page_psd

# region bboxes (x, y, w, h)
A = [80, 120, 320, 90]
B = [80, 320, 320, 90]
IGN = [80, 520, 320, 90]
C_MANUAL = [80, 720, 320, 90]


def _page(height=1200, width=900):
    white = np.full((height, width, 3), 255, dtype=np.uint8)
    original = white.copy()
    # Original carries source text in A that the pipeline will "clean".
    cv2.putText(original, "ORIG", (A[0] + 6, A[1] + 60), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 0, 0), 3)
    cleaned = white.copy()  # inpaint removed the source text -> cleaned differs from original
    translated = cleaned.copy()
    for box, txt in ((A, "AAA"), (B, "BBB"), (C_MANUAL, "CCC")):
        cv2.putText(translated, txt, (box[0] + 6, box[1] + 60), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 0, 0), 3)
    regions = [
        {"region_id": "R_A", "bbox": A, "target": "AAA", "region_type": "speech", "manual": False, "rendered": True},
        {"region_id": "R_B", "bbox": B, "target": "BBB", "region_type": "dialogue", "manual": False, "rendered": True},
        {"region_id": "R_IGN", "bbox": IGN, "target": "", "region_type": "sfx", "manual": False, "rendered": False},
        {"region_id": "manual:C", "bbox": C_MANUAL, "target": "CCC", "region_type": "narration", "manual": True, "rendered": True},
    ]
    return original, cleaned, translated, regions


def _regions_spec():
    # Matches the text _page() draws: A/B/C_MANUAL have translated glyphs, IGN is blank.
    return [
        {"region_id": "R_A", "bbox": A, "source_text": "1 AM HERE", "target": "AAA",
         "region_type": "speech", "translate_mode": "translate", "manual": False, "active": True},
        {"region_id": "R_B", "bbox": B, "source_text": "SRC B", "target": "BBB",
         "region_type": "dialogue", "translate_mode": "translate", "manual": False, "active": True},
        {"region_id": "R_IGN", "bbox": IGN, "source_text": "THUMP", "target": "",
         "region_type": "sfx", "translate_mode": "ignore", "manual": False, "active": True},
        {"region_id": "manual:C", "bbox": C_MANUAL, "source_text": "CCC SRC", "target": "CCC",
         "region_type": "narration", "translate_mode": "translate", "manual": True, "active": True},
    ]


def _export(tmp_path, **kw):
    original, cleaned, translated, _r = _page(**{k: v for k, v in kw.items() if k in ("height", "width")})
    regions = _regions_spec()
    out = export_professional_psd(
        [{"page_index": 1, "original_bgr": original, "cleaned_bgr": cleaned,
          "translated_bgr": translated, "regions": regions}],
        tmp_path / "psd", "Chapter", typesetting_mode=kw.get("mode", "on"))
    psd_file = sorted(out.glob("*.psd"))[0]
    return out, psd_file, (original, cleaned, translated, regions)


def test_layer_order_count_and_names(tmp_path):
    out, psd_file, _ = _export(tmp_path)
    psd = PSDImage.open(str(psd_file))
    names = [layer.name for layer in psd]
    assert names == ["Original", "Cleaned", "Text/Region 001", "Text/Region 002",
                     "Text/Region 003", "Translated Preview"]
    assert all(layer.kind == "pixel" for layer in psd)
    assert tuple(psd.size) == (900, 1200)


def test_ignore_excluded_and_manual_included(tmp_path):
    out, _psd_file, _ = _export(tmp_path)
    manifest = __import__("json").loads((out / "psd_manifest.json").read_text(encoding="utf-8"))
    region_layers = [l for l in manifest["pages"][0]["layers"] if l["role"] == "translated_region"]
    rids = [l["region_id"] for l in region_layers]
    assert "manual:C" in rids
    assert "R_IGN" not in rids
    assert len(region_layers) == 3
    assert any(l["manual"] for l in region_layers)


def test_effective_target_bbox_and_type_in_manifest(tmp_path):
    out, _psd_file, _ = _export(tmp_path)
    layers = __import__("json").loads((out / "psd_manifest.json").read_text(encoding="utf-8"))["pages"][0]["layers"]
    by_region = {l["region_id"]: l for l in layers if l["role"] == "translated_region"}
    assert by_region["R_A"]["effective_target"] == "AAA" and by_region["R_A"]["bbox"] == A
    assert by_region["R_B"]["region_type"] == "dialogue"
    assert by_region["manual:C"]["effective_target"] == "CCC" and by_region["manual:C"]["bbox"] == C_MANUAL


def test_cleaned_layer_has_no_translated_text_and_differs_from_original(tmp_path):
    _out, psd_file, (original, cleaned, translated, _regions) = _export(tmp_path)
    psd = PSDImage.open(str(psd_file))
    cleaned_layer = next(l for l in psd if l.name == "Cleaned").topil().convert("RGB")
    cl = np.asarray(cleaned_layer)[:, :, ::-1]  # RGB->BGR
    # Cleaned matches the cleaned input (no translated glyphs) ...
    assert np.abs(cl.astype(int) - cleaned.astype(int)).max() <= 2
    # ... and is not the Original (the source ORIG text was removed).
    x, y, w, h = A
    assert np.abs(cleaned[y:y+h, x:x+w].astype(int) - original[y:y+h, x:x+w].astype(int)).max() > 40
    # No dark translated glyphs inside a translated region on the cleaned page.
    bx, by, bw, bh = B
    assert cl[by:by+bh, bx:bx+bw].min() > 200


def test_region_layer_isolation(tmp_path):
    _out, psd_file, (original, cleaned, translated, _regions) = _export(tmp_path)
    psd = PSDImage.open(str(psd_file))
    region_layers = [l for l in psd if l.name.startswith("Text/Region")]
    assert len(region_layers) == 3
    for layer in region_layers:
        # Each region layer is bbox-scoped: its own bounds sit inside one region box,
        # never spanning the whole page or another region.
        assert layer.width < 900 and layer.height < 1200
        inside = any(layer.left >= bx and layer.top >= by
                     and layer.left + layer.width <= bx + bw + 2
                     and layer.top + layer.height <= by + bh + 2
                     for (bx, by, bw, bh) in (A, B, C_MANUAL))
        assert inside, (layer.left, layer.top, layer.width, layer.height)


def test_composite_equivalence_on_mode(tmp_path):
    # In ON mode Original + Preview are hidden by default, so the visible composite is
    # Cleaned + region layers, which must recompose the translated page.
    out, psd_file, (original, cleaned, translated, _regions) = _export(tmp_path, mode="on")
    psd = PSDImage.open(str(psd_file))
    assert next(l for l in psd if l.name == "Translated Preview").visible is False
    assert next(l for l in psd if l.name == "Original").visible is False
    composite = np.asarray(psd.composite().convert("RGB"))[:, :, ::-1]  # BGR
    diff = np.abs(composite.astype(int) - translated.astype(int)).max(axis=2)
    mismatch_ratio = float((diff > 12).mean())
    assert mismatch_ratio < 0.005, mismatch_ratio


def test_typesetting_off_layer_contract(tmp_path):
    out, psd_file, _ = _export(tmp_path, mode="off")
    psd = PSDImage.open(str(psd_file))
    assert [l.name for l in psd] == ["Original", "Cleaned"]
    assert next(l for l in psd if l.name == "Cleaned").visible is True
    manifest = __import__("json").loads((out / "psd_manifest.json").read_text(encoding="utf-8"))
    assert manifest["typesetting_mode"] == "off"
    assert manifest["translated_region_layers_included"] is False
    assert manifest["translated_preview_included"] is False


def test_typesetting_off_has_no_translated_glyphs(tmp_path):
    _out, psd_file, (original, cleaned, translated, _regions) = _export(tmp_path, mode="off")
    psd = PSDImage.open(str(psd_file))
    assert not any(l.name.startswith("Text/Region") for l in psd)
    # The visible composite (Cleaned only) has no translated glyphs in a translated region.
    composite = np.asarray(psd.composite().convert("RGB"))[:, :, ::-1]
    x, y, w, h = A
    assert composite[y:y+h, x:x+w].min() > 200  # region A stayed clean


def test_typesetting_off_manifest_preserves_region_data(tmp_path):
    out, _psd_file, _ = _export(tmp_path, mode="off")
    page = __import__("json").loads((out / "psd_manifest.json").read_text(encoding="utf-8"))["pages"][0]
    regions = {r["region_id"]: r for r in page["regions"]}
    assert set(regions) == {"R_A", "R_B", "R_IGN", "manual:C"}
    assert regions["R_A"]["target_text_effective"] == "AAA"
    assert regions["R_A"]["would_render_in_typesetting_on"] is True
    assert regions["R_IGN"]["translate_mode_effective"] == "ignore"
    assert regions["R_IGN"]["would_render_in_typesetting_on"] is False
    assert regions["manual:C"]["manual"] is True and regions["manual:C"]["source_text_effective"] == "CCC SRC"


def test_psd_tools_parse(tmp_path):
    _out, psd_file, _ = _export(tmp_path)
    psd = PSDImage.open(str(psd_file))
    assert psd.width == 900 and psd.height == 1200
    assert len(list(psd)) == 6
    assert psd.composite() is not None


def test_manifest_declares_raster_text_mode(tmp_path):
    out, _psd_file, _ = _export(tmp_path)
    manifest = __import__("json").loads((out / "psd_manifest.json").read_text(encoding="utf-8"))
    assert manifest["editable_text_supported"] is False
    assert manifest["psd_text_mode"] == "RASTER_PER_REGION"
    assert manifest["typesetting_mode"] == "on"


def test_tall_psd_15k(tmp_path):
    _out, psd_file, _ = _export(tmp_path, height=15000)
    psd = PSDImage.open(str(psd_file))
    assert psd.height == 15000 and psd.width == 900
    assert [l.name for l in psd][:2] == ["Original", "Cleaned"]


def test_tall_psd_20k(tmp_path):
    _out, psd_file, _ = _export(tmp_path, height=20000)
    psd = PSDImage.open(str(psd_file))
    assert psd.height == 20000
    assert len([l for l in psd if l.name.startswith("Text/Region")]) == 3
