"""Provider-agnostic clean strategy with a fail-closed Gemini fallback boundary."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

SEAM_PASS_THRESHOLD = 0.80
MASK_CONFIDENCE_THRESHOLD = 0.80
LOCAL_SAFE = "LOCAL_SAFE"
LOCAL_RECONSTRUCTION_HARD = "LOCAL_RECONSTRUCTION_HARD"
LOCAL_MASK_UNCERTAIN = "LOCAL_MASK_UNCERTAIN"
SFX = "SFX"
INVALID_SOURCE = "INVALID_SOURCE"
SYSTEM_ERROR = "SYSTEM_ERROR"
FALLBACK_ELIGIBLE = {LOCAL_RECONSTRUCTION_HARD}
INFRASTRUCTURE_STATUSES = {INVALID_SOURCE, SYSTEM_ERROR}

@dataclass(frozen=True)
class CleanRequest:
    original: np.ndarray
    authorized_mask: np.ndarray | None = None
    mask_confidence: float | None = None
    source_type: str = ""
    job_id: str = ""
    page_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

def _review(request: CleanRequest, *, reason: str, local_status: str, **extra: Any) -> dict[str, Any]:
    return {"status": "REVIEW_REQUIRED", "strategy_requested": "clean", "strategy_used": "none",
            "local_status": local_status, "fallback_invoked": False, "fallback_reason": reason,
            "fallback_status": "NOT_RUN", "safety_status": "REVIEW_REQUIRED", "review_required": True,
            "job_id": request.job_id, "page_id": request.page_id, **extra}

def _masked_composite(original: np.ndarray, generated: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray | None, int]:
    if not isinstance(original, np.ndarray) or not isinstance(generated, np.ndarray):
        return None, -1
    if original.shape != generated.shape or original.ndim != 3 or generated.ndim != 3:
        return None, -1
    authorized = np.asarray(mask) > 0
    if authorized.shape != original.shape[:2]:
        return None, -1
    output = original.copy()
    output[authorized] = generated[authorized]
    outside = ~authorized
    changed = int(np.count_nonzero(np.any(output[outside] != original[outside], axis=1))) if np.any(outside) else 0
    return output, changed

def run_clean_strategy(request: CleanRequest, local_result: dict[str, Any], *,
                       fallback: Callable[[CleanRequest], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Try local clean first; invoke fallback only for eligible content difficulty."""
    local = local_result if isinstance(local_result, dict) else {}
    local_status = str(local.get("status") or "").upper()
    if local_status == LOCAL_SAFE:
        return {"status": "COMPLETED", "strategy_requested": "clean", "strategy_used": "local",
                "local_status": local_status, "fallback_invoked": False, "fallback_reason": "local_safe",
                "fallback_status": "NOT_RUN", "safety_status": "PASS", "review_required": False,
                "image": local.get("image"), "outside_mask_changed_pixels": 0,
                "job_id": request.job_id, "page_id": request.page_id}
    if local_status == SFX:
        return _review(request, reason="sfx_firewall", local_status=local_status,
                       sfx_preserved=True, fallback_status="BLOCKED_SFX")
    if local_status in INFRASTRUCTURE_STATUSES:
        return _review(request, reason="infrastructure_error", local_status=local_status)
    if local_status not in FALLBACK_ELIGIBLE:
        return _review(request, reason="local_status_not_fallback_eligible", local_status=local_status)
    if request.authorized_mask is None or request.mask_confidence is None \
            or float(request.mask_confidence) < MASK_CONFIDENCE_THRESHOLD:
        return _review(request, reason="mask_uncertain", local_status=LOCAL_MASK_UNCERTAIN,
                       mask_status="MASK_UNCERTAIN")
    if fallback is None:
        return _review(request, reason="remote_unavailable", local_status=local_status,
                       fallback_status="REMOTE_UNAVAILABLE")
    try:
        remote = fallback(request)
    except (TimeoutError, OSError, RuntimeError, ValueError) as exc:
        return _review(request, reason="remote_failure", local_status=local_status,
                       fallback_invoked=True, fallback_status=type(exc).__name__)
    remote = remote if isinstance(remote, dict) else {}
    if str(remote.get("status") or "").upper() not in {"COMPLETED", "PASS"}:
        return _review(request, reason="fallback_not_completed", local_status=local_status,
                       fallback_invoked=True, fallback_status=str(remote.get("status") or "FAILED"))
    if int(remote.get("outside_mask_changed_pixels") or 0) != 0:
        return _review(request, reason="outside_mask_violation", local_status=local_status,
                       fallback_invoked=True, fallback_status="SAFETY_FAIL",
                       outside_mask_changed_pixels=int(remote["outside_mask_changed_pixels"]))
    composite, outside_changed = _masked_composite(request.original, remote.get("raw_image"), request.authorized_mask)
    if composite is None:
        return _review(request, reason="alignment_failed", local_status=local_status,
                       fallback_invoked=True, fallback_status="ALIGNMENT_FAIL")
    seam_score = float(remote.get("seam_score") or 0.0)
    if seam_score < SEAM_PASS_THRESHOLD:
        return _review(request, reason="seam_failed", local_status=local_status,
                       fallback_invoked=True, fallback_status="SEAM_FAIL", seam_score=seam_score)
    if outside_changed != 0:
        return _review(request, reason="outside_mask_violation", local_status=local_status,
                       fallback_invoked=True, fallback_status="SAFETY_FAIL",
                       outside_mask_changed_pixels=outside_changed)
    return {"status": "COMPLETED", "strategy_requested": "clean", "strategy_used": "gemini",
            "local_status": local_status, "fallback_invoked": True,
            "fallback_reason": "local_reconstruction_hard", "fallback_status": "COMPLETED",
            "safety_status": "PASS", "review_required": False, "image": composite,
            "raw_image": remote.get("raw_image"), "mask_generator_version": "AUTHORIZED_MASK_GENERATOR_V2",
            "outside_mask_changed_pixels": 0, "seam_threshold": SEAM_PASS_THRESHOLD,
            "seam_score": seam_score, "job_id": request.job_id, "page_id": request.page_id}
