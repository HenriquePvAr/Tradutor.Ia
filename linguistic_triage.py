"""Human linguistic triage: per-region gate, explainable queue, provider set (BLOCO 5).

Pure functions over audit records plus the human decisions already recorded. No
provider call, no PDF, no chapter-specific rule: every decision is derived from
the resolved policy, the text itself and the recorded evidence.

The linguistic gate is deliberately separate from the visual gate — a page that
renders cleanly can still carry a wrong translation, and neither gate alone
approves anything.
"""
from __future__ import annotations

import re
import unicodedata

import region_taxonomy as tax

GATE_VERSION = "1"

PASSED = "passed"
FAILED = "failed"
NEEDS_REVIEW = "needs_review"
NOT_APPLICABLE = "not_applicable"

# Latin letters that only appear in the target language's orthography are a cheap,
# language-shaped signal that a candidate was actually translated.
_ACCENTED = re.compile(r"[áàâãéêíóôõúüç]", re.I)
# UTF-8 bytes decoded as Latin-1 leave "Ã"/"Â" followed by a control-range char,
# and an undecodable byte leaves the replacement character.
_ENCODING_ARTEFACT = re.compile("[ÂÃâ][-¿]|�")
_SENTENCE_END = re.compile(r"[.!?…]\s*$")


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W\d_]+", str(text or ""), flags=re.UNICODE)


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(text or "").casefold())
                   if not unicodedata.combining(c))


