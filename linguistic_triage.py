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

GATE_VERSION = "3"

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


# --- cache proposals --------------------------------------------------------
# A cached answer is only useful when it actually carries a correction that can
# be applied to this region. "There is a cache entry" is not the same as "there
# is a fix", and treating them alike is how a region silently looks resolved.
CACHE_USABLE = "usable_correction"        # a fix exists and can be drafted
CACHE_ANSWERED_NO_CHANGE = "answered_no_change"   # already reviewed, nothing to apply
CACHE_ANSWERED_DEFERRED = "answered_deferred"     # model declined; a human must decide
CACHE_ABSENT = "absent"                   # never asked


def evaluate_cache_proposal(*, source_text: str, current_translation: str,
                            cached_review: dict | None, region_id: str = "") -> dict:
    """Classify what a cached review offers for one region.

    Two different questions are kept apart, because conflating them either
    invents a correction that does not exist or re-asks the provider about a
    region it already answered:

    - ``usable``  — there is a correction that can actually be drafted.
    - ``answered`` — the provider already ruled on this region, so asking again
      buys nothing.
    """
    source = str(source_text or "").strip()
    current = str(current_translation or "").strip()
    if not cached_review:
        return {"state": CACHE_ABSENT, "usable": False, "answered": False,
                "reason": "no_cached_answer", "proposal": ""}

    # Lineage: an answer recorded for a different region can never be reused.
    cached_region = str(cached_review.get("region_id") or "")
    if region_id and cached_region and cached_region != str(region_id):
        return {"state": CACHE_ABSENT, "usable": False, "answered": False,
                "reason": "cached_answer_belongs_to_another_region", "proposal": ""}

    action = str(cached_review.get("action") or "").strip().lower()
    proposal = str(cached_review.get("revised_translation") or "").strip()

    if action in ("keep", "preserve_original"):
        return {"state": CACHE_ANSWERED_NO_CHANGE, "usable": False, "answered": True,
                "reason": "cached_answer_keeps_the_original", "proposal": ""}
    if action == "manual_review":
        return {"state": CACHE_ANSWERED_DEFERRED, "usable": False, "answered": True,
                "reason": "cached_answer_defers_to_a_human", "proposal": ""}
    if not proposal:
        return {"state": CACHE_ANSWERED_NO_CHANGE, "usable": False, "answered": True,
                "reason": "cached_proposal_is_empty", "proposal": ""}
    if source and _fold(proposal) == _fold(source):
        return {"state": CACHE_ANSWERED_NO_CHANGE, "usable": False, "answered": True,
                "reason": "cached_proposal_equals_source", "proposal": proposal}
    if current and _fold(proposal) == _fold(current):
        return {"state": CACHE_ANSWERED_NO_CHANGE, "usable": False, "answered": True,
                "reason": "cached_proposal_already_applied", "proposal": proposal}
    return {"state": CACHE_USABLE, "usable": True, "answered": True,
            "reason": "cached_correction_available", "proposal": proposal}


# --- targeted OCR candidates ------------------------------------------------
def ocr_reprocessing_candidates(records: list[dict], *, decisions: dict | None = None) -> dict:
    """Regions whose source text cannot be trusted and would need a fresh read.

    Structural only: an explicit human ocr_invalid verdict, an unreadable
    normalized class, or a gate that flagged the source as unreadable.
    """
    decisions = decisions or {}
    candidates = []
    for record in records:
        region_id = str(record.get("region_id") or "")
        verdict = str((decisions.get(region_id) or {}).get("decision") or "")
        category = str(record.get("classification_normalized") or "")
        gate = record.get("linguistic_gate") or {}
        gate_flagged = "source_text_unreadable" in (gate.get("reason_codes") or [])
        if not (verdict == "ocr_invalid" or tax.is_unreadable(category) or gate_flagged):
            continue
        candidates.append({
            "page_id": record.get("page_id"), "page_number": record.get("page_number"),
            "region_id": region_id,
            "classification_normalized": category,
            "source_text": record.get("source_text"),
            "confidence": record.get("confidence"),
            "reason_codes": list(record.get("reason_codes") or []),
            "gate_reason_codes": list(gate.get("reason_codes") or []),
            "human_decision": verdict,
            "requested_action": "targeted_ocr",
        })
    pages = sorted({str(c["page_id"] or "") for c in candidates})
    return {"candidates": candidates, "candidate_count": len(candidates),
            "pages": pages, "page_count": len(pages), "ocr_executed": False}


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


