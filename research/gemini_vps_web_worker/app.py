from __future__ import annotations

import json
import hashlib
import math
import os
import re
from io import BytesIO
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from .models import JobStatus
from .storage import JobStore

ROOT = Path(os.environ.get("YOMU_WORKER_ROOT", ".runtime/gemini-hard-clean-worker"))
store = JobStore(ROOT)
app = FastAPI(title="Yomu Gemini Web Fallback", version="2.0.0-safe-clean-semantic")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_v2_submission(metadata: Any, *, job_id: str | None, page_id: str | None,
                            region_id: str | None, content_type: str | None,
                            image_bytes: bytes, mask_bytes: bytes | None) -> bool:
    if not isinstance(metadata, dict) or metadata.get("schema_version") != "safe-clean.v2":
        return False
    region_ids = metadata.get("region_ids")
    local_validation = metadata.get("local_validation")
    mask_coverage = metadata.get("mask_coverage")
    source_text = str(metadata.get("source_text") or "").strip()
    confidence = metadata.get("source_text_confidence")
    if not isinstance(region_ids, list) or len(region_ids) != 1 or not region_ids[0]:
        return False
    if str(region_id or "") != str(region_ids[0]):
        return False
    if not all(str(metadata.get(key) or "").strip() for key in
               ("request_id", "parent_job_id", "run_id", "page_id", "source_text_provenance", "source_language")):
        return False
    if str(job_id or "") != str(metadata.get("request_id")) or str(page_id or "") != str(metadata.get("page_id")):
        return False
    if metadata.get("translation_enabled") is not False or metadata.get("disposition") != "LOCAL_HARD":
        return False
    if metadata.get("ownership_confirmed") is not True or metadata.get("local_clean_attempted") is not True:
        return False
    if str(content_type or "").lower() in {"sfx", "unknown", "art", "decorative"}:
        return False
    if (not source_text or not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
            or not math.isfinite(float(confidence)) or confidence < 0.60):
        return False
    if not isinstance(local_validation, dict) or local_validation.get("clean_validation_passed") is not False:
        return False
    if not isinstance(mask_coverage, dict) or mask_coverage.get("status") != "PASS":
        return False
    if (mask_coverage.get("geometry_kind") != "owned_components_plus_supported_light_outline"
            or str(mask_coverage.get("region_id") or "") != str(region_id or "")
            or not isinstance(mask_coverage.get("owned_group_id"), str)
            or not mask_coverage.get("owned_group_id")
            or not isinstance(mask_coverage.get("target_group_index"), int)
            or isinstance(mask_coverage.get("target_group_index"), bool)
            or mask_coverage.get("target_group_index") < 0):
        return False
    try:
        coverage_ratio = float(mask_coverage.get("coverage_ratio"))
        minimum_coverage = float(mask_coverage.get("minimum_coverage_ratio"))
    except (TypeError, ValueError):
        return False
    coverage_groups = mask_coverage.get("groups")
    if (not math.isfinite(coverage_ratio) or not math.isfinite(minimum_coverage)
            or minimum_coverage < 0.98 or coverage_ratio < minimum_coverage
            or not isinstance(coverage_groups, list) or not coverage_groups):
        return False
    try:
        group_ratios = [float(group.get("coverage_ratio")) for group in coverage_groups
                        if isinstance(group, dict) and group.get("status") == "PASS"]
    except (TypeError, ValueError):
        return False
    if len(group_ratios) != len(coverage_groups) or any(
            not math.isfinite(ratio) or ratio < minimum_coverage for ratio in group_ratios):
        return False
    for group in coverage_groups:
        lines = group.get("lines")
        if not isinstance(lines, list) or not lines:
            return False
        try:
            line_ratios = [float(line.get("coverage_ratio")) for line in lines
                           if isinstance(line, dict) and line.get("status") == "PASS"]
        except (TypeError, ValueError):
            return False
        if len(line_ratios) != len(lines) or any(
                not math.isfinite(ratio) or ratio < minimum_coverage for ratio in line_ratios):
            return False
    if mask_bytes is None or not mask_bytes:
        return False
    if not all(_valid_sha256(metadata.get(key)) for key in
               ("source_sha256", "base_sha256", "mask_sha256")):
        return False
    if _sha256(image_bytes) != metadata["source_sha256"] or _sha256(mask_bytes) != metadata["mask_sha256"]:
        return False
    return True


