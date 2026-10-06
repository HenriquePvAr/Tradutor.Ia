from io import BytesIO
import importlib
from types import SimpleNamespace

import numpy as np
import cv2
from PIL import Image, ImageDraw
import hashlib
from fastapi.testclient import TestClient

from research.gemini_vps_web_worker.storage import JobStore
from research.gemini_vps_web_worker.models import JobStatus
from research.gemini_vps_web_worker.semantic_validation import _rapidocr_records, validate_text_removal
from research.gemini_vps_web_worker.validation import training_candidate_decision, validate_and_composite
from research.gemini_vps_web_worker.browser import ProviderArtifactSelectionError, _select_fresh_generated_image
from research.gemini_vps_web_worker.authorized_mask import (
    MIN_TEXT_GEOMETRY_COVERAGE, _complete_owned_glyph_mask,
)
from research.gemini_vps_web_worker.app import _validate_v2_submission, app
from research.gemini_hard_clean_fallback.contracts import PROMPT_PROFILE, PROMPT_PROFILE_ID


def png_bytes(color=(10, 20, 30, 255), size=(8, 8)):
    buf = BytesIO()
    Image.new("RGBA", size, color).save(buf, format="PNG")
    return buf.getvalue()


def test_live_clean_prompt_matches_manual_remove_and_preserve_intent():
    assert PROMPT_PROFILE_ID == "REMOVE_ENGLISH_TEXT_PRESERVE_ART_V3"
    assert "Remova o texto em inglês" in PROMPT_PROFILE
    assert "preservando a arte original" in PROMPT_PROFILE
    assert "sem adicionar texto substituto" in PROMPT_PROFILE
    assert len(PROMPT_PROFILE) < 180


def test_queue_persists_and_claims(tmp_path):
    store = JobStore(tmp_path)
    job = store.create(BytesIO(png_bytes()), filename="original.png")
    assert job.status == "QUEUED"
    claimed = store.claim_next()
    assert claimed and claimed.job_id == job.job_id
    assert store.get(job.job_id).status == "PREPARING"


def test_zero_byte_rejected(tmp_path):
    store = JobStore(tmp_path)
    try:
        store.create(BytesIO(b""), filename="empty.png")
    except ValueError as exc:
        assert "zero-byte" in str(exc)
    else:
        raise AssertionError("zero-byte input accepted")


def test_no_mask_is_review_required(tmp_path):
    original = tmp_path / "original.png"
    raw = tmp_path / "raw.png"
    original.write_bytes(png_bytes())
    raw.write_bytes(png_bytes((200, 1, 2, 255)))
    result = validate_and_composite(original, raw, tmp_path / "final.png", None)
    assert result["quality_status"] == "REVIEW_REQUIRED_NO_AUTHORIZED_MASK"


def test_masked_composite_preserves_every_pixel_outside_authorization(tmp_path):
    original = tmp_path / "original.png"
    raw = tmp_path / "raw.png"
    mask = tmp_path / "mask.png"
    final = tmp_path / "final.png"
    Image.new("RGBA", (8, 8), (10, 20, 30, 255)).save(original)
    Image.new("RGBA", (8, 8), (200, 1, 2, 255)).save(raw)
    mask_img = Image.new("L", (8, 8), 0)
    ImageDraw.Draw(mask_img).rectangle((2, 2, 3, 3), fill=255)
    mask_img.save(mask)
    result = validate_and_composite(original, raw, final, mask)
    assert result["quality_status"] == "REVIEW_REQUIRED_PROTOCOL_IDENTITY"
    assert result["outside_mask_changed_pixels"] == 0
    assert result["raw_outside_mask_changed_pixels"] == 60


def test_fully_transparent_provider_raw_is_reviewed_and_preserves_base(tmp_path):
    original = tmp_path / "original.png"
    raw = tmp_path / "raw-transparent.png"
    mask = tmp_path / "mask.png"
    final = tmp_path / "final.png"
    Image.new("RGB", (16, 16), (30, 60, 90)).save(original)
    Image.new("RGBA", (16, 16), (0, 0, 0, 0)).save(raw)
    mask_image = Image.new("L", (16, 16), 0)
    ImageDraw.Draw(mask_image).rectangle((4, 4, 11, 11), fill=255)
    mask_image.save(mask)

    result = validate_and_composite(original, raw, final, mask)

    assert result["quality_status"] == "REVIEW_REQUIRED_RAW_OUTPUT_TRANSPARENT"
    assert result["raw_alpha"]["max"] == 0
    assert result["ocr_text_removal"]["status"] == "INCONCLUSIVE"
    assert result["outside_mask_changed_pixels"] == 0
    assert result["alignment"]["status"] == "NOT_RUN"
    assert np.array_equal(np.asarray(Image.open(final).convert("RGB")),
                          np.asarray(Image.open(original).convert("RGB")))