def provider_exclusion_reason(record: dict, verdict: str = "") -> str | None:
    """Why this region does *not* need a provider call, or None if it does.

    One source of truth: the provider set, the editorial state and the counters
    all ask this, so they cannot drift apart.
    """
    category = str(record.get("classification_normalized") or "")
    effect = tax.decision_effect(verdict)
    if effect == "preserve" or verdict in ("ocr_invalid", "dismissed"):
        return _EXCLUDED_REASONS["decision"]
    if tax.is_preservable(category) and effect != "translate":
        return _EXCLUDED_REASONS["preservable"]
    if tax.is_unreadable(category):
        return _EXCLUDED_REASONS["unreadable"]
    # "Answered by the provider" only counts as resolved when the result
    # actually holds up. A cached answer that left the region reading like
    # the source (or empty) resolved nothing, so it still needs a call.
    gate_status = (record.get("linguistic_gate") or {}).get("status")
    if not record.get("provider_required") and gate_status != FAILED:
        return _EXCLUDED_REASONS["cache"]
    if not (record.get("translatable") or effect == "translate"):
        return _EXCLUDED_REASONS["not_translatable"]
    return None


def minimal_provider_set(records: list[dict], *, decisions: dict | None = None) -> dict:
    """Regions that genuinely still need a provider call, plus why others do not.

    A region that would otherwise qualify but whose text a human has not yet
    ruled on is held in ``awaiting_editorial`` instead of being billed.
    """
    decisions = decisions or {}
    included, excluded, awaiting = [], [], []
    for record in records:
        decision = decisions.get(str(record.get("region_id") or "")) or {}
        verdict = str(decision.get("decision") or "")
        state = classify_editorial_state(record, decision=decision)
        item = {**_slim(record), "editorial_reasons": state["reasons"],
                "ocr_status": state["ocr_assessment"]["status"]}
        if verdict:
            item["human_decision"] = verdict
            item["human_decision_id"] = str(decision.get("audit_decision_id") or "")
        if state["state"] == READY_FOR_AI_REVIEW:
            included.append({**item, "risk": _risk(record)})
        elif state["state"] == AWAITING_EDITORIAL_DECISION:
            awaiting.append(item)
        else:
            excluded.append({**item, "excluded_reason":
                             provider_exclusion_reason(record, verdict)
                             or _EXCLUDED_REASONS["unreadable"]})

    pages = sorted({str(item.get("page_id") or "") for item in included})
    return {
        "items": included,
        "excluded": excluded,
        "awaiting_editorial": awaiting,
        "awaiting_editorial_count": len(awaiting),
        "estimated_requests": len(included),   # one region per request
        "pages": pages,
        "page_count": len(pages),
        "excluded_count": len(excluded),
        "authorization": authorization_status(records, decisions=decisions),
    }


# --- editorial state --------------------------------------------------------
# Three mutually exclusive open states, in precedence order: a region whose text
# cannot be trusted is not an editorial question, and an editorial question is
# not yet a provider call.
TARGETED_OCR_PENDING = "targeted_ocr_pending"
AWAITING_EDITORIAL_DECISION = "awaiting_editorial_decision"
READY_FOR_AI_REVIEW = "ready_for_ai_review"
SETTLED = "settled"

# Reasons that keep a billable region from being billed. Each one is a question
# only a human can close.
_OCR_REASONS = ("ocr_read_implausible", "ocr_read_ambiguous")


def classify_editorial_state(record: dict, *, decision: dict | None = None) -> dict:
    """Where one region stands between a bad read and a provider call.

    This is the only authority on whether a region gets billed: the provider
    set and the counters both derive from it, so "ready for ai review" and
    "estimated requests" can never disagree.
    """
    decision = decision or {}
    verdict = str(decision.get("decision") or "")
    effect = tax.decision_effect(verdict)
    category = str(record.get("classification_normalized") or "")
    assessment = assess_ocr_plausibility(
        source_text=record.get("source_text") or "",
        confidence=record.get("confidence"),
        classification=category,
        container_evidence={"proven_proper_name": category == tax.PROPER_NAME_PRESERVE})

    reasons: list[str] = []
    if verdict == "ocr_invalid":
        state, reasons = TARGETED_OCR_PENDING, ["human_confirmed_ocr_invalid"]
    elif tax.is_unreadable(category):
        state, reasons = TARGETED_OCR_PENDING, ["unreadable_classification"]
    else:
        exclusion = provider_exclusion_reason(record, verdict)
        if exclusion is not None:
            # Never going to a provider anyway: an open question about it would
            # only be noise, so it does not block authorization.
            state, reasons = SETTLED, [exclusion]
        elif effect == "translate":
            # An explicit ruling closes the question, ambiguous read or not.
            state, reasons = READY_FOR_AI_REVIEW, [f"human_decision_{verdict}"]
        else:
            # A human asking for more review is a non-decision: it must keep the
            # region out of the provider set, not quietly let it through.
            if verdict == "needs_review":
                reasons.append("human_requested_review")
            if assessment["status"] == LIKELY_OCR_INVALID:
                reasons.append("ocr_read_implausible")
            elif assessment["status"] == AMBIGUOUS_OCR:
                reasons.append("ocr_read_ambiguous")
            if record.get("needs_human_review"):
                reasons.append("taxonomy_needs_human_review")
            state = AWAITING_EDITORIAL_DECISION if reasons else READY_FOR_AI_REVIEW
    if not reasons:
        reasons = ["source_text_trusted"]
    return {"state": state, "reasons": reasons, "ocr_assessment": assessment,
            "human_decision": verdict}


