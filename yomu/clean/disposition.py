"""Typed final dispositions for source-text regions in CLEAN-only and full runs."""
from __future__ import annotations

from enum import StrEnum
import re


class RegionDisposition(StrEnum):
    LOCAL_SAFE = "LOCAL_SAFE"
    LOCAL_HARD = "LOCAL_HARD"
    PRESERVE_SFX = "PRESERVE_SFX"
    PRESERVE_ART = "PRESERVE_ART"
    REVIEW_UNOWNED = "REVIEW_UNOWNED"
    REVIEW_OCR = "REVIEW_OCR"
    REVIEW_SEMANTIC = "REVIEW_SEMANTIC"
    REMOTE_SAFE = "REMOTE_SAFE"
    REMOTE_REJECTED = "REMOTE_REJECTED"


def classify_group_disposition(group) -> tuple[RegionDisposition, str]:
    """Choose a conservative disposition from persisted region evidence.

    This does not infer that arbitrary visible text belongs to the story. The
    upstream region taxonomy remains the ownership authority; uncertainty is
    made reviewable rather than silently treated as clean.
    """
    classification = str(getattr(group, "classification", "unknown") or "unknown").lower()
    if classification == "sfx":
        return RegionDisposition.PRESERVE_SFX, "sfx_policy_preserve"
    if getattr(group, "preserve_as_name", False):
        return RegionDisposition.PRESERVE_ART, "recognized_name_preserve"
    if getattr(group, "ocr_quality_blocked", False) or not str(getattr(group, "text", "") or "").strip():
        return RegionDisposition.REVIEW_OCR, "ocr_not_reliable"
    if getattr(group, "ignored", False):
        reason = str(getattr(group, "ignore_reason", "") or "")
        text = str(getattr(group, "text", "") or "").upper()
        if (re.search(r"(?:HTTPS?://|WWW\.|\b(?:ART|STORY|SCRIPT|LETTERING)\s+BY\b|DISCORD\.GG)", text)
                or reason in {"url_or_watermark", "production_credit", "logo_or_branding"}):
            return RegionDisposition.PRESERVE_ART, reason or "promo_credit_preserve"
        if classification in {"decorative", "unknown"} and reason in {"decorative_text", "weak_unknown_text"}:
            return RegionDisposition.REVIEW_SEMANTIC, reason or "semantic_ownership_uncertain"
        return RegionDisposition.PRESERVE_ART, reason or "policy_preserve"
    return RegionDisposition.REVIEW_UNOWNED, "story_ownership_not_established"


def serialize_group_disposition(group) -> dict:
    value = getattr(group, "clean_disposition", None) or RegionDisposition.REVIEW_UNOWNED
    try:
        disposition = RegionDisposition(str(value))
    except ValueError:
        disposition = RegionDisposition.REVIEW_UNOWNED
    return {
        "disposition": disposition.value,
        "reason": str(getattr(group, "clean_disposition_reason", "") or ""),
        "confidence": float(getattr(group, "clean_disposition_confidence", 0.0) or 0.0),
        "ownership_status": "ESTABLISHED" if disposition in {
            RegionDisposition.LOCAL_SAFE, RegionDisposition.LOCAL_HARD,
            RegionDisposition.REMOTE_SAFE, RegionDisposition.REMOTE_REJECTED,
        } else "UNCERTAIN",
        "local_attempted": bool(getattr(group, "clean_local_attempted", False)),
        "local_validation": dict(getattr(group, "clean_artifact_validation", {}) or {}),
        "remote_eligible": disposition == RegionDisposition.LOCAL_HARD,
        "remote_attempted": bool(getattr(group, "clean_remote_attempted", False)),
        "remote_result": str(getattr(group, "clean_remote_result", "") or ""),
        "review_required": disposition in {
            RegionDisposition.REVIEW_UNOWNED, RegionDisposition.REVIEW_OCR,
            RegionDisposition.REVIEW_SEMANTIC, RegionDisposition.REMOTE_REJECTED,
        },
    }