def test_partial_alpha_is_composited_over_base_before_authorized_mask(tmp_path):
    original = tmp_path / "original.png"
    raw = tmp_path / "raw-partial.png"
    mask = tmp_path / "mask.png"
    final = tmp_path / "final.png"
    Image.new("RGB", (16, 16), (30, 60, 90)).save(original)
    raw_image = Image.new("RGBA", (16, 16), (220, 10, 20, 0))
    raw_image.putpixel((5, 5), (220, 10, 20, 255))
    raw_image.save(raw)
    mask_image = Image.new("L", (16, 16), 0)
    ImageDraw.Draw(mask_image).rectangle((4, 4, 11, 11), fill=255)
    mask_image.save(mask)

    validate_and_composite(original, raw, final, mask)

    result = np.asarray(Image.open(final).convert("RGB"))
    assert tuple(result[5, 5]) == (220, 10, 20)
    assert tuple(result[6, 6]) == (30, 60, 90)
    assert tuple(result[1, 1]) == (30, 60, 90)


def test_provider_asset_selection_ignores_old_chat_images_and_chooses_fresh_high_res():
    baseline = {"old-image-fingerprint"}
    selected = _select_fresh_generated_image([
        {"index": 0, "fingerprint": "old-image-fingerprint", "width": 2048, "height": 2048},
        {"index": 3, "fingerprint": "new-thumbnail", "width": 512, "height": 512},
        {"index": 4, "fingerprint": "new-full-image", "width": 1024, "height": 1536},
    ], baseline)
    assert selected["fingerprint"] == "new-full-image"


def test_provider_asset_selection_fails_closed_when_no_fresh_image_exists():
    try:
        _select_fresh_generated_image([
            {"index": 0, "fingerprint": "old-image-fingerprint", "width": 1024, "height": 1536},
        ], {"old-image-fingerprint"})
    except ProviderArtifactSelectionError as exc:
        assert str(exc) == "fresh_generated_image_not_found"
    else:
        raise AssertionError("stale generated image was accepted")


def test_mask_coordinate_crop_is_normalized_and_safe(tmp_path):
    original = tmp_path / "original.png"
    raw = tmp_path / "raw.png"
    mask = tmp_path / "mask.png"
    final = tmp_path / "final.png"
    Image.new("RGBA", (8, 8), (10, 20, 30, 255)).save(original)
    raw_img = Image.new("RGBA", (8, 8), (10, 20, 30, 255))
    raw_img.putpixel((3, 3), (200, 1, 2, 255))
    raw_img.save(raw)
    Image.new("L", (2, 2), 255).save(mask)
    result = validate_and_composite(original, raw, final, mask,
                                   {"mask_coordinate_space": "CROP", "mask_crop_box": [2, 2, 2, 2]})
    assert result["quality_status"] == "REVIEW_REQUIRED_PROTOCOL_IDENTITY"
    assert result["outside_mask_changed_pixels"] == 0
    assert result["mask"]["coordinate_space"] == "CROP"


def test_full_mask_and_sfx_are_never_accepted(tmp_path):
    original = tmp_path / "original.png"
    raw = tmp_path / "raw.png"
    mask = tmp_path / "mask.png"
    Image.new("RGBA", (8, 8), (10, 20, 30, 255)).save(original)
    Image.new("RGBA", (8, 8), (200, 1, 2, 255)).save(raw)
    Image.new("L", (8, 8), 255).save(mask)
    try:
        validate_and_composite(original, raw, tmp_path / "final.png", mask)
    except ValueError as exc:
        assert str(exc) == "mask_full_image_forbidden"
    else:
        raise AssertionError("full-image authorization mask accepted")
    result = validate_and_composite(original, raw, tmp_path / "final-sfx.png", mask,
                                    {"content_type": "SFX"})
    assert result["quality_status"] == "REVIEW_REQUIRED_SFX_PROTECTED"