def authorization_status(records: list[dict], *, decisions: dict | None = None) -> dict:
    """Whether the chapter may be authorized, and precisely what blocks it."""
    decisions = decisions or {}
    ocr_blocked, editorial_blocked, billable = [], [], 0
    for record in records:
        decision = decisions.get(str(record.get("region_id") or "")) or {}
        state = classify_editorial_state(record, decision=decision)
        if state["state"] == READY_FOR_AI_REVIEW:
            billable += 1
        elif state["state"] == AWAITING_EDITORIAL_DECISION:
            region_id = str(record.get("region_id") or "")
            if any(r in _OCR_REASONS for r in state["reasons"]):
                ocr_blocked.append(region_id)
            else:
                editorial_blocked.append(region_id)
    # An unresolved read is reported ahead of an editorial question: re-reading
    # the text can change what the editorial question even is.
    if ocr_blocked:
        status = "blocked_by_ocr_review"
    elif editorial_blocked:
        status = "blocked_pending_editorial_decisions"
    else:
        status = "ready_for_human_authorization"
    return {"status": status, "billable": billable,
            "blocked_by_ocr_review": sorted(ocr_blocked),
            "blocked_pending_editorial_decisions": sorted(editorial_blocked),
            "provider_executed": False}


def ocr_invalid_candidates(records: list[dict], *, decisions: dict | None = None) -> dict:
    """Regions whose read looks corrupted, split by how sure the detector is.

    ``auto_markable`` items carry unambiguous evidence and may be confirmed in
    bulk; ``review_required`` items must be judged one at a time. Nothing here
    is marked without a human saying so.
    """
    decisions = decisions or {}
    auto, review, confirmed = [], [], []
    for record in records:
        decision = decisions.get(str(record.get("region_id") or "")) or {}
        state = classify_editorial_state(record, decision=decision)
        assessment = state["ocr_assessment"]
        item = {**_slim(record), "ocr_assessment": assessment,
                "editorial_state": state["state"],
                "human_decision": state["human_decision"],
                "human_decision_id": str(decision.get("audit_decision_id") or ""),
                "requested_action": "targeted_ocr"}
        if state["human_decision"] == "ocr_invalid":
            confirmed.append(item)
        elif assessment["status"] == LIKELY_OCR_INVALID:
            auto.append(item)
        elif assessment["status"] == AMBIGUOUS_OCR:
            review.append(item)
    pages = sorted({str(i.get("page_id") or "") for i in auto + review + confirmed})
    return {"auto_markable": auto, "review_required": review, "confirmed": confirmed,
            "auto_markable_count": len(auto), "review_required_count": len(review),
            "confirmed_count": len(confirmed),
            "pages": pages, "page_count": len(pages), "ocr_executed": False}


def pending_editorial_decisions(records: list[dict], *, decisions: dict | None = None) -> dict:
    """Regions a human must rule on before any provider call is authorized."""
    decisions = decisions or {}
    items = []
    for record in records:
        decision = decisions.get(str(record.get("region_id") or "")) or {}
        state = classify_editorial_state(record, decision=decision)
        if state["state"] != AWAITING_EDITORIAL_DECISION:
            continue
        items.append({**_slim(record), "editorial_reasons": state["reasons"],
                      "ocr_assessment": state["ocr_assessment"],
                      "human_decision": state["human_decision"],
                      "human_decision_id": str(decision.get("audit_decision_id") or "")})
    items.sort(key=lambda i: str(i.get("region_id") or ""))
    pages = sorted({str(i.get("page_id") or "") for i in items})
    return {"items": items, "item_count": len(items),
            "pages": pages, "page_count": len(pages)}