@app.get("/health")
def health():
    return {"api": "PASS", "queue": "PASS", "worker": "UNKNOWN", "browser": "UNKNOWN",
            "gemini_session": "UNKNOWN", "disk": {"root": str(ROOT)}}


@app.get("/v1/hard-clean/schema")
def safe_clean_schema():
    """Read-only contract probe; does not enqueue a generation."""
    return {
        "schema_version": "safe-clean.v2",
        "translation_enabled": False,
        "required_validation": [
            "outside_mask_changed_pixels", "alignment", "seam", "sfx",
            "residual", "ocr_text_removal", "authorized_mask_text_geometry_coverage",
        ],
        "ocr_text_removal_states": ["PASS", "FAIL", "INCONCLUSIVE"],
        "raw_result_loopback_only": True,
    }


@app.post("/v1/hard-clean/jobs", status_code=202)
async def submit_job(
    image: Annotated[UploadFile, File(...)],
    job_id: Annotated[str | None, Form()] = None,
    page_id: Annotated[str | None, Form()] = None,
    region_id: Annotated[str | None, Form()] = None,
    content_type: Annotated[str | None, Form()] = None,
    metadata: Annotated[str | None, Form(description="Required JSON safe-clean.v2 identity, source OCR provenance and authorization evidence")] = None,
    authorized_mask: Annotated[UploadFile | None, File()] = None,
):
    try:
        parsed_metadata = json.loads(metadata) if metadata else {}
        image_bytes = await image.read()
        mask_bytes = await authorized_mask.read() if authorized_mask else None
        if not _validate_v2_submission(parsed_metadata, job_id=job_id, page_id=page_id,
                                       region_id=region_id, content_type=content_type,
                                       image_bytes=image_bytes, mask_bytes=mask_bytes):
            raise ValueError("safe_clean_v2_metadata_invalid")
        parsed_metadata.update({"page_id": page_id, "region_id": region_id, "content_type": content_type})
        job = store.create(BytesIO(image_bytes), filename=image.filename or "original.bin",
                           job_id=job_id, metadata=parsed_metadata,
                           mask=BytesIO(mask_bytes) if mask_bytes is not None else None)
    except (ValueError, FileExistsError, json.JSONDecodeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"job_id": job.job_id, "status": job.status}


@app.get("/v1/hard-clean/jobs/{job_id}")
def get_status(job_id: str):
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return {**job.to_dict(), "result_available": bool(job.final_path and Path(job.final_path).exists())}


@app.get("/v1/hard-clean/jobs/{job_id}/result")
def get_result(job_id: str):
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if job.status not in {JobStatus.COMPLETED, JobStatus.REVIEW_REQUIRED} or not job.final_path or not Path(job.final_path).exists():
        raise HTTPException(409, f"result unavailable: {job.status}")
    return FileResponse(job.final_path, media_type="image/png", filename="final.png")


@app.get("/v1/hard-clean/jobs/{job_id}/raw")
def get_raw_result(job_id: str, request: Request):
    """Loopback-only audit endpoint; raw Gemini output is never the clean artifact."""
    if not request.client or request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, "raw evidence is loopback-only")
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if job.status not in {JobStatus.COMPLETED, JobStatus.REVIEW_REQUIRED}:
        raise HTTPException(409, f"raw result unavailable: {job.status}")
    if not job.raw_path or not Path(job.raw_path).is_file():
        raise HTTPException(404, "raw result unavailable")
    return FileResponse(job.raw_path, media_type="image/png", filename="gemini-raw.png")
