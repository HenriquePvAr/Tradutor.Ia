"""Fail-closed Gemini translation fallback policy.

This module owns routing only.  It never calls Gemini by itself: the caller must
provide an already-authenticated text adapter.  Keeping the adapter injected
prevents the translation fallback from silently becoming an OCR or image-clean
fallback and makes billing remain one logical translation operation.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable

from yomu.translation import semantic_fidelity

TRANSLATION_FALLBACK_ELIGIBLE = "GEMINI_TRANSLATION_FALLBACK_ELIGIBLE"
TRANSLATION_FALLBACK_UNAVAILABLE = "TRANSLATION_FALLBACK_UNAVAILABLE"
TRANSLATION_FALLBACK_REJECTED = "TRANSLATION_FALLBACK_REJECTED"
TRANSLATION_FALLBACK_ACCEPTED = "TRANSLATION_FALLBACK_ACCEPTED"

_OCR_FAILURE_MARKERS = (
    "ocr_suspicious", "ocr_recovery", "source_ocr", "segmentation_incomplete",
    "unreadable", "partial_ocr", "joined_words",
)
_SEMANTIC_FAILURE_MARKERS = (
    "semantic_fidelity", "meaning_mismatch", "entity_changed", "quantity_changed",
    "negation_changed", "state_action_changed", "actor_relation_changed",
    "temporal_relation_changed", "modality_tense_changed", "word_sense_context",
)
_BLOCKED_CLASSES = {"sfx", "proper_name", "proper_name_only", "decorative_text", "decorative"}


def _reason(group: Any) -> str:
    return str(
        getattr(group, "translation_final_reason", "")
        or getattr(group, "translation_validation_reason", "")
        or ""
    ).strip().lower()


def _source_text(group: Any) -> str:
    return re.sub(r"\s+", " ", str(getattr(group, "canonical_source_text", "") or getattr(group, "text", "") or "")).strip()


def translation_fallback_eligibility(group: Any) -> tuple[bool, str]:
    """Return whether a group is eligible, with an auditable reason code."""
    classification = str(getattr(group, "classification", "") or "").strip().lower()
    if classification in _BLOCKED_CLASSES or bool(getattr(group, "preserve_as_name", False)):
        return False, "content_type_protected"
    source = _source_text(group)
    if not source:
        return False, "source_text_missing"
    if bool(getattr(group, "ocr_quality_blocked", False)):
        return False, "ocr_recovery_required"
    reasons = _reason(group)
    if any(marker in reasons for marker in _OCR_FAILURE_MARKERS):
        return False, "ocr_recovery_required"
    if not bool(getattr(group, "sent_to_translation", False)):
        return False, "primary_translation_not_attempted"
    if bool(getattr(group, "translation_valid", False)) and str(getattr(group, "translation", "") or "").strip():
        return False, "primary_translation_valid"
    if not (
        bool(getattr(group, "translation_attempts_exhausted", False))
        or bool(getattr(group, "translation_unresolved", False))
        or not bool(getattr(group, "translation_valid", False))
        or any(marker in reasons for marker in _SEMANTIC_FAILURE_MARKERS)
    ):
        return False, "primary_translation_not_failed"
    if not any(marker in reasons for marker in _SEMANTIC_FAILURE_MARKERS) and not (
        bool(getattr(group, "translation_attempts_exhausted", False))
        or bool(getattr(group, "translation_unresolved", False))
        or not bool(getattr(group, "translation_valid", False))
    ):
        return False, "failure_not_semantic"
    return True, TRANSLATION_FALLBACK_ELIGIBLE


def normalize_text_candidate(value: Any) -> str:
    """Accept only plain text; reject markdown/explanations/structured leakage."""
    if isinstance(value, dict):
        value = value.get("translation") or value.get("text") or ""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text or "```" in text or "\n" in text:
        return ""
    if re.search(r"(?i)^(translation|resposta|answer)\s*:", text):
        return ""
    return text


def apply_translation_fallbacks(
    groups: Iterable[Any],
    adapter: Callable[..., Any] | None,
    *,
    target_locale: str = "pt-BR",
    context_provider: Callable[[Any], Iterable[str]] | None = None,
    fidelity_verifier: Any = None,
) -> dict[str, Any]:
    """Try bounded text-only recovery after primary validation is exhausted.

    Billing is intentionally outside this function: one accepted candidate is
    still part of the original translation operation and must not reserve YK a
    second time.
    """
    stats = {"eligible": 0, "attempted": 0, "accepted": 0, "rejected": 0, "unavailable": 0, "items": []}
    if adapter is None:
        return stats
    from yomu.ocr.ocr_balloon import apply_group_translations

    for group in groups or ():
        eligible, reason = translation_fallback_eligibility(group)
        if not eligible:
            continue
        stats["eligible"] += 1
        source = _source_text(group)
        context = tuple(context_provider(group) if context_provider else getattr(group, "page_context_texts", ()) or ())
        try:
            candidate = normalize_text_candidate(adapter(
                source_text=source,
                target_locale=target_locale,
                region_type=str(getattr(group, "classification", "") or ""),
                context=context,
            ))
        except Exception as exc:  # adapter outages are review, never job crashes
            candidate = ""
            reason = f"adapter_error:{type(exc).__name__}"
        stats["attempted"] += 1
        if not candidate:
            stats["unavailable"] += 1
            group.translation_fallback_status = TRANSLATION_FALLBACK_UNAVAILABLE
            stats["items"].append({"group_id": str(getattr(group, "group_id", "")), "status": TRANSLATION_FALLBACK_UNAVAILABLE, "reason": reason})
            continue
        finding = semantic_fidelity.evaluate_local_fidelity(
            source,
            candidate,
            classification=str(getattr(group, "classification", "speech") or "speech"),
            protected_entities=tuple(getattr(group, "detected_proper_names", ()) or ()),
            proper_names=tuple(getattr(group, "detected_proper_names", ()) or ()),
            context_texts=context,
        )
        verdict = finding
        if finding.status == semantic_fidelity.VERIFY and fidelity_verifier is not None:
            verdict = semantic_fidelity.adjudicate(
                fidelity_verifier,
                source_text=source,
                candidate=candidate,
                finding=finding,
                target_language=target_locale,
                protected_entities=tuple(getattr(group, "detected_proper_names", ()) or ()),
                context=context,
            )
        if verdict.status != semantic_fidelity.FAITHFUL:
            stats["rejected"] += 1
            group.manual_review_required = True
            group.translation_fallback_status = TRANSLATION_FALLBACK_REJECTED
            group.translation_validation_reason = TRANSLATION_FALLBACK_REJECTED
            group.translation_fallback_reason = ":".join(verdict.reason_codes) if verdict.reason_codes else TRANSLATION_FALLBACK_REJECTED
            stats["items"].append({"group_id": str(getattr(group, "group_id", "")), "status": TRANSLATION_FALLBACK_REJECTED, "reason": group.translation_fallback_reason})
            continue
        apply_group_translations([group], [candidate])
        if bool(getattr(group, "translation_valid", False)) and str(getattr(group, "translation", "") or "").strip():
            group.fallback_used = True
            group.fallback_reason = "gemini_translation_fallback"
            group.translation_fallback_status = TRANSLATION_FALLBACK_ACCEPTED
            group.translation_attempts_exhausted = False
            group.translation_unresolved = False
            group.manual_review_required = False
            stats["accepted"] += 1
            stats["items"].append({"group_id": str(getattr(group, "group_id", "")), "status": TRANSLATION_FALLBACK_ACCEPTED})
        else:
            stats["rejected"] += 1
            group.translation_fallback_status = TRANSLATION_FALLBACK_REJECTED
            stats["items"].append({"group_id": str(getattr(group, "group_id", "")), "status": TRANSLATION_FALLBACK_REJECTED, "reason": "same_validator_rejected"})
    return stats