def editorial_counters(records: list[dict], *, decisions: dict | None = None) -> dict:
    """The three separate totals: they must never be merged into one number."""
    decisions = decisions or {}
    counts = {TARGETED_OCR_PENDING: 0, AWAITING_EDITORIAL_DECISION: 0,
              READY_FOR_AI_REVIEW: 0, SETTLED: 0}
    for record in records:
        decision = decisions.get(str(record.get("region_id") or "")) or {}
        counts[classify_editorial_state(record, decision=decision)["state"]] += 1
    return {
        "targeted_ocr_pending": counts[TARGETED_OCR_PENDING],
        "awaiting_editorial_decision": counts[AWAITING_EDITORIAL_DECISION],
        "ready_for_ai_review": counts[READY_FOR_AI_REVIEW],
        "settled": counts[SETTLED],
        "total": len(records),
        "authorization_blocked": counts[AWAITING_EDITORIAL_DECISION] > 0,
    }


def _slim(record: dict) -> dict:
    keep = ("page_id", "page_number", "region_id", "classification_original",
            "classification_normalized", "semantic_role", "source_text",
            "current_translation", "reason_codes", "cache_status",
            "cache_correction_available", "provider_required", "confidence",
            "needs_human_review", "suggested_action", "reviewable",
            "translatable", "preservable", "bounding_box",
            "context_before", "context_after", "linguistic_gate")
    return {k: record.get(k) for k in keep}


def _risk(record: dict) -> str:
    gate = (record.get("linguistic_gate") or {}).get("status")
    if gate == FAILED:
        return "high"
    if gate == NEEDS_REVIEW or record.get("needs_human_review"):
        return "medium"
    return "low"


# --- OCR plausibility -------------------------------------------------------
# Recognises the *shape* of a corrupted read, never a known token. A rare name,
# an acronym, a short sfx or a fictional term must never be discarded
# automatically, so a single weak signal is not enough: an automatic
# likely_ocr_invalid needs one unambiguous signal or two independent ones.

OCR_PLAUSIBILITY_VERSION = "1"

PLAUSIBLE_SEMANTIC = "plausible_semantic_text"
LIKELY_OCR_INVALID = "likely_ocr_invalid"
AMBIGUOUS_OCR = "ambiguous_review_required"
PRESERVABLE_NONSEMANTIC = "preservable_nonsemantic"

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_URL_SHAPE = re.compile(r"\b[\w-]+\.[A-Za-z]{2,5}\b")
_KNOWN_TLDS = frozenset({"com", "net", "org", "io", "br", "co", "us", "xyz", "info",
                         "gg", "me", "tv", "app", "dev", "site", "online"})
# A word that starts upper and then mixes case mid-token is a classic bad read.
_MIXED_CASE_WORD = re.compile(r"\b(?=[A-Za-z]{3,}\b)[A-Z]{2,}[a-z]|[a-z][A-Z]{2,}")
_ALNUM_TOKEN = re.compile(r"[A-Za-z0-9]+")


def _digit_substituted_words(text: str) -> list[str]:
    """Tokens where a digit sits inside an otherwise alphabetic word.

    An identifier (an invite code, a model number, an id) legitimately mixes
    letters and digits, so it carries *several* digits. A bad read substitutes
    one glyph - 0 for O, 1 for l - inside a word, so it carries exactly one.
    """
    hits = []
    for token in _ALNUM_TOKEN.findall(text):
        digits = [c for c in token if c.isdigit()]
        letters = [c for c in token if c.isalpha()]
        if len(digits) == 1 and len(letters) >= 2:
            hits.append(token)
    return hits