def test_training_candidate_policy_requires_explicit_ocr_and_quality():
    validation = {"quality_status": "ACCEPTABLE", "identity": {"schema_version": "safe-clean.v2"},
                  "outside_mask_changed_pixels": 0, "seam": {"status": "SEAM_PASS"},
                  "alignment_validation": {"status": "PASS"}, "residual": {"status": "PASS"},
                  "sfx": {"status": "PASS"}, "ocr_text_removal": {"status": "INCONCLUSIVE"}}
    blocked = training_candidate_decision(validation, {"visual_quality": "ACCEPTABLE"})
    assert blocked["training_candidate"] is False
    assert blocked["reason"] == "OCR_TEXT_REMOVAL_GATE_MISSING"
    validation["ocr_text_removal"] = {"status": "PASS"}
    accepted = training_candidate_decision(validation, {})
    assert accepted["training_candidate"] is True


def _semantic_fixture(tmp_path):
    source = tmp_path / "source.png"
    final = tmp_path / "final.png"
    Image.new("RGB", (120, 80), "white").save(source)
    final_image = Image.new("RGB", (120, 80), "white")
    ImageDraw.Draw(final_image).rectangle((40, 30, 48, 34), fill="black")
    final_image.save(final)
    mask = np.zeros((80, 120), dtype=np.uint8)
    mask[15:55, 15:105] = 255
    metadata = {"source_text": "WHY IS IT SO NOISY?", "source_text_confidence": .98,
                "source_text_provenance": "rapidocr_windows", "source_language": "en"}
    return source, final, mask, metadata


def test_region_semantic_validator_passes_only_after_two_clean_target_views(tmp_path):
    source, final, mask, metadata = _semantic_fixture(tmp_path)
    calls = iter([
        [[None, "WHY IS IT SO NOISY?", .98]],
        [[None, "WHY IS IT SO NOISY?", .96]],
        [], [],
    ])
    result = validate_text_removal(source, final, mask, metadata, ocr_engine=lambda _image: next(calls))
    assert result["status"] == "PASS"
    assert result["source_text_provenance"] == "rapidocr_windows"
    assert result["visual_residual_detector"]["views"] == 2
    assert result["visual_residual_detector"]["post_text_boxes"] == 0


def test_region_semantic_validator_rejects_partial_residual(tmp_path):
    source, final, mask, metadata = _semantic_fixture(tmp_path)
    calls = iter([
        [[None, "WHY IS IT SO NOISY?", .98]],
        [[None, "WHY IS IT SO NOISY?", .96]],
        [[None, "WHY IS IT SO", .92]], [],
    ])
    result = validate_text_removal(source, final, mask, metadata, ocr_engine=lambda _image: next(calls))
    assert result["status"] == "FAIL"
    assert "why" in result["residual_matches"]


def test_region_semantic_validator_keeps_ocr_failure_inconclusive(tmp_path):
    source, final, mask, metadata = _semantic_fixture(tmp_path)
    calls = iter([
        [[None, "WHY IS IT SO NOISY?", .98]],
        [[None, "WHY IS IT SO NOISY?", .96]],
        [], None,
    ])
    def engine(_image):
        value = next(calls)
        if value is None:
            raise RuntimeError("ocr_failed")
        return value
    result = validate_text_removal(source, final, mask, metadata, ocr_engine=engine)
    assert result["status"] == "INCONCLUSIVE"


def test_rapidocr_no_detection_tuple_is_valid_empty_result():
    image = np.full((20, 40, 3), 255, dtype=np.uint8)
    assert _rapidocr_records(image, lambda _image: (None, None)) == []


def test_clean_raw_with_rapidocr_no_detection_passes_without_mask_pixels_as_ocr(tmp_path):
    source, final, mask, metadata = _semantic_fixture(tmp_path)
    calls = iter([
        ([ [ [[0, 0], [10, 0], [10, 5], [0, 5]], "WHY IS IT SO NOISY?", .98 ] ], [0.1]),
        ([ [ [[0, 0], [10, 0], [10, 5], [0, 5]], "WHY IS IT SO NOISY?", .96 ] ], [0.1]),
        (None, None),
        (None, None),
    ])
    captured = []

    def engine(image):
        captured.append(image.copy())
        return next(calls)

    result = validate_text_removal(source, final, mask, metadata, ocr_engine=engine)
    assert result["status"] == "PASS"
    expected_final_roi = np.asarray(Image.open(final).convert("RGB"))[11:59, 11:109, ::-1]
    assert np.array_equal(captured[2], expected_final_roi)
    gray = cv2.cvtColor(expected_final_roi[:, :, ::-1], cv2.COLOR_RGB2GRAY)
    enlarged = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    expected_enhanced = cv2.cvtColor(
        cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(enlarged),
        cv2.COLOR_GRAY2BGR,
    )
    assert np.array_equal(captured[3], expected_enhanced)


