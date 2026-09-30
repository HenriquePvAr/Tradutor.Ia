"""Generic local-background reconstruction tests, independent of any chapter."""

import cv2
import numpy as np

import ocr_balloon as ob
from ocr_engine import OCRLine


def _group(box=(40, 40, 140, 40)):
    x, y, w, h = box
    polygon = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.int32)
    line = OCRLine(
        text="SOURCE TEXT",
        raw_text="SOURCE TEXT",
        confidence=0.99,
        polygon=polygon,
        box=box,
        engine="synthetic",
        page=1,
    )
    group = ob.TextGroup(group_id="local-background", lines=[line], text=line.text)
    group.classification = "speech"
    return group


def _mask(shape=(120, 220), box=(70, 50, 70, 20)):
    x, y, w, h = box
    result = np.zeros(shape, dtype=np.uint8)
    result[y:y + h, x:x + w] = 255
    return result


def _apply(fill, *, profile="solid_colored", text_fill=(245, 245, 245)):
    image = np.full((120, 220, 3), fill, dtype=np.uint8)
    mask = _mask()
    image[mask > 0] = text_fill
    group = _group()
    group.background_type = "textured_art"
    group.background_metrics = {
        "local_background_class": profile,
        "local_background_median_bgr": list(fill),
        "local_background_sample_count": 100,
    }
    return image, mask, group


def test_local_profile_preserves_light_dark_gray_and_chroma_without_defaults():
    for fill, text in [
        ((255, 255, 255), (20, 20, 20)),
        ((8, 8, 8), (245, 245, 245)),
        ((122, 122, 122), (245, 245, 245)),
        ((185, 55, 35), (245, 245, 245)),
        ((30, 210, 90), (20, 20, 20)),
    ]:
        image, mask, group = _apply(fill, text_fill=text)
        cleaned = ob._apply_cleanup_mask(image, image, group, mask)
        error = np.abs(cleaned[mask > 0].astype(np.int16) - np.asarray(fill, dtype=np.int16))
        assert float(np.percentile(error, 99)) <= 1.0


def test_estimator_classifies_local_colored_and_dark_contexts():
    for fill, expected in [
        ((245, 245, 245), "solid_light"),
        ((12, 12, 12), "solid_dark"),
        ((35, 110, 185), "solid_colored"),
    ]:
        image = np.full((120, 220, 3), fill, dtype=np.uint8)
        group = _group()
        mask = _mask()
        profile = ob._estimate_local_background_profile(
            image, group, mask, (40, 40, 140, 40)
        )
        assert profile["local_background_class"] == expected


def test_gradient_profile_is_not_flattened_to_a_fixed_color():
    image = np.zeros((120, 220, 3), dtype=np.uint8)
    for row in range(image.shape[0]):
        value = 35 + int(180 * row / (image.shape[0] - 1))
        image[row, :, :] = value
    mask = _mask()
    image[mask > 0] = (250, 250, 250)
    group = _group()
    group.background_type = "textured_art"
    group.background_metrics = {
        "local_background_class": "gradient",
        "local_background_median_bgr": [120, 120, 120],
        "local_background_sample_count": 100,
    }
    cleaned = ob._apply_cleanup_mask(image, image, group, mask)
    values = cleaned[mask > 0][:, 0]
    assert int(values.max()) - int(values.min()) > 10


def test_local_color_mismatch_is_reported_by_cleanup_metrics():
    original, mask, group = _apply((30, 210, 90))
    cleaned = original.copy()
    cleaned[mask > 0] = (255, 255, 255)
    metrics = ob._local_background_reconstruction_metrics(original, cleaned, group, mask)
    assert metrics["local_background_color_mismatch"] is True