def assess_ocr_plausibility(*, source_text: str, confidence=None,
                            classification: str = "", container_evidence: dict | None = None) -> dict:
    """Structured, explainable judgement about whether a read can be trusted."""
    container_evidence = dict(container_evidence or {})
    text = str(source_text or "")
    stripped = text.strip()
    letters = [c for c in stripped if c.isalpha()]
    digits = [c for c in stripped if c.isdigit()]
    symbols = [c for c in stripped if not c.isalnum() and not c.isspace()]
    total = max(1, len(stripped.replace(" ", "")))
    vowels = [c for c in letters if c.upper() in "AEIOUY" or unicodedata.normalize("NFKD", c)[0].upper() in "AEIOU"]
    words = _words(stripped)

    try:
        conf = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        conf = None

    metrics = {
        "alphabetic_ratio": round(len(letters) / total, 3),
        "digit_ratio": round(len(digits) / total, 3),
        "symbol_ratio": round(len(symbols) / total, 3),
        "vowel_ratio": round(len(vowels) / max(1, len(letters)), 3),
        "repetition_score": round(max((len(m) for m in re.findall(r"(.)\1+", stripped)), default=0) / total, 3),
        "word_count": len(words),
    }

    strong: list[str] = []
    weak: list[str] = []
    positive: list[str] = []

    if _ENCODING_ARTEFACT.search(stripped):
        strong.append("mojibake_detected")
    if _CONTROL_CHARS.search(text):
        strong.append("control_characters_present")
    if _digit_substituted_words(stripped):
        strong.append("digit_inside_word")
    # A url-shaped token whose suffix is not a real network suffix is a corrupted
    # address or watermark — shape-based, not a specific domain.
    for match in _URL_SHAPE.finditer(stripped):
        suffix = match.group(0).rsplit(".", 1)[-1].lower()
        if suffix not in _KNOWN_TLDS:
            strong.append("corrupted_url_or_watermark_suffix")
            break
        positive.append("valid_network_suffix")

    if _MIXED_CASE_WORD.search(stripped):
        weak.append("mixed_case_inside_word")
    if letters and metrics["vowel_ratio"] < 0.2 and len(letters) >= 4:
        weak.append("implausible_vowel_ratio")
    if metrics["symbol_ratio"] > 0.4:
        weak.append("symbol_heavy_text")
    if words and sum(1 for w in words if len(w) <= 2) / len(words) >= 0.6 and len(words) >= 3:
        weak.append("extreme_fragmentation")
    if conf is not None and conf < 0.5:
        weak.append("very_low_ocr_confidence")
    if container_evidence.get("text_unrelated_to_container"):
        weak.append("text_unrelated_to_container")

    # Positive (protective) evidence: things that make a token legitimate even
    # when short or rare. These keep names, acronyms and sfx out of the bin.
    if tax.looks_like_sfx(stripped):
        positive.append("onomatopoeia_shape")
    # "Multiple words" means whitespace-separated words: a single dotted token
    # such as a corrupted address is one token, not a phrase.
    whitespace_words = [w for w in stripped.split() if _words(w)]
    if tax.has_semantic_content(stripped) and len(whitespace_words) >= 2:
        positive.append("multi_word_semantic_text")
    if stripped.isupper() and 2 <= len(stripped.replace(".", "")) <= 6 and stripped.replace(".", "").isalpha():
        positive.append("acronym_shape")   # an acronym is not garbage
    if tax.has_semantic_content(stripped) and "multi_word_semantic_text" not in positive:
        positive.append("plausible_word_shape")
    if container_evidence.get("proven_proper_name"):
        positive.append("proven_proper_name")
    category = str(classification or "")
    if tax.is_preservable(category):
        positive.append("preservable_classification")

    # Encoding damage corrupts the read itself; unlike the shape heuristics it
    # is not softened by the text around it reading like a phrase.
    corrupted_encoding = [c for c in ("mojibake_detected", "control_characters_present")
                          if c in strong]
    if corrupted_encoding:
        status = LIKELY_OCR_INVALID
    elif tax.is_preservable(category) and not strong:
        status = PRESERVABLE_NONSEMANTIC
    elif strong and not ("multi_word_semantic_text" in positive):
        status = LIKELY_OCR_INVALID
    elif len(weak) >= 2 and not positive:
        status = LIKELY_OCR_INVALID
    elif strong or weak:
        status = AMBIGUOUS_OCR
    elif "multi_word_semantic_text" in positive:
        status = PLAUSIBLE_SEMANTIC
    else:
        status = AMBIGUOUS_OCR

    # Confidence in *this* judgement, not in the OCR read.
    if status == LIKELY_OCR_INVALID:
        judgement = 0.9 if strong else 0.7
    elif status == PLAUSIBLE_SEMANTIC:
        judgement = 0.8
    elif status == PRESERVABLE_NONSEMANTIC:
        judgement = 0.75
    else:
        judgement = 0.4

    recommended = {LIKELY_OCR_INVALID: "confirm_ocr_invalid",
                   PLAUSIBLE_SEMANTIC: "keep_as_text",
                   PRESERVABLE_NONSEMANTIC: "confirm_preservation"}.get(status, "human_review")

    return {
        "status": status,
        "confidence": judgement,
        "reason_codes": strong + weak,
        "positive_evidence": positive,
        "strong_evidence": strong,
        "weak_evidence": weak,
        **metrics,
        "lexical_plausibility": round(len(positive) / (len(positive) + len(strong) + len(weak) + 1), 3),
        "encoding_flags": corrupted_encoding,
        "container_evidence": container_evidence,
        "classification_evidence": category,
        "recommended_action": recommended,
        "auto_markable": status == LIKELY_OCR_INVALID,
    }