def test_neighboring_sfx_outside_target_mask_does_not_enter_semantic_ocr(tmp_path):
    source, final, mask, metadata = _semantic_fixture(tmp_path)
    source_image = Image.open(source).convert("RGB")
    ImageDraw.Draw(source_image).text((1, 1), "SFX", fill="black")
    source_image.save(source)
    captured = []
    calls = iter([
        [[None, "WHY IS IT SO NOISY?", .98]],
        [[None, "WHY IS IT SO NOISY?", .96]],
        [], [],
    ])
    def engine(image):
        captured.append(image.copy())
        return next(calls)
    result = validate_text_removal(source, final, mask, metadata, ocr_engine=engine)
    assert result["status"] == "PASS"
    assert len(captured) == 4
    # OCR sees only the mask-bounded ROI, using each image's own pixels.
    assert captured[0].shape[:2] == captured[2].shape[:2]
    expected_source = np.asarray(source_image)[11:59, 11:109]
    expected_final = np.asarray(Image.open(final).convert("RGB"))[11:59, 11:109]
    assert np.array_equal(captured[0], expected_source[:, :, ::-1])
    assert np.array_equal(captured[2], expected_final[:, :, ::-1])


def test_glyph_shaped_mask_selects_roi_without_repainting_clean_ocr_pixels(tmp_path):
    source = tmp_path / "source-glyphs.png"
    final = tmp_path / "final-clean.png"
    source_pixels = np.full((80, 120, 3), 255, dtype=np.uint8)
    final_pixels = np.full((80, 120, 3), 248, dtype=np.uint8)
    # Binary mask follows letter-like strokes; it must never become OCR pixels.
    mask = np.zeros((80, 120), dtype=np.uint8)
    mask[25:29, 25:65] = 255
    mask[25:50, 25:29] = 255
    source_pixels[mask > 0] = 0
    Image.fromarray(source_pixels).save(source)
    Image.fromarray(final_pixels).save(final)
    captured = []
    calls = iter([
        [[None, "MY FRIEND FINALLY BECAME", .98]],
        [[None, "MY FRIEND FINALLY BECAME", .96]],
        [], [],
    ])

    def engine(image):
        captured.append(image.copy())
        return next(calls)

    metadata = {"source_text": "MY FRIEND FINALLY BECAME", "source_text_confidence": .98,
                "source_text_provenance": "rapidocr_windows", "source_language": "en"}
    result = validate_text_removal(source, final, mask, metadata, ocr_engine=engine)

    assert result["status"] == "PASS"
    assert len(captured) == 4
    ys, xs = np.where(mask > 0)
    pad = max(4, int(round(min(source_pixels.shape[:2]) * .004)))
    x0, x1 = max(0, int(xs.min()) - pad), min(120, int(xs.max()) + 1 + pad)
    y0, y1 = max(0, int(ys.min()) - pad), min(80, int(ys.max()) + 1 + pad)
    expected_source = cv2.cvtColor(source_pixels[y0:y1, x0:x1], cv2.COLOR_RGB2BGR)
    expected_final = cv2.cvtColor(final_pixels[y0:y1, x0:x1], cv2.COLOR_RGB2BGR)
    assert np.array_equal(captured[0], expected_source)
    assert np.array_equal(captured[2], expected_final)
    assert np.all(captured[2] == 248)
    assert not np.array_equal(captured[2], cv2.cvtColor(
        np.repeat(mask[y0:y1, x0:x1, None], 3, axis=2), cv2.COLOR_RGB2BGR))


