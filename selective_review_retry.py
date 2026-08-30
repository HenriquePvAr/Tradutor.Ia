"""Selective, region-scoped retry for regions already flagged manual_review_required.

Mission #84F40: the OCR/translation/render gates already correctly refuse to
silently ship a region they cannot trust (fixed in #84F39). This module adds a
narrow *second chance* for the regions that stay flagged, one cause class at a
time, without loosening any existing gate:

* ``classify_review_cause`` names why a region is under review, reusing the
  same signal fields the pipeline already records (no new heuristics).
* ``retry_ocr_region`` re-applies the pipeline's own OCR repair + acceptance
  gate (``ocr_engine.repair_ocr_text`` / ``assess_ocr_repair``) to the source
  text. It can only discard characters the acceptance gate already proves are
  noise - it never invents words. If nothing safe is available, the source
  text (and the review flag) is returned unchanged.
* ``retry_fidelity_region`` re-runs a translation attempt for one region only,
  through a caller-supplied ``translate_fn`` (dependency injection - this
  module never talks to a translation provider itself), and re-validates the
  candidate with a caller-supplied ``validate_fn``. The threshold/validator is
  whatever the caller passes in - this module cannot loosen it because it
  never defines one.
* ``render_residual_forgivable`` is a pure evidence check used by the render
  gate: it forgives a post-render-OCR residual only when the source removal is
  already *proven* complete and every flagged token was already, individually,
  cleared by the pipeline's own provenance-based noise test (explainable by
  the expected translation, not by the source). It never touches pixels,
  never calls translation or render code again, and never forgives a token
  merely for being short.
* ``run_selective_retry`` is the single orchestrator used to replay retries
  against an already-completed job's flagged regions (mission step 9). It
  never clears ``manual_review_required`` unless the retried candidate clears
  every gate the caller wires in; on any doubt the region stays exactly as
  review-required as it started.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

import ocr_engine

BRANDING_REASONS = {"url", "credit"}

OCR_QUALITY_REASONS = {
    "alphanumeric_ocr_artifact",
    "long_consonant_run",
    "compact_word_segmentation_candidate",
    "generic_ocr_repair_available",
    "dictionary_near_miss",
    "long_token_without_spaces",
    "improbable_apostrophe_pattern",
    "mixed_case_ocr_artifact",
    "short_malformed_case_ocr_artifact",
}

FIDELITY_FINAL_REASONS = {
    "terminology_conflict_after_retries",
    "invalid_translation_after_retries",
    "character_conflict_after_retries",
}

RENDER_FINAL_REASONS = {
    "translation_not_rendered_after_validation",
}


def classify_review_cause(
    *,
    quality_reasons=(),
    translation_final_reason="",
    translation_validation_reason="",
    translation_final_state="",
    art_reconstruction_status="",
):
    """Return one of "branding", "ocr", "fidelity", "render", "other".

    Uses only fields the pipeline already produces (``quality_report.json``'s
    per-region ``translation_terminal_items``/``suspicious_groups`` entries),
    so it works both live (on ``Group`` attributes) and offline (on the JSON
    dict a completed job wrote to disk).
    """
    reason = str(translation_final_reason or "")
    if reason in BRANDING_REASONS:
        return "branding"
    if reason in RENDER_FINAL_REASONS:
        return "render"
    if reason in FIDELITY_FINAL_REASONS or str(translation_validation_reason or "").split(
        ":", 1
    )[0] in {"terminology_conflict", "unnatural_ptbr_verb_mood", "character_conflict"}:
        return "fidelity"
    if (
        translation_final_state == "skipped_with_reason"
        and reason == "translation_not_selected"
        and set(quality_reasons or []) & OCR_QUALITY_REASONS
    ):
        return "ocr"
    if set(quality_reasons or []) & OCR_QUALITY_REASONS and reason not in FIDELITY_FINAL_REASONS:
        return "ocr"
    if art_reconstruction_status == "review":
        # Translated cleanly; only the reconstruction-confidence gate is
        # unsure. Not ordinary-story English residual, and not this module's
        # job to resolve - it is a render-confidence, not a render failure.
        return "other"
    return "other"


@dataclass
class OcrRetryResult:
    original_text: str
    repaired_text: str
    changed: bool
    reason: str
    accepted: bool


def retry_ocr_region(source_text: str, *, confidence: float = 0.0, source_engine: str = "rapidocr") -> OcrRetryResult:
    """Re-run the pipeline's own conservative OCR repair on one region's text.

    Reuses ``ocr_engine.repair_ocr_text`` (the generator) gated by
    ``ocr_engine.assess_ocr_repair`` (the same runtime acceptance check the
    live OCR-line pass already uses) - never a bespoke or looser check. A
    repair the acceptance gate would reject (e.g. a dictionary "correction"
    that changes letters, or a segmentation with no dictionary support) is
    refused here exactly as it is refused live: the region keeps its original
    text and stays review-required.
    """
    original = str(source_text or "")
    repaired, reason = ocr_engine.repair_ocr_text(original)
    if repaired == original or not reason:
        return OcrRetryResult(original, original, False, "", False)
    assessment = ocr_engine.assess_ocr_repair(
        original, repaired, reason, confidence=confidence, source_engine=source_engine
    )
    accepted = bool(assessment["accepted"])
    return OcrRetryResult(
        original_text=original,
        repaired_text=repaired if accepted else original,
        changed=accepted,
        reason=reason,
        accepted=accepted,
    )


@dataclass
class FidelityRetryResult:
    candidate: str
    valid: bool
    reason: str
    attempted: bool


def retry_fidelity_region(
    source_text: str,
    previous_candidate: str,
    *,
    classification: str,
    allowed_names,
    validation_reason: str,
    translate_fn: Callable[..., str],
    validate_fn: Callable[..., "tuple"],
    required_name_spans=None,
    context_before: str = "",
    context_after: str = "",
) -> FidelityRetryResult:
    """One bounded extra translation attempt for a single flagged region.

    ``translate_fn``/``validate_fn`` are injected by the caller so this
    function never talks to a translation provider and never redefines a
    validator or threshold - it only re-runs whichever ones the live pipeline
    already uses, once, for this one region. The caller decides whether this
    consumes its own retry budget; this module has no global state and no
    opinion about how many regions may retry in one run.
    """
    if translate_fn is None or validate_fn is None:
        return FidelityRetryResult(previous_candidate, False, "no_retry_available", False)
    candidate = translate_fn(
        source_text,
        previous_translation=previous_candidate,
        validation_reason=validation_reason,
        context_before=context_before,
        context_after=context_after,
    )
    candidate = str(candidate or "").strip()
    if not candidate:
        return FidelityRetryResult("", False, "empty_retry_candidate", True)
    valid, reason = validate_fn(
        source_text,
        candidate,
        classification,
        allowed_names,
        required_name_spans=required_name_spans,
    )
    return FidelityRetryResult(candidate, bool(valid), str(reason or ""), True)


def render_residual_forgivable(
    *,
    flagged_tokens,
    forgiven_ocr_noise_tokens,
    source_text_coverage: float,
) -> bool:
    """Pure evidence check: is a post-render residual already proven to be noise?

    Deliberately does not invent a new leniency rule. The render gate already
    computes, per token, whether a flagged token is explainable as OCR noise
    from the *expected translation* rather than the source
    (``token not in provenance_tokens and token in expected_joined`` - see
    ``ocr_balloon._post_render_source_text_check``) - that is the only
    provenance-safe test in this codebase, and it is what
    ``test_expected_source_word_is_never_forgiven_as_noise`` pins: a token the
    source genuinely owns must never be forgiven merely for being short.

    This only widens *when* that already-computed, per-token verdict is
    trusted: instead of requiring the whole rendered string to match the
    expected translation's shape (``rendered_matches_expected``, which one
    unrelated noisy token elsewhere in a long line can fail on its own), it
    accepts forgiveness once every *flagged* token individually cleared that
    same per-token check and the mask proved the source was fully removed. It
    never re-renders, re-translates, or excuses a token the per-token check
    did not already clear.
    """
    tokens = {str(token or "").upper() for token in (flagged_tokens or [])}
    if not tokens:
        return False
    if float(source_text_coverage or 0.0) < 1.0:
        return False
    forgiven = {str(token or "").upper() for token in (forgiven_ocr_noise_tokens or [])}
    return tokens <= forgiven


@dataclass
class SelectiveRetryOutcome:
    region_key: tuple
    cause: str
    attempted: bool
    resolved: bool
    manual_review_required: bool
    detail: dict = field(default_factory=dict)


def run_selective_retry(
    record: dict,
    *,
    page_index=None,
    translate_fn: Optional[Callable] = None,
    validate_fn: Optional[Callable] = None,
) -> SelectiveRetryOutcome:
    """Apply the class-specific retry to one terminal-item record.

    ``record`` is one entry from ``quality_report.json``'s
    ``pages[*].translation_terminal_items`` (or any object exposing the same
    keys). Never clears ``manual_review_required`` unless the retried
    candidate is explicitly proven valid by the same gates the caller wires
    in - on any exception, missing dependency, or failed gate the region comes
    back exactly as review-required as it went in, so it can never
    disappear silently.
    """
    region_id = record.get("id") or record.get("region_id")
    key = (page_index, region_id)
    cause = classify_review_cause(
        quality_reasons=record.get("quality_reasons", ()),
        translation_final_reason=record.get("translation_final_reason", ""),
        translation_validation_reason=record.get("translation_validation_reason", ""),
        translation_final_state=record.get("translation_final_state", ""),
        art_reconstruction_status=record.get("art_reconstruction_status", ""),
    )
    still_required = bool(record.get("manual_review_required"))

    if cause == "branding":
        # Policy allows this text to remain in English (URL/credits/SFX).
        # Retrying it as ordinary story text would be exactly the mistake
        # the mission forbids, so this module refuses to touch it.
        return SelectiveRetryOutcome(key, cause, False, False, still_required, {})

    if cause == "ocr":
        result = retry_ocr_region(record.get("text", ""))
        detail = {
            "before": result.original_text,
            "after": result.repaired_text,
            "changed": result.changed,
            "repair_reason": result.reason,
        }
        if not result.changed:
            return SelectiveRetryOutcome(key, cause, True, False, still_required, detail)
        # A changed source text still needs a real translation + validation
        # pass before the region can leave review; this module does not
        # decide that on its own without those being wired in.
        if translate_fn is None or validate_fn is None:
            detail["note"] = "ocr_text_repaired_but_no_translation_pass_wired"
            return SelectiveRetryOutcome(key, cause, True, False, still_required, detail)
        fidelity = retry_fidelity_region(
            result.repaired_text,
            record.get("translation", ""),
            classification=record.get("classification", "speech"),
            allowed_names=record.get("detected_proper_names") or [],
            validation_reason="ocr_retry",
            translate_fn=translate_fn,
            validate_fn=validate_fn,
        )
        detail["translation_candidate"] = fidelity.candidate
        detail["translation_valid"] = fidelity.valid
        detail["translation_reason"] = fidelity.reason
        resolved = fidelity.attempted and fidelity.valid
        return SelectiveRetryOutcome(key, cause, True, resolved, not resolved, detail)

    if cause == "fidelity":
        fidelity = retry_fidelity_region(
            record.get("text", ""),
            record.get("translation_candidate") or record.get("translation", ""),
            classification=record.get("classification", "speech"),
            allowed_names=record.get("detected_proper_names") or [],
            validation_reason=record.get("translation_validation_reason", ""),
            translate_fn=translate_fn,
            validate_fn=validate_fn,
        )
        detail = {
            "candidate": fidelity.candidate,
            "valid": fidelity.valid,
            "reason": fidelity.reason,
            "attempted": fidelity.attempted,
        }
        resolved = fidelity.attempted and fidelity.valid
        return SelectiveRetryOutcome(key, cause, fidelity.attempted, resolved, not resolved, detail)

    if cause == "render":
        attempts = record.get("visual_attempts") or []
        last = attempts[-1] if attempts else {}
        post_render = (last or {}).get("post_render_ocr") or {}
        flagged = post_render.get("residual_source_tokens") or post_render.get(
            "detected_residual_tokens"
        ) or []
        forgivable = render_residual_forgivable(
            flagged_tokens=flagged,
            forgiven_ocr_noise_tokens=post_render.get("forgiven_ocr_noise_tokens") or [],
            source_text_coverage=(post_render.get("source_text_coverage") or 0.0),
        )
        detail = {
            "flagged_tokens": flagged,
            "source_text_coverage": post_render.get("source_text_coverage"),
            "forgivable": forgivable,
        }
        return SelectiveRetryOutcome(key, cause, True, forgivable, not forgivable, detail)

    return SelectiveRetryOutcome(key, cause, False, False, still_required, {})