def evaluate_linguistic_gate(*, source_text: str, current_translation: str,
                             policy: dict, evidence: dict | None = None) -> dict:
    """Per-region linguistic checks. Never mutates anything, never calls out."""
    evidence = dict(evidence or {})
    source = str(source_text or "").strip()
    candidate = str(current_translation or "").strip()
    category = str(policy.get("normalized_classification") or "")
    checks: dict[str, str] = {}
    reasons: list[str] = []

    def mark(name, status, reason=None):
        checks[name] = status
        if reason and status in (FAILED, NEEDS_REVIEW):
            reasons.append(reason)

    # A preserved region is not judged as a translation: keeping it identical is
    # the correct outcome, and changing it would be the defect.
    if tax.is_preservable(category):
        for name in ("language_residual", "mixed_language", "candidate_equals_source",
                     "empty_candidate", "grammar_risk", "truncation"):
            checks[name] = NOT_APPLICABLE
        mark("sfx_translation_risk",
             FAILED if candidate and _fold(candidate) != _fold(source) and source else PASSED,
             "preservable_region_was_altered")
        mark("watermark_change_risk",
             FAILED if category == tax.WATERMARK_PRESERVE and candidate and _fold(candidate) != _fold(source) else PASSED,
             "watermark_text_changed")
        mark("proper_name_risk", PASSED)
        mark("ocr_invalid", NOT_APPLICABLE)
        status = FAILED if FAILED in checks.values() else PASSED
        return {"status": status, "checks": checks, "reason_codes": reasons,
                "gate_version": GATE_VERSION}

    # Unreadable source: nothing may be translated from it.
    if tax.is_unreadable(category):
        for name in ("language_residual", "mixed_language", "candidate_equals_source",
                     "grammar_risk", "truncation", "sfx_translation_risk",
                     "watermark_change_risk", "proper_name_risk"):
            checks[name] = NOT_APPLICABLE
        mark("ocr_invalid", NEEDS_REVIEW, "source_text_unreadable")
        mark("empty_candidate", NEEDS_REVIEW if not candidate else PASSED)
        return {"status": NEEDS_REVIEW, "checks": checks, "reason_codes": reasons,
                "gate_version": GATE_VERSION}

    # Translatable (or still undetermined) region.
    mark("ocr_invalid", PASSED)
    mark("empty_candidate", FAILED if source and not candidate else PASSED, "empty_translation")
    mark("candidate_equals_source",
         FAILED if source and candidate and _fold(candidate) == _fold(source) else PASSED,
         "candidate_equals_source")

    source_tokens = {_fold(w) for w in _words(source) if len(w) >= 3}
    candidate_tokens = [_fold(w) for w in _words(candidate) if len(w) >= 3]
    shared = sum(1 for t in candidate_tokens if t in source_tokens) if candidate_tokens else 0
    ratio = shared / len(candidate_tokens) if candidate_tokens else 0.0
    if not candidate:
        mark("language_residual", NOT_APPLICABLE)
        mark("mixed_language", NOT_APPLICABLE)
    else:
        mark("language_residual", FAILED if ratio >= 0.6 else PASSED, "source_language_residual")
        mark("mixed_language",
             NEEDS_REVIEW if 0.25 <= ratio < 0.6 else PASSED, "mixed_language_candidate")

    if candidate:
        mark("encoding_error", FAILED if _ENCODING_ARTEFACT.search(candidate) else PASSED,
             "encoding_artefact_in_candidate")
        # A translated sentence normally carries target-language orthography.
        multiword = len(_words(source)) >= 3
        mark("grammar_risk",
             NEEDS_REVIEW if multiword and candidate and not _ACCENTED.search(candidate) else PASSED,
             "no_target_language_orthography")
        if source:
            length_ratio = len(candidate) / max(1, len(source))
            mark("truncation", NEEDS_REVIEW if length_ratio < 0.45 else PASSED, "suspicious_truncation")
        mark("punctuation_risk",
             NEEDS_REVIEW if _SENTENCE_END.search(source or "") and not _SENTENCE_END.search(candidate) else PASSED,
             "sentence_punctuation_dropped")
    else:
        for name in ("encoding_error", "grammar_risk", "truncation", "punctuation_risk"):
            checks[name] = NOT_APPLICABLE

    mark("semantic_inversion",
         NEEDS_REVIEW if evidence.get("semantic_inversion_suspected") else PASSED,
         "possible_semantic_inversion")
    mark("terminology_conflict",
         NEEDS_REVIEW if evidence.get("terminology_conflict") else PASSED, "terminology_conflict")
    mark("proper_name_risk", PASSED)
    mark("sfx_translation_risk", PASSED)
    mark("watermark_change_risk", PASSED)

    if FAILED in checks.values():
        status = FAILED
    elif NEEDS_REVIEW in checks.values() or tax.needs_human_review(category):
        status = NEEDS_REVIEW
    else:
        status = PASSED
    return {"status": status, "checks": checks, "reason_codes": reasons,
            "gate_version": GATE_VERSION}


# --- explainable triage queue ----------------------------------------------
# Higher score = looked at sooner. Every contribution carries its own label so
# the UI can show *why* an item is where it is.
_WEIGHTS = (
    ("unreadable_source", 100, lambda p, g, d: tax.is_unreadable(p["normalized_classification"])),
    ("undetermined_class", 90, lambda p, g, d: tax.needs_human_review(p["normalized_classification"])),
    ("preservable_but_flagged", 70, lambda p, g, d: p["preservable"] and g["status"] in (FAILED, NEEDS_REVIEW)),
    ("linguistic_gate_failed", 60, lambda p, g, d: g["status"] == FAILED),
    ("translatable_with_cache", 40, lambda p, g, d: p["translatable"] and not p["provider_required"]),
    ("linguistic_gate_needs_review", 30, lambda p, g, d: g["status"] == NEEDS_REVIEW),
    ("needs_human_review", 20, lambda p, g, d: p["needs_human_review"]),
    ("provider_required", 10, lambda p, g, d: p["provider_required"]),
)


