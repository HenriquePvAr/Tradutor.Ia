"""CLEAN is source-removal evidence, independent from target renderability."""
from __future__ import annotations

from unittest import mock

import cv2
import numpy as np

import yomu.ocr.ocr_balloon as ob
import yomu.translation.semantic_fidelity as semantic_fidelity
from yomu.ocr.ocr_engine import OCRLine


def _unusable_group():
    polygon = np.array([[30, 40], [210, 40], [210, 90], [30, 90]], dtype=np.int32)
    line = OCRLine(
        text="SOURCE SENTENCE",
        raw_text="SOURCE SENTENCE",
        confidence=0.99,
        polygon=polygon,
        box=(30, 40, 180, 50),
        engine="synthetic",
        page=1,
    )
    group = ob.TextGroup(group_id="synthetic-region", lines=[line], text=line.text)
    group.classification = "speech"
    group.source_engine = "synthetic"
    group.region_id = "synthetic-region"
    group.sent_to_translation = True
    group.translation = "TARGET"
    group.translation_candidate = "TARGET"
    group.translation_valid = False
    group.translation_final_state = "rejected"
    group.translation_final_reason = "semantic_fidelity_failed"
    group.semantic_review_reason = semantic_fidelity.SOURCE_SEGMENTATION_INCOMPLETE
    return group


def test_unrenderable_target_still_produces_clean_artifact_without_typography():
    original = np.full((140, 260, 3), 235, dtype=np.uint8)
    source = original.copy()
    cv2.putText(source, "SOURCE", (35, 76), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (10, 10, 10), 2)
    group = _unusable_group()
    mask = np.zeros(source.shape[:2], dtype=np.uint8)
    mask[40:90, 30:210] = 255
    clean = source.copy()
    clean[mask > 0] = (235, 235, 235)

    with (
        mock.patch.object(
            ob,
            "_remove_text_for_group",
            return_value=(source.copy(), mask, {"mask_valid": True}),
        ) as remove,
        mock.patch.object(ob, "_apply_cleanup_mask", return_value=clean),
        mock.patch.object(
            ob,
            "_uncovered_source_text_evidence",
            return_value={"measured": True, "source_text_coverage": 0.0},
        ),
        mock.patch.object(ob, "_draw_group_translation") as draw,
    ):
        capture = {}
        rendered, _ = ob.render_analyzed_image(source, group.lines, [], [group], capture=capture)

    assert np.array_equal(rendered, source), "unusable target must not be drawn"
    assert not np.array_equal(capture["cleaned_bgr"], source)
    assert group.clean_artifact_validation["clean_validation_passed"] is True
    remove.assert_called()
    draw.assert_not_called()


def test_local_cleanup_preserves_dark_and_colored_solid_backgrounds():
    for fill, background_type in (((8, 8, 8), "dark_balloon"), ((35, 110, 185), "narration_box")):
        original = np.full((120, 220, 3), fill, dtype=np.uint8)
        group = _unusable_group()
        group.background_type = background_type
        group.background_metrics = {
            "uniform_light": False,
            "open_dark_narration": background_type == "dark_balloon",
            "dark_context": background_type == "dark_balloon",
        }
        mask = np.zeros(original.shape[:2], dtype=np.uint8)
        mask[45:80, 40:180] = 255
        result = ob._apply_cleanup_mask(original.copy(), original, group, mask)
        error = np.abs(
            result[mask > 0].astype(np.int16) - np.asarray(fill, dtype=np.int16)
        )
        assert float(error.mean()) <= 3
        assert float(np.percentile(error, 99)) <= 8