def test_safe_clean_v2_submission_requires_hard_region_source_provenance_and_exact_hashes():
    image_bytes = png_bytes(size=(32, 24))
    mask_io = BytesIO()
    Image.new("L", (32, 24), 0).save(mask_io, format="PNG")
    mask_image = Image.open(BytesIO(mask_io.getvalue()))
    ImageDraw.Draw(mask_image).rectangle((5, 5, 18, 14), fill=255)
    encoded_mask = BytesIO()
    mask_image.save(encoded_mask, format="PNG")
    mask_bytes = encoded_mask.getvalue()
    metadata = {
        "schema_version": "safe-clean.v2", "request_id": "request-1",
        "parent_job_id": "job-1", "run_id": "run-1", "page_id": "4",
        "region_ids": ["region-4-a"], "source_sha256": hashlib.sha256(image_bytes).hexdigest(),
        "base_sha256": "a" * 64, "mask_sha256": hashlib.sha256(mask_bytes).hexdigest(),
        "source_text": "WHY IS IT SO NOISY?", "source_text_confidence": .98,
        "source_text_provenance": "rapidocr_windows", "source_language": "eng",
        "translation_enabled": False, "disposition": "LOCAL_HARD",
        "ownership_confirmed": True,
        "local_clean_attempted": True, "local_validation": {"clean_validation_passed": False},
        "mask_coverage": {"status": "PASS", "geometry_kind": "owned_components_plus_supported_light_outline",
                          "coverage_ratio": .99, "minimum_coverage_ratio": .98,
                          "region_id": "region-4-a",
                          "target_group_index": 0, "owned_group_id": "owned-group-1",
                          "groups": [{"status": "PASS", "coverage_ratio": .99,
                                      "lines": [{"status": "PASS", "coverage_ratio": .99}]}]},
    }
    assert _validate_v2_submission(metadata, job_id="request-1", page_id="4", region_id="region-4-a",
                                   content_type="narration", image_bytes=image_bytes, mask_bytes=mask_bytes)
    invalid = dict(metadata, source_sha256="0" * 64)
    assert not _validate_v2_submission(invalid, job_id="request-1", page_id="4", region_id="region-4-a",
                                       content_type="narration", image_bytes=image_bytes, mask_bytes=mask_bytes)
    assert not _validate_v2_submission(dict(metadata, disposition="REVIEW_UNOWNED"), job_id="request-1",
                                       page_id="4", region_id="region-4-a", content_type="narration",
                                       image_bytes=image_bytes, mask_bytes=mask_bytes)
    assert not _validate_v2_submission(dict(metadata, translation_enabled=True), job_id="request-1",
                                       page_id="4", region_id="region-4-a", content_type="narration",
                                       image_bytes=image_bytes, mask_bytes=mask_bytes)
    assert not _validate_v2_submission(dict(metadata, source_text_confidence=float("nan")),
                                       job_id="request-1", page_id="4", region_id="region-4-a",
                                       content_type="narration", image_bytes=image_bytes, mask_bytes=mask_bytes)
    assert not _validate_v2_submission(dict(metadata, ownership_confirmed=False),
                                       job_id="request-1", page_id="4", region_id="region-4-a",
                                       content_type="narration", image_bytes=image_bytes, mask_bytes=mask_bytes)
    assert not _validate_v2_submission(dict(metadata, local_clean_attempted=False),
                                       job_id="request-1", page_id="4", region_id="region-4-a",
                                       content_type="narration", image_bytes=image_bytes, mask_bytes=mask_bytes)
    assert not _validate_v2_submission(dict(metadata, mask_coverage={**metadata["mask_coverage"],
                                                                       "status": "FAIL"}),
                                       job_id="request-1", page_id="4", region_id="region-4-a",
                                       content_type="narration", image_bytes=image_bytes, mask_bytes=mask_bytes)
    assert not _validate_v2_submission(dict(metadata, mask_coverage={**metadata["mask_coverage"],
                                                                       "coverage_ratio": .90,
                                                                       "groups": [{"status": "FAIL", "coverage_ratio": .90,
                                                                                   "lines": [{"status": "FAIL", "coverage_ratio": .90}]}]}),
                                       job_id="request-1", page_id="4", region_id="region-4-a",
                                       content_type="narration", image_bytes=image_bytes, mask_bytes=mask_bytes)
    assert not _validate_v2_submission({key: value for key, value in metadata.items() if key != "mask_coverage"},
                                       job_id="request-1", page_id="4", region_id="region-4-a",
                                       content_type="narration", image_bytes=image_bytes, mask_bytes=mask_bytes)
    partial_lines = {**metadata["mask_coverage"], "groups": [{"status": "PASS", "coverage_ratio": .99,
                                                               "lines": [{"status": "FAIL", "coverage_ratio": .90}]}]}
    assert not _validate_v2_submission(dict(metadata, mask_coverage=partial_lines),
                                       job_id="request-1", page_id="4", region_id="region-4-a",
                                       content_type="narration", image_bytes=image_bytes, mask_bytes=mask_bytes)


