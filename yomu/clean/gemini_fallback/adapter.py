"""Production adapter for the existing SAFE hard-clean remote contract."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
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
    request_id = str(request.request_id or uuid.uuid4())
    with tempfile.TemporaryDirectory(prefix="yomu-gemini-clean-") as temp:
        root = Path(temp)
        image_path = root / "original.png"
        mask_path = root / "authorized-mask.png"
        result_path = root / "result.png"
        raw_path = root / "gemini-raw.png"
        if not cv2.imwrite(str(image_path), request.original):
            raise OSError("original_materialization_failed")
        if not cv2.imwrite(str(mask_path), (np.asarray(request.authorized_mask) > 0).astype(np.uint8) * 255):
            raise OSError("mask_materialization_failed")
        source_sha256 = _sha256_file(image_path)
        mask_sha256 = _sha256_file(mask_path)
        base_image = request.base_image if isinstance(request.base_image, np.ndarray) else request.original
        base_path = root / "base.png"
        if not cv2.imwrite(str(base_path), base_image):
            raise OSError("base_materialization_failed")
        metadata = dict(request.metadata or {})
        metadata.update({
            "schema_version": "safe-clean.v2",
            "request_id": request_id,
            "parent_job_id": request.job_id,
            "run_id": request.run_id,
            "page_id": request.page_id,
            "region_ids": list(request.region_ids or ()),
            "source_sha256": source_sha256,
            "mask_sha256": mask_sha256,
            "base_sha256": _sha256_file(base_path),
            "content_type": request.source_type,
            "translation_enabled": False,
        })
        submitted = client.submit(
            image_path,
            authorized_mask=mask_path,
            job_id=request_id,
            page_id=request.page_id,
            region_id=",".join(request.region_ids or ()),
            content_type=request.source_type,
            metadata=json.dumps(metadata, sort_keys=True, separators=(",", ":")),
        )
        job_id = str((submitted or {}).get("job_id") or "")
        if not job_id or job_id != request_id:
            raise ValueError("remote_request_identity_mismatch")
        status = client.wait(job_id)
        remote_status = str(status.get("status") or "").upper()
        if remote_status not in {"COMPLETED", "PASS", "REVIEW_REQUIRED"}:
            return {"status": str(status.get("status") or "FAILED"), "remote_job_id": job_id}
        echoed = status.get("metadata")
        remote_output = echoed.get("output") if isinstance(echoed, dict) else None
        remote_output = remote_output if isinstance(remote_output, dict) else {}
        validation = echoed.get("validation") if isinstance(echoed, dict) else None
        identity = validation.get("identity") if isinstance(validation, dict) else None
        mask_metrics = validation.get("mask") if isinstance(validation, dict) else None
        seam = validation.get("seam") if isinstance(validation, dict) else None
        alignment_summary = validation.get("alignment") if isinstance(validation, dict) else None
        alignment = validation.get("alignment_validation") if isinstance(validation, dict) else None
        residual = validation.get("residual") if isinstance(validation, dict) else None
        sfx = validation.get("sfx") if isinstance(validation, dict) else None
        semantic = validation.get("ocr_text_removal") if isinstance(validation, dict) else None
        if not isinstance(echoed, dict) or any((
            status.get("job_id") != request_id,
            echoed.get("schema_version") != "safe-clean.v2",
            echoed.get("request_id") != request_id,
            echoed.get("parent_job_id") != request.job_id,
            str(echoed.get("page_id") or "") != str(request.page_id),
            str(echoed.get("run_id") or "") != str(request.run_id),
            echoed.get("region_ids") != list(request.region_ids or ()),
            not isinstance(identity, dict),
            identity.get("schema_version") != "safe-clean.v2" if isinstance(identity, dict) else True,
            identity.get("request_id") != request_id if isinstance(identity, dict) else True,
            identity.get("parent_job_id") != request.job_id if isinstance(identity, dict) else True,
            identity.get("run_id") != request.run_id if isinstance(identity, dict) else True,
            identity.get("page_id") != str(request.page_id) if isinstance(identity, dict) else True,
            identity.get("region_ids") != list(request.region_ids or ()) if isinstance(identity, dict) else True,
            status.get("source_sha256") != source_sha256,
            echoed.get("source_sha256") != source_sha256,
            identity.get("source_sha256") != source_sha256 if isinstance(identity, dict) else True,
            identity.get("base_sha256") != metadata["base_sha256"] if isinstance(identity, dict) else True,
            identity.get("mask_sha256") != mask_sha256 if isinstance(identity, dict) else True,
            not isinstance(mask_metrics, dict) or mask_metrics.get("mask_sha256") != mask_sha256,
            not isinstance(semantic, dict) or semantic.get("status") not in {"PASS", "FAIL", "INCONCLUSIVE"},
            not isinstance(seam, dict) or seam.get("status") not in {"SEAM_PASS", "SEAM_REVIEW"},
            not isinstance(alignment_summary, dict) or alignment_summary.get("status") not in {"PASS", "FAIL"},
            not isinstance(alignment, dict) or alignment.get("status") not in {"PASS", "FAIL"},
            not isinstance(sfx, dict) or sfx.get("status") not in {"PASS", "BLOCKED"},
            not isinstance(residual, dict) or residual.get("status") not in {"PASS", "FAIL", "INCONCLUSIVE"},
            not isinstance(validation, dict) or not validation.get("final_sha256"),
            not isinstance(validation, dict) or not validation.get("raw_sha256"),
        )):
            raise ValueError("remote_validation_or_identity_evidence_missing")
        client.download_result(job_id, result_path)
        download_raw = getattr(client, "download_raw_result", None)
        if not callable(download_raw):
            raise ValueError("remote_raw_evidence_endpoint_missing")
        download_raw(job_id, raw_path)
        safe_clean = cv2.imread(str(result_path), cv2.IMREAD_COLOR)
        if safe_clean is None or safe_clean.shape != request.original.shape:
            return {"status": "FAILED", "remote_job_id": job_id, "reason": "result_shape_invalid"}
        raw_image = cv2.imread(str(raw_path), cv2.IMREAD_COLOR)
        if raw_image is None or raw_image.shape != request.original.shape:
            return {"status": "FAILED", "remote_job_id": job_id, "reason": "raw_result_shape_invalid"}
        if _sha256_file(result_path) != validation["final_sha256"] or _sha256_file(raw_path) != validation["raw_sha256"]:
            raise ValueError("remote_image_hash_mismatch")
        _persist_evidence(request, request_id, image_path, mask_path, raw_path, result_path, metadata, status)
        safe_pass = all((
            remote_status in {"COMPLETED", "PASS"},
            validation.get("quality_status") == "ACCEPTABLE",
            validation.get("outside_mask_changed_pixels") == 0,
            semantic.get("status") == "PASS",
            validation.get("sfx_firewall") == "PASS",
            sfx.get("status") == "PASS",
            seam.get("status") == "SEAM_PASS",
            alignment.get("status") == "PASS",
            alignment_summary.get("status") == "PASS",
            residual.get("status") == "PASS",
        ))
        if not safe_pass:
            failed_gate = next((name for name, condition in (
                ("semantic_text_removal", semantic.get("status") == "PASS"),
                ("outside_mask", validation.get("outside_mask_changed_pixels") == 0),
                ("alignment", alignment.get("status") == "PASS"),
                ("alignment_summary", alignment_summary.get("status") == "PASS"),
                ("seam", seam.get("status") == "SEAM_PASS"),
                ("sfx", sfx.get("status") == "PASS"),
                ("residual", residual.get("status") == "PASS"),
                ("worker_status", remote_status in {"COMPLETED", "PASS"}),
            ) if not condition), "validation")
            rejected = semantic.get("status") == "FAIL" or failed_gate in {"outside_mask", "alignment", "seam", "sfx"}
            return {
                "status": "REMOTE_REJECTED" if rejected else "REVIEW_REQUIRED",
                "remote_job_id": job_id, "request_id": request_id,
                "ocr_text_removal": semantic, "raw_image": raw_image,
                "safe_clean_image": safe_clean, "failed_gate": failed_gate,
                "outside_mask_changed_pixels": validation.get("outside_mask_changed_pixels"),
                "seam_score": seam.get("score"),
                "source_text_observations": semantic.get("source_observations", []),
                "post_clean_text_observations": semantic.get("post_clean_observations", []),
                "translation_enabled": False, "translated_text_ptbr": None,
                "generation_seq": remote_output.get("generation_seq"),
                "slot_id": remote_output.get("slot_id"),
                "duration_seconds": remote_output.get("duration_seconds"),
            }
        return {
            "status": "COMPLETED",
            # The current known worker result is already a safe composite. It is
            # never mislabeled or promoted to raw Gemini evidence here.
            "safe_clean_image": safe_clean,
            "outside_mask_changed_pixels": validation["outside_mask_changed_pixels"],
            "seam_score": seam["score"],
            "ocr_text_removal": semantic,
            "alignment_validation": alignment,
            "seam_validation": seam,
            "sfx_validation": sfx,
            "residual_validation": residual,
            "raw_image": raw_image,
            "raw_gemini_available": True,
            "remote_job_id": job_id,
            "request_id": request_id,
            "source_sha256": source_sha256,
            "mask_sha256": mask_sha256,
            "base_sha256": metadata["base_sha256"],
            "source_text_observations": semantic.get("source_observations", []),
            "post_clean_text_observations": semantic.get("post_clean_observations", []),
            "residual_validation": residual,
            "alignment_validation": alignment,
            "sfx_validation": sfx,
            "translation_enabled": False,
            "translated_text_ptbr": None,
            "generation_seq": remote_output.get("generation_seq"),
            "slot_id": remote_output.get("slot_id"),
            "duration_seconds": remote_output.get("duration_seconds"),
        }


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _persist_evidence(request: Any, request_id: str, source: Path, mask: Path,
                      raw_result: Path, safe_result: Path, metadata: dict, response: dict) -> None:
    root = str(getattr(request, "evidence_dir", "") or "").strip()
    if not root:
        return
    target = Path(root) / request_id
    target.mkdir(parents=True, exist_ok=False)
    base = request.base_image if isinstance(request.base_image, np.ndarray) else request.original
    base_path = target / "local_base.png"
    ok, encoded_base = cv2.imencode(".png", base)
    if not ok:
        raise OSError("evidence_base_write_failed")
    temp_base = target / "local_base.png.tmp"
    temp_base.write_bytes(encoded_base.tobytes())
    os.replace(temp_base, base_path)
    for src, name in ((source, "source.png"), (mask, "authorized-mask.png"),
                      (raw_result, "gemini-raw.png"), (safe_result, "safe-clean.png")):
        data = src.read_bytes()
        temp = target / (name + ".tmp")
        temp.write_bytes(data)
        os.replace(temp, target / name)
    manifest = {
        "schema_version": "safe-clean-evidence.v1",
        "request": metadata,
        "response_status": response.get("status"),
        "response_validation": response.get("metadata", {}).get("validation", {}),
        "source_text_observations": response.get("metadata", {}).get("validation", {}).get("ocr_text_removal", {}).get("source_observations", []),
        "post_clean_text_observations": response.get("metadata", {}).get("validation", {}).get("ocr_text_removal", {}).get("post_clean_observations", []),
        "translation_enabled": False,
        "translated_text_ptbr": None,
        "images": {"source": "source.png", "local_base": "local_base.png",
                   "authorized_mask": "authorized-mask.png", "raw_gemini": "gemini-raw.png",
                   "safe_clean": "safe-clean.png"},
        "training_eligible": False,
        "training_candidate_after_automatic_gates": bool(
            response.get("metadata", {}).get("training_candidate", {}).get("training_candidate")
        ),
        "training_reason": "OWNER_REVIEW_REQUIRED_BEFORE_PROMOTION",
    }
    temp_manifest = target / "evidence.json.tmp"
    temp_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temp_manifest, target / "evidence.json")
