"""Production adapter for the existing SAFE hard-clean remote contract."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .remote_client import HardCleanRemoteClient


def remote_clean_callback(client: HardCleanRemoteClient, request: Any) -> dict[str, Any]:
    """Submit one page with an authorized mask and return decoded worker output.

    Temporary files are process-local and removed before returning.  The caller
    still performs the final authorized-mask composite; worker output is never
    accepted as a page-wide replacement.
    """
    if request.authorized_mask is None:
        raise ValueError("authorized_mask_required")
    with tempfile.TemporaryDirectory(prefix="yomu-gemini-clean-") as temp:
        root = Path(temp)
        image_path = root / "original.png"
        mask_path = root / "authorized-mask.png"
        result_path = root / "result.png"
        if not cv2.imwrite(str(image_path), request.original):
            raise OSError("original_materialization_failed")
        if not cv2.imwrite(str(mask_path), (np.asarray(request.authorized_mask) > 0).astype(np.uint8) * 255):
            raise OSError("mask_materialization_failed")
        metadata = {"source": "yomu", "contract": "SAFE_CLEAN_V2", "job_id": request.job_id, "page_id": request.page_id}
        metadata.update(dict(request.metadata or {}))
        submitted = client.submit(image_path, authorized_mask=mask_path, job_id=request.job_id, page_id=request.page_id, metadata=str(metadata))
        job_id = str((submitted or {}).get("job_id") or "")
        if not job_id:
            raise RuntimeError("remote_job_id_missing")
        status = client.wait(job_id)
        if str(status.get("status") or "").upper() not in {"COMPLETED", "PASS"}:
            return {"status": str(status.get("status") or "FAILED"), "remote_job_id": job_id}
        client.download_result(job_id, result_path)
        raw = cv2.imread(str(result_path), cv2.IMREAD_COLOR)
        if raw is None or raw.shape != request.original.shape:
            return {"status": "FAILED", "remote_job_id": job_id, "reason": "result_shape_invalid"}
        return {
            "status": "COMPLETED",
            "raw_image": raw,
            "outside_mask_changed_pixels": int(status.get("outside_mask_changed_pixels") or 0),
            "seam_score": float(status.get("seam_score") or 0.0),
            "remote_job_id": job_id,
        }