def _dense_multiline_group():
    line_1 = SimpleNamespace(box=(10, 10, 100, 24), polygon=np.array([[10, 10], [109, 10], [109, 33], [10, 33]]))
    line_2 = SimpleNamespace(box=(18, 42, 92, 24), polygon=np.array([[18, 42], [109, 42], [109, 65], [18, 65]]))
    return SimpleNamespace(lines=[line_1, line_2], cleanup_lines=[])


def _neighbor_group():
    line_1 = SimpleNamespace(box=(165, 80, 70, 20), polygon=np.array([[165, 80], [234, 80], [234, 99], [165, 99]]),
                             text="NEIGHBOR", confidence=.99)
    return SimpleNamespace(lines=[line_1], cleanup_lines=[], text="NEIGHBOR", classification="narration",
                           classification_confidence=.99, box=(165, 80, 70, 20))


def test_authorized_mask_expands_multiline_owned_text_to_bounded_outline_geometry():
    group = _dense_multiline_group()
    core = np.zeros((80, 140), dtype=np.uint8)
    core[11:33, 11:109] = 255
    core[43:65, 19:109] = 255
    image = np.full((80, 140, 3), 245, dtype=np.uint8)
    image[core > 0] = 20
    complete, coverage = _complete_owned_glyph_mask(image, group, core)

    assert coverage["status"] == "PASS"
    assert coverage["coverage_ratio"] >= MIN_TEXT_GEOMETRY_COVERAGE
    assert coverage["padding_px"] == 1
    assert np.all(complete[core > 0] == 255)
    assert np.count_nonzero(complete) < 2 * np.count_nonzero(core)
    assert np.count_nonzero(complete & ~core) > 0  # stroke/outline margin is included


def test_authorized_mask_safety_boundary_clips_sfx_and_neighbor_and_fails_coverage_gate():
    group = _dense_multiline_group()
    core = np.zeros((80, 140), dtype=np.uint8)
    core[11:33, 11:109] = 255
    core[43:65, 19:109] = 255
    image = np.full((80, 140, 3), 245, dtype=np.uint8)
    image[core > 0] = 20
    protected = np.zeros_like(core)
    protected[10:25, 10:25] = 255  # an adjacent SFX/neighbor-owned segment
    complete, coverage = _complete_owned_glyph_mask(image, group, core,
                                                     protected_mask=protected)

    assert not np.any((complete > 0) & (protected > 0))
    assert coverage["clipped_by_safety_boundary"] is True
    assert coverage["status"] == "FAIL"


def test_authorized_mask_generator_persists_coverage_gate_and_blocks_incomplete_mask(tmp_path, monkeypatch):
    from research.gemini_vps_web_worker import authorized_mask

    class FakeEngine:
        def __init__(self, _language):
            pass

        def detect_lines(self, _image):
            return [object()]

    group = _dense_multiline_group()
    group.text = "FIRST LINE SECOND LINE"
    group.classification = "narration"
    group.classification_confidence = .99
    group.box = (10, 10, 100, 56)
    group.lines[0].text = "FIRST LINE"
    group.lines[0].confidence = .99
    group.lines[1].text = "SECOND LINE"
    group.lines[1].confidence = .99
    monkeypatch.setattr(authorized_mask, "OCREngine", FakeEngine)
    monkeypatch.setattr(authorized_mask.yomu_ocr, "analyze_image_array", lambda _image, _lines: ([], [group]))
    monkeypatch.setattr(authorized_mask.yomu_ocr, "_build_text_mask",
                        lambda shape, _groups, padding=None: np.ones(shape[:2], dtype=np.uint8) * 255)
    image_path = tmp_path / "source.png"
    cv2.imwrite(str(image_path), np.full((240, 300, 3), 245, dtype=np.uint8))

    dense = np.zeros((240, 300), dtype=np.uint8)
    dense[11:33, 11:109] = 255
    dense[43:65, 19:109] = 255
    monkeypatch.setattr(authorized_mask.yomu_ocr, "_component_text_mask",
                        lambda *_args, **_kwargs: (dense.copy(), {"accepted_text_components": 2}))
    pass_meta = authorized_mask.generate_authorized_mask(image_path, tmp_path / "mask-pass.png",
                                                         page_id="053", region_id="REGION_001")
    assert pass_meta["status"] == "PASS"
    assert pass_meta["mask_coverage"]["status"] == "PASS"
    assert pass_meta["mask_coverage"]["region_id"] == "REGION_001"
    assert pass_meta["mask_coverage"]["target_group_index"] == 0
    assert pass_meta["mask_coverage"]["owned_group_id"]
    assert pass_meta["mask_coverage"]["coverage_ratio"] >= MIN_TEXT_GEOMETRY_COVERAGE

    sparse = np.zeros_like(dense)
    sparse[20, 20] = 255
    monkeypatch.setattr(authorized_mask.yomu_ocr, "_component_text_mask",
                        lambda *_args, **_kwargs: (sparse.copy(), {"accepted_text_components": 1}))
    fail_meta = authorized_mask.generate_authorized_mask(image_path, tmp_path / "mask-fail.png",
                                                         page_id="053", region_id="REGION_001")
    assert fail_meta["status"] == "REVIEW_REQUIRED_MASK_UNCERTAIN"
    assert fail_meta["mask_path"] is None
    assert fail_meta["mask_coverage"]["status"] == "FAIL"


