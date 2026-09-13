"""Fail-closed, review-only learned reconstruction shadow service.

This module intentionally does not apply a learned candidate to authoritative
artwork.  A future human-decision phase may consume the immutable bundle.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

import config


CANDIDATE_ID = "C8_LAMA_ONNX_NATIVE_TILE_V3"
MODEL_FILENAME = "inpainting_lama_2025jan.onnx"
MODEL_SIZE = 92_591_623
MODEL_SHA256 = "7df918ac3921d3daf0aae1d219776cf0dc4e4935f035af81841b40adcf74fdf2"
ADAPTER_ID = "LAMA_ONNX_ADAPTER_V3_NATIVE_TILE"
ADAPTER_VERSION = "V3"
H1_ID = "H1_RAPIDOCR_TEXT_RESIDUAL_GUARD"
NON_TEXT_WARNING = "NON_TEXT_HALLUCINATION_NOT_COVERED"


@dataclass(frozen=True)
class AutoAcceptanceResult:
    status: str
    reason: str


class UnqualifiedAutoAcceptancePolicy:
    """Current production policy: automatic promotion is never qualified."""

    policy_id = "UNQUALIFIED_AUTO_ACCEPTANCE_POLICY"
    policy_version = "1"

    def evaluate(self, *_args: Any, **_kwargs: Any) -> AutoAcceptanceResult:
        return AutoAcceptanceResult("REVIEW", "AUTO_ACCEPT_POLICY_NOT_QUALIFIED")


@dataclass(frozen=True)
class ShadowRequest:
    case_region_id: str
    pre_cleanup: np.ndarray
    mask: np.ndarray
    baseline: np.ndarray
    review_required: bool
    request_origin: str = "MANUAL"


@dataclass(frozen=True)
class RescueResult:
    status: str
    candidate_available: bool
    candidate_identity: str
    candidate_path: str | None
    candidate_hash: str | None
    guard_status: str
    guard_evidence: dict[str, Any]
    failure_reason: str | None
    review_required: bool
    bundle_path: str | None
    request_origin: str = "MANUAL"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "candidate_available": self.candidate_available,
            "candidate_identity": self.candidate_identity,
            "candidate_path": self.candidate_path,
            "candidate_hash": self.candidate_hash,
            "guard_status": self.guard_status,
            "guard_evidence": self.guard_evidence,
            "failure_reason": self.failure_reason,
            "review_required": self.review_required,
            "bundle_path": self.bundle_path,
            "request_origin": self.request_origin,
        }


def _sha256_array(value: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(value).tobytes()).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_failure(reason: str, *, status: str = "SHADOW_UNAVAILABLE") -> RescueResult:
    return RescueResult(
        status=status,
        candidate_available=False,
        candidate_identity=CANDIDATE_ID,
        candidate_path=None,
        candidate_hash=None,
        guard_status="NOT_RUN",
        guard_evidence={},
        failure_reason=reason,
        review_required=True,
        bundle_path=None,
    )


def run_h1_guard(candidate: np.ndarray, mask: np.ndarray) -> dict[str, Any]:
    """Run the frozen H1 rule with the existing RapidOCR runtime lazily.

    Import and model construction happen only after an explicit enabled review
    request; feature-off startup and ordinary cleanup never pay this cost.
    """
    from rapidocr_onnxruntime import RapidOCR
    import cv2

    result = RapidOCR()(candidate)
    rows = result[0] if isinstance(result, tuple) else result
    detections: list[dict[str, Any]] = []
    for item in rows or []:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        polygon, text, confidence = item[:3]
        text = str(text).strip()
        if not text or not any(char.isalnum() for char in text):
            continue
        points = np.round(np.asarray(polygon, dtype=np.float32)).astype(np.int32)
        footprint = np.zeros(mask.shape[:2], dtype=np.uint8)
        cv2.fillPoly(footprint, [points], 255)
        overlap = int(np.logical_and(footprint > 0, mask > 0).sum())
        detections.append({
            "text": text,
            "confidence": float(confidence),
            "polygon": points.tolist(),
            "overlap_pixels": overlap,
        })
    return {
        "status": "REJECT" if any(item["overlap_pixels"] > 0 for item in detections) else "PASS",
        "detections": detections,
    }


class LearnedRescueService:
    """Generate an immutable C8 shadow candidate only on explicit review request.

    ``runtime_factory`` and ``guard`` are injected so ordinary tests never need
    the optional 92 MB model or an OCR runtime.  The production boundary can
    provide the frozen V3 adapter and H1 implementation later.
    """

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        model_path: str | Path | None = None,
        adapter_id: str = ADAPTER_ID,
        adapter_version: str = ADAPTER_VERSION,
        adapter_hash: str = "",
        h1_spec_hash: str = "",
        model_validator: Callable[[Path], str | None] | None = None,
        mode: str | None = None,
        auto_policy: Any | None = None,
    ) -> None:
        if mode is None:
            mode = config.LEARNED_RESCUE_MODE
            if enabled is not None:
                mode = "manual" if enabled else "off"
        self.mode = mode if mode in {"off", "manual", "auto"} else "off"
        self.enabled = self.mode != "off"
        self.model_path = Path(model_path or config.LEARNED_RESCUE_MODEL_PATH) if (model_path or config.LEARNED_RESCUE_MODEL_PATH) else None
        self.adapter_id = adapter_id
        self.adapter_version = adapter_version
        self.adapter_hash = adapter_hash
        self.h1_spec_hash = h1_spec_hash
        self.model_validator = model_validator
        self.auto_policy = auto_policy or UnqualifiedAutoAcceptancePolicy()

    def request_shadow(
        self,
        request: ShadowRequest,
        *,
        runtime_factory: Callable[[np.ndarray, np.ndarray], np.ndarray] | None = None,
        guard: Callable[[np.ndarray, np.ndarray], dict[str, Any]] | None = None,
        bundle_root: str | Path | None = None,
        request_origin: str | None = None,
    ) -> RescueResult:
        """Handle one explicit request; never returns an authoritative image."""
        origin = str(request_origin or request.request_origin or "MANUAL").upper()
        if origin not in {"MANUAL", "AUTO"}:
            return _safe_failure("INVALID_REQUEST_ORIGIN")
        if not self.enabled:
            return _safe_failure("FEATURE_DISABLED")
        if self.mode == "manual" and origin != "MANUAL":
            return _safe_failure("MANUAL_REQUEST_REQUIRED")
        if not request.review_required:
            return _safe_failure("REVIEW_CONTEXT_REQUIRED")
        if runtime_factory is None:
            return _safe_failure("MODEL_UNAVAILABLE")
        if guard is None:
            guard = run_h1_guard
        for name, value in (("pre_cleanup", request.pre_cleanup), ("mask", request.mask), ("baseline", request.baseline)):
            if not isinstance(value, np.ndarray) or value.size == 0:
                return _safe_failure(f"INVALID_{name.upper()}")
        if request.pre_cleanup.shape != request.baseline.shape or request.mask.shape[:2] != request.pre_cleanup.shape[:2]:
            return _safe_failure("INPUT_SHAPE_MISMATCH")
        if self.model_path is None or not self.model_path.is_file():
            return _safe_failure("MODEL_UNAVAILABLE")
        identity_error = self.model_validator(self.model_path) if self.model_validator else (
            None if self.model_path.stat().st_size == MODEL_SIZE and _sha256_file(self.model_path) == MODEL_SHA256
            else "MODEL_HASH_MISMATCH"
        )
        if identity_error:
            return _safe_failure("MODEL_HASH_MISMATCH")
        started = time.perf_counter()
        try:
            candidate = np.asarray(runtime_factory(request.pre_cleanup.copy(), request.mask.copy()))
        except Exception as exc:  # pragma: no cover - exercised by injected tests
            return _safe_failure("CANDIDATE_GENERATION_FAILED")
        if candidate.shape != request.pre_cleanup.shape or candidate.dtype != request.pre_cleanup.dtype:
            return _safe_failure("CANDIDATE_GENERATION_FAILED")
        outside = request.mask == 0
        if not np.array_equal(candidate[outside], request.pre_cleanup[outside]):
            return _safe_failure("OUTSIDE_MASK_INVARIANT_FAILED")
        candidate_hash = _sha256_array(candidate)
        try:
            evidence = dict(guard(candidate.copy(), request.mask.copy()) or {})
            guard_status = str(evidence.get("status") or evidence.get("decision") or "ERROR").upper()
            if guard_status not in {"PASS", "REJECT"}:
                return _safe_failure("GUARD_ERROR", status="GUARD_ERROR")
        except Exception:  # fail closed; details are intentionally not leaked
            return _safe_failure("GUARD_ERROR", status="GUARD_ERROR")
        root = Path(bundle_root) if bundle_root else None
        candidate_path = None
        bundle_path = None
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)
            candidate_path = root / "candidate.npy"
            np.save(candidate_path, candidate)
            bundle = {
                "schema": "learned-rescue-review-bundle-v1",
                "case_region_id": request.case_region_id,
                "request_origin": origin,
                "candidate_status": "GUARD_PASS" if guard_status == "PASS" else "GUARD_REJECT",
                "pre_cleanup_hash": _sha256_array(request.pre_cleanup),
                "mask_hash": _sha256_array(request.mask),
                "baseline_hash": _sha256_array(request.baseline),
                "candidate_hash": candidate_hash,
                "model_hash": MODEL_SHA256,
                "adapter_id": self.adapter_id,
                "adapter_version": self.adapter_version,
                "adapter_hash": self.adapter_hash,
                "h1_id": H1_ID,
                "h1_spec_hash": self.h1_spec_hash,
                "h1_result": guard_status,
                "h1_detections": evidence.get("detections", []),
                "outside_mask_invariant": {"changed_pixels": 0},
                "warnings": [NON_TEXT_WARNING],
                "runtime_seconds": time.perf_counter() - started,
                "review_decision": None,
            }
            bundle_path = root / "review_bundle.json"
            bundle_path.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
        return RescueResult(
            status="REVIEW_PENDING",
            candidate_available=True,
            candidate_identity=CANDIDATE_ID,
            candidate_path=str(candidate_path) if candidate_path else None,
            candidate_hash=candidate_hash,
            guard_status=guard_status,
            guard_evidence=evidence,
            failure_reason=None,
            review_required=True,
            bundle_path=str(bundle_path) if bundle_path else None,
            request_origin=origin,
        )

    def apply_human_decision(
        self,
        request: ShadowRequest,
        *,
        candidate_path: str | Path,
        decision: str,
        downstream_validator: Callable[[np.ndarray], bool],
        authoritative_path: str | Path,
        bundle_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Stage and atomically promote an exact reviewed candidate only."""
        decision = str(decision).upper()
        if decision == "HUMAN_ACCEPT_BASELINE":
            return {"status": "REVIEW_ACCEPT_BASELINE", "authoritative_source": "BASELINE"}
        if decision == "HUMAN_KEEP_UNRESOLVED":
            return {"status": "REVIEW_KEEP_UNRESOLVED", "authoritative_source": "BASELINE"}
        if decision != "HUMAN_ACCEPT_C8":
            return {"status": "REVIEW_KEEP_UNRESOLVED", "failure_reason": "INVALID_HUMAN_DECISION", "authoritative_source": "BASELINE"}
        if not request.review_required:
            return {"status": "REVIEW_KEEP_UNRESOLVED", "failure_reason": "REVIEW_CONTEXT_REQUIRED", "authoritative_source": "BASELINE"}
        path = Path(candidate_path)
        if not path.is_file():
            return {"status": "REVIEW_KEEP_UNRESOLVED", "failure_reason": "CANDIDATE_UNAVAILABLE", "authoritative_source": "BASELINE"}
        candidate = np.load(path)
        if candidate.shape != request.pre_cleanup.shape or candidate.dtype != request.pre_cleanup.dtype:
            return {"status": "REVIEW_KEEP_UNRESOLVED", "failure_reason": "CANDIDATE_IDENTITY_MISMATCH", "authoritative_source": "BASELINE"}
        if not np.array_equal(candidate[request.mask == 0], request.pre_cleanup[request.mask == 0]):
            return {"status": "REVIEW_KEEP_UNRESOLVED", "failure_reason": "OUTSIDE_MASK_INVARIANT_FAILED", "authoritative_source": "BASELINE"}
        if bundle_path:
            bundle = json.loads(Path(bundle_path).read_text(encoding="utf-8"))
            bound = {
                "pre_cleanup_hash": _sha256_array(request.pre_cleanup),
                "mask_hash": _sha256_array(request.mask),
                "baseline_hash": _sha256_array(request.baseline),
                "model_hash": MODEL_SHA256,
                "adapter_hash": self.adapter_hash,
            }
            if (
                bundle.get("h1_result") != "PASS"
                or bundle.get("candidate_hash") != _sha256_array(candidate)
                or any(bundle.get(key) != value for key, value in bound.items())
                or bundle.get("candidate_identity", CANDIDATE_ID) != CANDIDATE_ID
            ):
                return {"status": "REVIEW_KEEP_UNRESOLVED", "failure_reason": "CANDIDATE_IDENTITY_MISMATCH", "authoritative_source": "BASELINE"}
        staged = request.baseline.copy()
        staged[request.mask != 0] = candidate[request.mask != 0]
        if not np.array_equal(staged[request.mask == 0], request.baseline[request.mask == 0]):
            return {"status": "REVIEW_KEEP_UNRESOLVED", "failure_reason": "OUTSIDE_MASK_INVARIANT_FAILED", "authoritative_source": "BASELINE"}
        try:
            valid = bool(downstream_validator(staged.copy()))
        except Exception:
            valid = False
        if not valid:
            return {"status": "APPLICATION_VALIDATION_FAILED", "failure_reason": "DOWNSTREAM_VALIDATION_FAILED", "authoritative_source": "BASELINE"}
        target = Path(authoritative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".staged.npy")
        np.save(temp, staged)
        temp.replace(target)
        return {"status": "REVIEW_ACCEPT_C8", "authoritative_source": "HUMAN_ACCEPTED_C8", "authoritative_hash": _sha256_array(staged)}

    def request_auto_shadow(self, request: ShadowRequest, **kwargs: Any) -> RescueResult:
        """Generate an AUTO shadow only; unqualified policy always ends in review."""
        if self.mode != "auto":
            return _safe_failure("AUTO_MODE_REQUIRED")
        result = self.request_shadow(request, request_origin="AUTO", **kwargs)
        if not result.candidate_available:
            return result
        # The policy interface is deliberately observable, but the production
        # default can only return REVIEW.  No machine-only promotion exists.
        policy_result = self.auto_policy.evaluate(result)
        if not isinstance(policy_result, AutoAcceptanceResult):
            policy_result = AutoAcceptanceResult("REVIEW", "AUTO_POLICY_INVALID_RESULT")
        evidence = dict(result.guard_evidence)
        evidence["auto_policy_status"] = policy_result.status
        evidence["auto_policy_reason"] = policy_result.reason
        return RescueResult(
            status="REVIEW_PENDING",
            candidate_available=True,
            candidate_identity=result.candidate_identity,
            candidate_path=result.candidate_path,
            candidate_hash=result.candidate_hash,
            guard_status=result.guard_status,
            guard_evidence=evidence,
            failure_reason="AUTO_ACCEPT_POLICY_NOT_QUALIFIED" if policy_result.status != "QUALIFIED_ACCEPT" else "AUTO_ACCEPT_DISABLED_IN_PRODUCTION",
            review_required=True,
            bundle_path=result.bundle_path,
            request_origin="AUTO",
        )
