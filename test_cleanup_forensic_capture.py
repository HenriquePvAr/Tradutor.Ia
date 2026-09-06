"""Opt-in cleanup forensic capture contracts."""
from __future__ import annotations

import _test_bootstrap  # noqa: F401

import json
from pathlib import Path

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import cv2
import numpy as np

import ocr_balloon as ob
from ocr_engine import OCRLine


def _fixture():
    image = np.zeros((180, 320, 3), dtype=np.uint8)
    image[:] = (120, 145, 170)
    cv2.line(image, (0, 20), (319, 160), (75, 95, 120), 2)
    cv2.putText(image, "ART TEXT", (35, 105), cv2.FONT_HERSHEY_SIMPLEX, 1.4,
                (20, 25, 30), 4, cv2.LINE_AA)
    line = OCRLine(
        text="THIS IS ART TEXT",
        confidence=0.98,
        polygon=np.array([[28, 65], [230, 65], [230, 120], [28, 120]], dtype=np.int32),
        box=(28, 65, 202, 55),
        raw_text="THIS IS ART TEXT",
        engine="synthetic",
        page=1,
        metadata={"ocr_line_id": "L1"},
    )
    group = ob.TextGroup(group_id="G1", lines=[line], text="THIS IS ART TEXT", translation="ISTO E TEXTO")
    group.classification = "speech"
    group.source_completeness = {"status": "pass"}
    group.region_id = "R1"
    group.source_engine = "synthetic"
    return image, group


def test_capture_disabled_is_inert(tmp_path):
    image, group = _fixture()
    baseline, baseline_mask, baseline_metrics = ob._remove_text_for_group(
        image.copy(), image, group, strategy="source_scoped")
    assert list(tmp_path.iterdir()) == []
    assert baseline_mask is not None
    assert baseline_metrics["mask_valid"] is True


def test_capture_on_persists_exact_cleanup_arrays(tmp_path):
    image, group = _fixture()
    capture = ob.CleanupForensicCapture(tmp_path)
    captured, runtime_mask, metrics = ob._remove_text_for_group(
        image.copy(), image, group, strategy="source_scoped",
        forensic_capture=capture)
    assert metrics["mask_valid"] is True
    directory = tmp_path / "cleanup" / "page_1_G1_source_scoped"
    assert (directory / "metadata.json").exists()
    assert (directory / "mask.png").exists()
    assert (directory / "pre_cleanup.png").exists()
    assert (directory / "post_cleanup.png").exists()
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    persisted_mask = cv2.imread(str(directory / "mask.png"), cv2.IMREAD_UNCHANGED)
    persisted_pre = cv2.imread(str(directory / "pre_cleanup.png"), cv2.IMREAD_UNCHANGED)
    persisted_post = cv2.imread(str(directory / "post_cleanup.png"), cv2.IMREAD_UNCHANGED)
    assert np.array_equal(persisted_mask, runtime_mask)
    assert np.array_equal(persisted_pre, image)
    assert np.array_equal(persisted_post, captured)
    assert metadata["mask"]["nonzero_pixel_count"] == int(np.count_nonzero(runtime_mask))
    assert metadata["ocr_lines"][0]["polygon"] == group.lines[0].polygon.tolist()
    assert metadata["pre_typography_distinct"] is False


def test_capture_on_is_pixel_neutral(tmp_path):
    image_a, group_a = _fixture()
    image_b, group_b = _fixture()
    clean_a, mask_a, metrics_a = ob._remove_text_for_group(
        image_a.copy(), image_a, group_a, strategy="source_scoped")
    clean_b, mask_b, metrics_b = ob._remove_text_for_group(
        image_b.copy(), image_b, group_b, strategy="source_scoped",
        forensic_capture=ob.CleanupForensicCapture(tmp_path))
    assert np.array_equal(mask_a, mask_b)
    assert np.array_equal(clean_a, clean_b)
    assert metrics_a["mask_valid"] == metrics_b["mask_valid"]
    assert metrics_a.get("reason", "") == metrics_b.get("reason", "")


def test_capture_write_failure_does_not_escape(tmp_path, monkeypatch):
    image, group = _fixture()
    capture = ob.CleanupForensicCapture(tmp_path / "capture")
    monkeypatch.setattr(cv2, "imwrite", lambda *args, **kwargs: False)
    cleaned, mask, metrics = ob._remove_text_for_group(
        image.copy(), image, group, strategy="source_scoped",
        forensic_capture=capture)
    assert metrics["mask_valid"] is True
    assert mask is not None
    assert cleaned.shape == image.shape