def test_authorized_mask_requires_group_ownership_and_keeps_neighbor_story_group_out(tmp_path, monkeypatch):
    from research.gemini_vps_web_worker import authorized_mask

    class FakeEngine:
        def __init__(self, _language):
            pass

        def detect_lines(self, _image):
            return [object()]

    target = _dense_multiline_group()
    target.text = "TARGET STORY TEXT"
    target.classification = "narration"
    target.classification_confidence = .99
    target.box = (10, 10, 100, 56)
    for line, text in zip(target.lines, ["TARGET STORY", "TEXT"]):
        line.text = text
        line.confidence = .99
    neighbor = _neighbor_group()
    monkeypatch.setattr(authorized_mask, "OCREngine", FakeEngine)
    monkeypatch.setattr(authorized_mask.yomu_ocr, "analyze_image_array", lambda _image, _lines: ([], [target, neighbor]))
    monkeypatch.setattr(authorized_mask.yomu_ocr, "_build_text_mask",
                        lambda shape, _groups, padding=None: np.ones(shape[:2], dtype=np.uint8) * 255)
    dense = np.zeros((240, 300), dtype=np.uint8)
    dense[11:33, 11:109] = 255
    dense[43:65, 19:109] = 255
    monkeypatch.setattr(authorized_mask.yomu_ocr, "_component_text_mask",
                        lambda *_args, **_kwargs: (dense.copy(), {"accepted_text_components": 2}))
    image_path = tmp_path / "source.png"
    cv2.imwrite(str(image_path), np.full((240, 300, 3), 245, dtype=np.uint8))

    ambiguous = authorized_mask.generate_authorized_mask(image_path, tmp_path / "ambiguous.png",
                                                         region_id="target-region")
    assert ambiguous["status"] == "REVIEW_REQUIRED_MASK_UNCERTAIN"
    assert ambiguous["mask_path"] is None
    assert ambiguous["blocked_regions"][0]["reason"] == "TARGET_GROUP_OWNERSHIP_REQUIRED"

    selected = authorized_mask.generate_authorized_mask(image_path, tmp_path / "target-only.png",
                                                         region_id="target-region", target_group_index=0)
    assert selected["status"] == "PASS"
    page_mask = cv2.imread(selected["mask_path"], cv2.IMREAD_GRAYSCALE)
    assert not np.any(page_mask[80:100, 165:235])


def test_safe_clean_schema_probe_is_read_only():
    response = TestClient(app).get("/v1/hard-clean/schema")
    assert response.status_code == 200
    assert response.json()["schema_version"] == "safe-clean.v2"
    assert response.json()["translation_enabled"] is False
    assert response.json()["ocr_text_removal_states"] == ["PASS", "FAIL", "INCONCLUSIVE"]
    assert "authorized_mask_text_geometry_coverage" in response.json()["required_validation"]