def triage_score(policy: dict, gate: dict, decision: dict | None = None) -> tuple[int, list[str]]:
    """Return (score, explanations). A decided item drops to the bottom."""
    decision = decision or {}
    reasons: list[str] = []
    score = 0
    for label, weight, predicate in _WEIGHTS:
        try:
            hit = bool(predicate(policy, gate, decision))
        except Exception:  # noqa: BLE001 - a malformed record must not break the queue
            hit = False
        if hit:
            score += weight
            reasons.append(label)
    if not reasons:
        # Every item explains its position, including the quiet ones.
        reasons.append("no_open_flags")
    if decision.get("decision"):
        score -= 200  # already triaged by a human: keep it, but out of the way
        reasons.append(f"human_decision_{decision['decision']}")
    return score, reasons


def build_triage_queue(records: list[dict], *, decisions: dict | None = None) -> list[dict]:
    """Order audit records by explainable priority. Deterministic ties."""
    decisions = decisions or {}
    queue = []
    for record in records:
        policy = {
            "normalized_classification": record.get("classification_normalized", ""),
            "translatable": bool(record.get("translatable")),
            "preservable": bool(record.get("preservable")),
            "provider_required": bool(record.get("provider_required")),
            "needs_human_review": bool(record.get("needs_human_review")),
        }
        gate = record.get("linguistic_gate") or {"status": NOT_APPLICABLE, "checks": {}}
        decision = decisions.get(str(record.get("region_id") or ""))
        score, reasons = triage_score(policy, gate, decision)
        queue.append({**record, "triage_score": score, "triage_reasons": reasons,
                      "human_decision": decision})
    # Stable, deterministic: score desc, then region id asc.
    queue.sort(key=lambda item: (-item["triage_score"], str(item.get("region_id") or "")))
    return queue


# --- minimal provider set ---------------------------------------------------
_EXCLUDED_REASONS = {
    "preservable": "preservable_class",
    "unreadable": "ocr_invalid",
    "cache": "resolved_by_cache",
    "decision": "human_decision_excludes",
    "not_translatable": "not_translatable",
}


def minimal_provider_set(records: list[dict], *, decisions: dict | None = None) -> dict:
    """Regions that genuinely still need a provider call, plus why others do not."""
    decisions = decisions or {}
    included, excluded = [], []
    for record in records:
        region_id = str(record.get("region_id") or "")
        decision = decisions.get(region_id) or {}
        verdict = str(decision.get("decision") or "")
        category = str(record.get("classification_normalized") or "")

        if verdict in ("preserve", "ocr_invalid", "dismissed"):
            excluded.append({**_slim(record), "excluded_reason": _EXCLUDED_REASONS["decision"],
                             "human_decision": verdict})
            continue
        if tax.is_preservable(category) and verdict != "translate":
            excluded.append({**_slim(record), "excluded_reason": _EXCLUDED_REASONS["preservable"]})
            continue
        if tax.is_unreadable(category):
            excluded.append({**_slim(record), "excluded_reason": _EXCLUDED_REASONS["unreadable"]})
            continue
        if not record.get("provider_required"):
            excluded.append({**_slim(record), "excluded_reason": _EXCLUDED_REASONS["cache"]})
            continue
        if not (record.get("translatable") or verdict == "translate"):
            excluded.append({**_slim(record), "excluded_reason": _EXCLUDED_REASONS["not_translatable"]})
            continue
        included.append({**_slim(record), "human_decision": verdict,
                         "risk": _risk(record)})

    pages = sorted({str(item.get("page_id") or "") for item in included})
    return {
        "items": included,
        "excluded": excluded,
        "estimated_requests": len(included),   # one region per request
        "pages": pages,
        "page_count": len(pages),
        "excluded_count": len(excluded),
    }


def _slim(record: dict) -> dict:
    keep = ("page_id", "page_number", "region_id", "classification_original",
            "classification_normalized", "source_text", "current_translation",
            "reason_codes", "cache_status", "provider_required")
    return {k: record.get(k) for k in keep}


def _risk(record: dict) -> str:
    gate = (record.get("linguistic_gate") or {}).get("status")
    if gate == FAILED:
        return "high"
    if gate == NEEDS_REVIEW or record.get("needs_human_review"):
        return "medium"
    return "low"
