from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import cv2
from PIL import Image, ImageChops

from research.gemini_hard_clean_fallback.transport import inspect_image, normalize_image_for_gemini
from .semantic_validation import validate_text_removal

MAX_MASK_RATIO = 0.34
SEAM_PASS_THRESHOLD = 0.80


def training_candidate_decision(validation: dict[str, Any], metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply the frozen R&D dataset promotion policy, fail-closed."""
    metadata = metadata if isinstance(metadata, dict) else {}
    quality = str(validation.get("quality_status") or "").upper()
    semantic = validation.get("ocr_text_removal")
    ocr = str(semantic.get("status") or "").upper() if isinstance(semantic, dict) else ""
    checks = {
        "quality": quality == "ACCEPTABLE",
        "identity": isinstance(validation.get("identity"), dict)
            and validation["identity"].get("schema_version") == "safe-clean.v2",
        "seam": validation.get("seam", {}).get("status") == "SEAM_PASS",
        "outside_mask": validation.get("outside_mask_changed_pixels") == 0,
        "alignment": validation.get("alignment_validation", {}).get("status") == "PASS",
        "residual": validation.get("residual", {}).get("status") == "PASS",
        "ocr_text_removal": ocr == "PASS",
        "sfx": validation.get("sfx", {}).get("status") == "PASS",
    }
    candidate = all(checks.values())
    return {"training_candidate": candidate, "checks": checks,
            "reason": "ALL_FROZEN_GATES_PASS" if candidate else next(
                (name.upper() + "_GATE_MISSING" for name, ok in checks.items() if not ok), "UNKNOWN")}

def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def validate_input(source: str | Path, normalized_dir: str | Path):
    diagnostics = inspect_image(source)
    if diagnostics.pil_decode != "PASS" or diagnostics.file_size_bytes <= 0:
        raise ValueError(f"invalid input: {diagnostics.to_dict()}")
    return diagnostics, normalize_image_for_gemini(source, normalized_dir)

def _sfx_block_reason(metadata: dict[str, Any]) -> str | None:
    content_type = str(metadata.get("content_type") or "").upper()
    classification = str(metadata.get("classification") or metadata.get("mask_classification") or "").lower()
    if content_type == "SFX" or classification == "sfx" or metadata.get("erase_eligible", True) is False:
        return "REVIEW_REQUIRED_SFX_PROTECTED"
    return None

def _page_mask(mask_path: str | Path, page_size: tuple[int, int], metadata: dict[str, Any]):
    with Image.open(mask_path) as source:
        mask = source.convert("L")
        coordinate_space = str(metadata.get("mask_coordinate_space") or "PAGE").upper()
        if coordinate_space in {"PAGE", "PAGE_SPACE"}:
            if mask.size != page_size:
                raise ValueError("mask_shape_mismatch")
            page = mask.copy()
        elif coordinate_space in {"CROP", "CROP_SPACE"}:
            box = metadata.get("mask_crop_box")
            if not isinstance(box, (list, tuple)) or len(box) != 4:
                raise ValueError("mask_crop_geometry_required")
            x, y, w, h = [int(value) for value in box]
            if w <= 0 or h <= 0 or mask.size != (w, h):
                raise ValueError("mask_crop_shape_mismatch")
            px, py = page_size
            if x < 0 or y < 0 or x + w > px or y + h > py:
                raise ValueError("mask_crop_out_of_bounds")
            page = Image.new("L", page_size, 0)
            page.paste(mask, (x, y))
        else:
            raise ValueError("mask_coordinate_space_invalid")
    binary = page.point(lambda value: 255 if value >= 128 else 0, mode="L")
    covered = int(np.count_nonzero(np.asarray(binary)))
    total = page.size[0] * page.size[1]
    if covered == 0:
        raise ValueError("mask_empty")
    if covered == total:
        raise ValueError("mask_full_image_forbidden")
    ratio = covered / total
    if ratio > MAX_MASK_RATIO:
        raise ValueError("mask_area_excessive")
    return binary, {"coordinate_space": coordinate_space, "page_size": list(page.size),
                    "coverage_pixels": covered, "coverage_ratio": round(ratio, 6),
                    "mask_sha256": sha256_file(mask_path)}


def seam_metrics(original: Image.Image, final: Image.Image, page_mask: Image.Image,
                 *, band_radius: int = 3) -> dict[str, Any]:
    """Measure only the local transition at the effective-mask boundary.

    The untouched outside ring is compared against the corresponding source
    ring, so a legitimate interior edit is not treated as an image-wide error.
    The score is intentionally explainable and bounded in [0, 1].
    """
    source = np.asarray(original.convert("RGB"), dtype=np.float32)
    result = np.asarray(final.convert("RGB"), dtype=np.float32)
    mask = np.asarray(page_mask) > 0
    radius = max(1, int(band_radius))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius * 2 + 1, radius * 2 + 1))
    mask_u8 = mask.astype(np.uint8)
    inner = mask & (cv2.erode(mask_u8, kernel) == 0)
    outer = (cv2.dilate(mask_u8, kernel) > 0) & ~mask
    if min(int(inner.sum()), int(outer.sum())) < 8:
        return {"status": "SEAM_REVIEW", "score": None, "reason": "insufficient_boundary_band",
                "band_radius": radius, "inner_pixels": int(inner.sum()), "outer_pixels": int(outer.sum())}

    def gray(rgb):
        return rgb[..., 0] * 0.2126 + rgb[..., 1] * 0.7152 + rgb[..., 2] * 0.0722

    source_gray, final_gray = gray(source), gray(result)
    source_luma_jump = abs(float(source_gray[inner].mean()) - float(source_gray[outer].mean())) / 255.0
    final_luma_jump = abs(float(final_gray[inner].mean()) - float(final_gray[outer].mean())) / 255.0
    luminance_discontinuity = min(1.0, abs(final_luma_jump - source_luma_jump))
    source_color_jump = float(np.linalg.norm(source[inner].mean(axis=0) - source[outer].mean(axis=0))) / 441.67
    final_color_jump = float(np.linalg.norm(result[inner].mean(axis=0) - result[outer].mean(axis=0))) / 441.67
    color_discontinuity = min(1.0, abs(final_color_jump - source_color_jump))

    def gradient(gray_image):
        gx = cv2.Sobel(gray_image, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray_image, cv2.CV_32F, 0, 1, ksize=3)
        return cv2.magnitude(gx, gy)

    source_grad, final_grad = gradient(source_gray), gradient(final_gray)
    gradient_jump = min(1.0, float(abs(final_grad[outer].mean() - source_grad[outer].mean())) / 1020.0)
    source_texture = float(source_gray[inner].std() - source_gray[outer].std()) / 255.0
    final_texture = float(final_gray[inner].std() - final_gray[outer].std()) / 255.0
    texture_mismatch = min(1.0, abs(final_texture - source_texture))
    score = min(1.0, 0.35 * luminance_discontinuity + 0.25 * color_discontinuity +
                0.20 * gradient_jump + 0.20 * texture_mismatch)
    return {"status": "SEAM_PASS" if score <= SEAM_PASS_THRESHOLD else "SEAM_REVIEW",
            "score": round(score, 6), "threshold": SEAM_PASS_THRESHOLD,
            "band_radius": radius, "inner_pixels": int(inner.sum()), "outer_pixels": int(outer.sum()),
            "luminance_discontinuity": round(luminance_discontinuity, 6),
            "color_discontinuity": round(color_discontinuity, 6),
            "gradient_jump": round(gradient_jump, 6), "texture_mismatch": round(texture_mismatch, 6)}

def validate_and_composite(original: str | Path, raw: str | Path, final: str | Path,
                           mask: str | Path | None, metadata: dict[str, Any] | None = None,
                           *, semantic_ocr_engine=None) -> dict[str, Any]:
    metadata = metadata if isinstance(metadata, dict) else {}
    sfx_reason = _sfx_block_reason(metadata)
    if sfx_reason:
        return {"quality_status": sfx_reason, "outside_mask_changed_pixels": None, "sfx_firewall": "BLOCKED"}
    raw_diag = inspect_image(raw)
    if raw_diag.pil_decode != "PASS":
        raise ValueError("raw output failed decode")
    source_sha, raw_sha = sha256_file(original), sha256_file(raw)
    with Image.open(original) as original_img, Image.open(raw) as raw_img:
        original_img = original_img.convert("RGBA")
        raw_size = raw_img.size
        raw_page = raw_img.convert("RGBA")
        raw_alpha = np.asarray(raw_page.getchannel("A"), dtype=np.uint8)
        alpha_metrics = {
            "min": int(raw_alpha.min()), "max": int(raw_alpha.max()),
            "mean": round(float(raw_alpha.mean()), 6),
            "transparent_pixels": int(np.count_nonzero(raw_alpha == 0)),
            "partial_pixels": int(np.count_nonzero((raw_alpha > 0) & (raw_alpha < 255))),
            "total_pixels": int(raw_alpha.size),
        }
        if alpha_metrics["max"] == 0:
            original_img.save(final, format="PNG")
            return {
                "quality_status": "REVIEW_REQUIRED_RAW_OUTPUT_TRANSPARENT",
                "outside_mask_changed_pixels": 0, "outside_mask_changed_pixels_exact": 0,
                "source_sha256": source_sha, "raw_sha256": raw_sha,
                "final_sha256": sha256_file(final), "raw_size": list(raw_size),
                "page_size": list(original_img.size), "raw_alpha": alpha_metrics,
                "alignment": {"status": "NOT_RUN", "method": "NOT_RUN"},
                "alignment_validation": {"status": "NOT_RUN", "method": "NOT_RUN"},
                "residual": {"status": "INCONCLUSIVE", "source_text_removal": "INCONCLUSIVE",
                             "reason": "provider_raw_image_fully_transparent"},
                "ocr_text_removal": {"status": "INCONCLUSIVE",
                                      "reason": "provider_raw_image_fully_transparent"},
                "sfx": {"status": "PASS", "policy": "authorized_target_only"},
                "sfx_firewall": "PASS",
            }
        if mask is None:
            return {"quality_status": "REVIEW_REQUIRED_NO_AUTHORIZED_MASK", "outside_mask_changed_pixels": None,
                    "source_sha256": source_sha, "raw_sha256": raw_sha,
                    "raw_alpha": alpha_metrics, "sfx_firewall": "PASS"}
        alignment = "NATIVE"
        if raw_size != original_img.size:
            source_ratio = original_img.width / original_img.height
            raw_ratio = raw_page.width / raw_page.height
            if abs(source_ratio - raw_ratio) > 0.001:
                return {"quality_status": "REVIEW_REQUIRED_ALIGNMENT", "alignment": "ASPECT_RATIO_MISMATCH",
                        "outside_mask_changed_pixels": None, "source_sha256": source_sha, "raw_sha256": raw_sha}
            raw_page = raw_page.resize(original_img.size, Image.Resampling.LANCZOS)
            alignment = "NOMINAL_RESIZE_REVIEWED"
        page_mask, mask_metrics = _page_mask(mask, original_img.size, metadata)
        # Honor alpha before applying the binary safety mask. Image.composite
        # alone ignores transparency semantics and would paste hidden RGB
        # values (black in the observed smoke) into the authorized area.
        visible_raw = Image.alpha_composite(original_img, raw_page)
        result = Image.composite(visible_raw, original_img, page_mask)
        result.save(final, format="PNG")
        diff = np.asarray(ImageChops.difference(result, original_img))
        raw_diff = np.asarray(ImageChops.difference(visible_raw, original_img))
        outside = np.asarray(page_mask) == 0
        changed = int(np.count_nonzero(np.any(diff[outside] != 0, axis=1)))
        raw_outside_changed = int(np.count_nonzero(np.any(raw_diff[outside] != 0, axis=1)))
        seam = seam_metrics(original_img, result, page_mask)
        result_sha = sha256_file(final)
        page_size = list(original_img.size)
    mask_sha = mask_metrics["mask_sha256"]
    identity = {
        "schema_version": metadata.get("schema_version"),
        "request_id": metadata.get("request_id"),
        "job_id": metadata.get("request_id"),
        "parent_job_id": metadata.get("parent_job_id"),
        "run_id": metadata.get("run_id"),
        "page_id": str(metadata.get("page_id") or ""),
        "region_ids": metadata.get("region_ids"),
        "source_sha256": metadata.get("source_sha256"),
        "base_sha256": metadata.get("base_sha256"),
        "mask_sha256": metadata.get("mask_sha256"),
    }
    identity_missing = (
        identity["schema_version"] != "safe-clean.v2"
        or not identity["request_id"] or not identity["parent_job_id"]
        or not identity["run_id"] or not identity["page_id"]
        or not isinstance(identity["region_ids"], list) or not identity["region_ids"]
        or identity["source_sha256"] != source_sha
        or identity["mask_sha256"] != mask_sha
        or not isinstance(identity["base_sha256"], str) or len(identity["base_sha256"]) != 64
    )
    semantic = validate_text_removal(original, final, np.asarray(page_mask), metadata,
                                     ocr_engine=semantic_ocr_engine)
    residual_status = semantic["status"]
    residual = {
        "status": "PASS" if residual_status == "PASS" else "FAIL" if residual_status == "FAIL" else "INCONCLUSIVE",
        "method": "region_scoped_source_token_comparison_and_two_view_ocr_detection",
        "source_text_removal": residual_status,
        "residual_matches": semantic.get("residual_matches", []),
    }
    alignment_status = "PASS" if alignment in {"NATIVE", "NOMINAL_RESIZE_REVIEWED"} else "FAIL"
    sfx = {"status": "PASS", "policy": "authorized_target_only"}
    if identity_missing:
        quality = "REVIEW_REQUIRED_PROTOCOL_IDENTITY"
    elif changed != 0:
        quality = "REVIEW_REQUIRED_OUTSIDE_MASK_CHANGED"
    elif seam["status"] != "SEAM_PASS":
        quality = "REVIEW_REQUIRED_SEAM"
    elif semantic["status"] == "FAIL":
        quality = "REJECTED_RESIDUAL_SOURCE_TEXT"
    elif semantic["status"] != "PASS":
        quality = "REVIEW_REQUIRED_OCR_TEXT_REMOVAL_INCONCLUSIVE"
    else:
        quality = "ACCEPTABLE"
    return {"quality_status": quality, "identity": identity,
            "outside_mask_changed_pixels": changed,
            "outside_mask_changed_pixels_exact": changed, "source_sha256": source_sha,
            "raw_sha256": raw_sha, "final_sha256": result_sha,
            "alignment": {"status": alignment_status, "method": alignment},
            "alignment_method": alignment,
            "raw_outside_mask_changed_pixels": raw_outside_changed,
            "alignment_validation": {"status": alignment_status, "method": alignment},
            "seam": seam, "residual": residual, "ocr_text_removal": semantic,
            "raw_size": list(raw_size), "page_size": page_size, "mask": mask_metrics,
            "raw_alpha": alpha_metrics,
            "sfx": sfx, "sfx_firewall": "PASS"}