def test_worker_quality_pass_requires_semantic_removal_and_exact_identity(tmp_path):
    original = tmp_path / "source.png"
    raw = tmp_path / "raw.png"
    mask_path = tmp_path / "mask.png"
    final = tmp_path / "final.png"
    source_image = Image.new("RGB", (180, 70), "white")
    ImageDraw.Draw(source_image).text((15, 22), "WHY IS IT SO NOISY?", fill="black")
    source_image.save(original)
    Image.new("RGB", (180, 70), "white").save(raw)
    mask_image = Image.new("L", (180, 70), 0)
    ImageDraw.Draw(mask_image).rectangle((8, 18, 160, 39), fill=255)
    mask_image.save(mask_path)
    metadata = {
        "schema_version": "safe-clean.v2", "request_id": "req-1", "parent_job_id": "job-1",
        "run_id": "run-1", "page_id": "1", "region_ids": ["r-1"],
        "source_sha256": __import__("research.gemini_vps_web_worker.validation", fromlist=["sha256_file"]).sha256_file(original),
        "base_sha256": "a" * 64,
        "mask_sha256": __import__("research.gemini_vps_web_worker.validation", fromlist=["sha256_file"]).sha256_file(mask_path),
        "source_text": "WHY IS IT SO NOISY?", "source_text_confidence": .98,
        "source_text_provenance": "rapidocr_windows", "source_language": "eng",
        "ownership_confirmed": True, "local_clean_attempted": True,
    }
    calls = iter([
        [[None, "WHY IS IT SO NOISY?", .98]], [[None, "WHY IS IT SO NOISY?", .96]], [], [],
    ])
    result = validate_and_composite(original, raw, final, mask_path, metadata,
                                    semantic_ocr_engine=lambda _image: next(calls))
    assert result["quality_status"] == "ACCEPTABLE"
    assert result["outside_mask_changed_pixels"] == 0
    assert result["ocr_text_removal"]["status"] == "PASS"
    assert result["identity"]["request_id"] == "req-1"


def test_worker_rejects_partial_source_text_even_when_composite_is_geometrically_safe(tmp_path):
    original = tmp_path / "source.png"
    raw = tmp_path / "raw.png"
    mask_path = tmp_path / "mask.png"
    final = tmp_path / "final.png"
    source_image = Image.new("RGB", (180, 70), "white")
    ImageDraw.Draw(source_image).text((15, 22), "WHY IS IT SO NOISY?", fill="black")
    source_image.save(original)
    raw_image = Image.new("RGB", (180, 70), "white")
    ImageDraw.Draw(raw_image).text((15, 22), "WHY IS IT SO", fill="black")
    raw_image.save(raw)
    mask_image = Image.new("L", (180, 70), 0)
    ImageDraw.Draw(mask_image).rectangle((8, 18, 160, 39), fill=255)
    mask_image.save(mask_path)
    from research.gemini_vps_web_worker.validation import sha256_file
    metadata = {
        "schema_version": "safe-clean.v2", "request_id": "req-2", "parent_job_id": "job-1",
        "run_id": "run-1", "page_id": "1", "region_ids": ["r-1"],
        "source_sha256": sha256_file(original), "base_sha256": "b" * 64,
        "mask_sha256": sha256_file(mask_path), "source_text": "WHY IS IT SO NOISY?",
        "source_text_confidence": .98, "source_text_provenance": "rapidocr_windows",
        "source_language": "eng",
    }
    calls = iter([
        [[None, "WHY IS IT SO NOISY?", .98]], [[None, "WHY IS IT SO NOISY?", .96]],
        [[None, "WHY IS IT SO", .94]], [],
    ])
    result = validate_and_composite(original, raw, final, mask_path, metadata,
                                    semantic_ocr_engine=lambda _image: next(calls))
    assert result["outside_mask_changed_pixels"] == 0
    assert result["ocr_text_removal"]["status"] == "FAIL"
    assert result["quality_status"] == "REJECTED_RESIDUAL_SOURCE_TEXT"


def test_raw_evidence_route_is_declared_loopback_only():
    paths = app.openapi()["paths"]
    assert "/v1/hard-clean/jobs/{job_id}/raw" in paths
    response = TestClient(app).get("/v1/hard-clean/jobs/not-a-job/raw")
    assert response.status_code == 403


def test_raw_and_result_routes_return_distinct_persisted_artifact_fields(tmp_path, monkeypatch):
    module = importlib.import_module("research.gemini_vps_web_worker.app")
    store = JobStore(tmp_path / "store")
    job = store.create(BytesIO(png_bytes()), filename="original.png")
    raw = tmp_path / "captured-gemini-raw.png"
    final = tmp_path / "safe-composite.png"
    raw.write_bytes(png_bytes((1, 2, 3, 255)))
    final.write_bytes(png_bytes((4, 5, 6, 255)))
    store.update(job, JobStatus.REVIEW_REQUIRED, raw_path=str(raw), final_path=str(final))
    monkeypatch.setattr(module, "store", store)

    from starlette.requests import Request
    request = Request({"type": "http", "client": ("127.0.0.1", 12345)})
    raw_response = module.get_raw_result(job.job_id, request)
    result_response = module.get_result(job.job_id)

    assert raw_response.path == str(raw)
    assert result_response.path == str(final)
    assert raw_response.path != result_response.path
