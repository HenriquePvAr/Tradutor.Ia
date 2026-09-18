"""Read-only region-state audit helpers used by Mission 2 fixtures.

This module deliberately does not participate in the production pipeline.  It
turns the existing ``TextGroup`` state into deterministic counts so a missing
translation cannot be hidden by a later rendering stage.
"""

from collections import Counter


def _truthy(value):
    return bool(value)


def classify_region_terminal(group):
    """Return one terminal class for an existing region object."""
    state = str(getattr(group, "translation_final_state", "") or "").strip()
    text = str(getattr(group, "text", "") or "").strip()
    translated = str(getattr(group, "translation", "") or "").strip()
    if state in {"manual_review", "review_required"} or _truthy(
        getattr(group, "manual_review_required", False)
    ):
        return "review_required"
    if state in {"translation_failed", "rejected"} or _truthy(
        getattr(group, "translation_unresolved", False)
    ):
        return "explicit_failure"
    if not text:
        return "non_translatable"
    if _truthy(getattr(group, "ignored", False)) or not _truthy(
        getattr(group, "sent_to_translation", False)
    ):
        return "non_translatable"
    if state == "translated" and translated:
        return "translated"
    return "silent_missing"


def classify_region_issue(group):
    """Return a testable taxonomy code for the first observed loss point."""
    text = str(getattr(group, "text", "") or "").strip()
    sent = _truthy(getattr(group, "sent_to_translation", False))
    translation = str(getattr(group, "translation", "") or "").strip()
    state = str(getattr(group, "translation_final_state", "") or "").strip()
    if not text:
        return "UT1_OCR_NO_TEXT"
    if _truthy(getattr(group, "ignored", False)):
        return "UT2_OCR_TEXT_FILTERED"
    if not sent:
        return "UT4_TRANSLATION_NOT_REQUESTED"
    if state == "translation_failed":
        return "UT6_TRANSLATION_ERROR"
    if not translation and state in {"rejected", "review_required", "manual_review"}:
        return "UT5_TRANSLATION_EMPTY_RESPONSE"
    if not translation and state == "":
        return "UT7_TRANSLATED_TEXT_NOT_ASSIGNED"
    if state in {"manual_review", "review_required"}:
        return "UT12_EXPLICIT_REVIEW"
    if float(getattr(group, "text_overflow_ratio", 0.0) or 0.0) > 0 and not int(getattr(group, "translation_retry_count", 0) or 0):
        return "UT10_OVERFLOW_RETRY_SKIPPED"
    return ""


def summarize_translation_region_states(groups):
    """Summarize existing region objects without mutating them.

    Fields are intentionally derived from the established ``TextGroup``
    attributes (``sent_to_translation``, ``translation_final_state``,
    ``translation_retry_count`` and ``text_overflow_ratio``).
    """
    groups = list(groups or [])
    out = Counter()
    for group in groups:
        text = str(getattr(group, "text", "") or "").strip()
        sent = _truthy(getattr(group, "sent_to_translation", False))
        translation = str(getattr(group, "translation", "") or "").strip()
        state = str(getattr(group, "translation_final_state", "") or "").strip()
        retry_count = int(getattr(group, "translation_retry_count", 0) or 0)
        overflow = float(getattr(group, "text_overflow_ratio", 0.0) or 0.0) > 0
        terminal = classify_region_terminal(group)

        out["TOTAL_REGIONS"] += 1
        out["OCR_TEXT_REGIONS"] += bool(text)
        out["OCR_EMPTY_REGIONS"] += not bool(text)
        out["TRANSLATABLE_REGIONS"] += sent
        out["NON_TRANSLATABLE_REGIONS"] += not sent
        out["TRANSLATION_REQUESTED_REGIONS"] += sent
        out["TRANSLATION_SUCCESS_NONEMPTY_REGIONS"] += bool(sent and translation)
        out["TRANSLATION_EMPTY_RESPONSE_REGIONS"] += bool(sent and not translation and state != "translation_failed")
        out["TRANSLATION_ERROR_REGIONS"] += state == "translation_failed"
        out["TRANSLATED_TEXT_ASSIGNED_REGIONS"] += bool(sent and translation)
        out["POST_PROCESS_TRANSLATED_REGIONS"] += bool(state == "translated" and translation)
        out["OVERFLOW_REGIONS"] += overflow
        out["OVERFLOW_RETRIED_REGIONS"] += bool(overflow and retry_count > 0)
        out["FINAL_RENDER_TRANSLATED_REGIONS"] += bool(state == "translated" and translation)
        out["EXPLICIT_FAILURE_REGIONS"] += terminal == "explicit_failure"
        out["EXPLICIT_REVIEW_REGIONS"] += terminal == "review_required"
        out["SILENT_MISSING_REGIONS"] += terminal == "silent_missing"
        out["TERMINAL_CLASSIFICATION_TOTAL"] += 1
        out[f"TERMINAL_{terminal.upper()}"] += 1
    return dict(out)


def canonical_region_snapshot(groups):
    """Produce a timing-independent semantic snapshot for comparisons."""
    rows = []
    for index, group in enumerate(groups or []):
        rows.append(
            (
                int(getattr(group, "page_index", 0) or 0),
                str(getattr(group, "region_id", "") or getattr(group, "group_id", index)),
                classify_region_terminal(group),
                str(getattr(group, "translation", "") or "").strip(),
                int(getattr(group, "translation_retry_count", 0) or 0),
            )
        )
    return tuple(sorted(rows))
