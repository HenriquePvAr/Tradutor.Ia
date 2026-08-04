"""Run a human-authorized provider set, and nothing else (BLOCO 6B).

The authorization request is the contract. This module refuses to send anything
that is not in it, refuses to send it twice, and refuses to send it at all if
the audit moved underneath the request. It writes only translation results into
the additive audit area: no page, no PDF, no revision, no publication is
touched, and no new one is created.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import audit_decisions
import linguistic_triage
import visual_refinement_provider_guard

EXECUTION_VERSION = "1"
_REQUESTS_NAME = "provider_authorization_requests.json"

READY = "ready_for_human_authorization"
EXECUTED = "executed"

# The only reasons a first-pass rejection is worth retrying once: an empty
# result, source-language residue, or the candidate echoing the source
# verbatim (the only real "literal" signal the gate has - see
# .runtime/claude-translation-quality-retry/triage_contract.md). Anything
# else (encoding damage, needs_review-only signals) is terminal: retrying
# would not fix it, so it goes straight to review instead of spending a
# second call.
_RECOVERABLE_GATE_REASONS = frozenset({
    "empty_translation", "source_language_residual", "candidate_equals_source",
})


class ProviderCallNotAuthorized(RuntimeError):
    """Raised the moment a path tries to reach a provider it may not use."""


class NoProviderReviewer:
    """A reviewer that cannot review.

    Rendering a line a human already wrote must not be able to call out, so the
    preview path is given this instead of a real client: any attempt to review,
    health-check or translate fails loudly rather than quietly spending a
    request. Structural, not a convention someone can forget.
    """

    model = "no-provider-guard"
    base_url = ""
    requests = 0
    valid_batches = 0
    repaired_batches = 0
    fallback_individual = 0
    invalid_batches = 0

    def _refuse(self, *_args, **_kwargs):
        raise ProviderCallNotAuthorized(
            visual_refinement_provider_guard.FORBIDDEN_REASON)

    review_batch = _refuse
    translate_many = _refuse
    translate = _refuse
    health_check = _refuse


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def requests_path(output_dir: str) -> Path:
    return Path(output_dir) / "quality_revision" / _REQUESTS_NAME


def find_request(output_dir: str, request_id: str) -> dict[str, Any] | None:
    for entry in _read_json(requests_path(output_dir), {}).get("requests", []) or []:
        if isinstance(entry, dict) and str(entry.get("authorization_request_id")) == str(request_id):
            return entry
    return None


def resolve_text_to_send(record: dict[str, Any], decision: dict[str, Any] | None) -> tuple[str, str]:
    """The text that actually goes to the provider, and where it came from.

    A human who looked at the crop and wrote down what it really says has
    produced a better source than the stored OCR read; sending the degraded read
    instead would spend the call on text nobody believes.
    """
    corrected = audit_decisions.corrected_reading(decision or {})
    if corrected:
        return corrected, "human_corrected_reading"
    return str(record.get("source_text") or ""), "ocr_source_text"


def plan_execution(request: dict[str, Any], records: list[dict[str, Any]], *,
                   decisions: dict[str, Any] | None = None,
                   source_audit_hash: str = "") -> dict[str, Any]:
    """Decide exactly what may be sent. Pure: it never calls anything.

    Fails closed on every drift: a request already executed, a request that is
    not ready, an audit that changed since it was created, a region that left
    the request's scope, or a region that is no longer billable.
    """
    if not request:
        raise ValueError("authorization_request_not_found")
    if bool(request.get("provider_executed")):
        raise ValueError("authorization_request_already_executed")
    if str(request.get("status") or "") != READY:
        raise ValueError("authorization_request_not_ready")
    if source_audit_hash and str(request.get("source_audit_hash") or "") != str(source_audit_hash):
        # The request was approved against a different audit; re-approve it.
        raise ValueError("source_audit_hash_mismatch")

    authorized = [str(r) for r in (request.get("region_ids") or [])]
    if not authorized:
        raise ValueError("authorization_request_has_no_regions")
    if len(set(authorized)) != len(authorized):
        # A duplicate would either bill the same region twice or let a second
        # retry silently overwrite the first result under the same key.
        raise ValueError("duplicate_region_id_in_authorization")

    decisions = decisions or {}
    by_region = {str(r.get("region_id")): r for r in records}
    missing = [r for r in authorized if r not in by_region]
    if missing:
        raise ValueError("region_not_in_audit")

    items, drifted = [], []
    for region_id in authorized:
        record = by_region[region_id]
        decision = decisions.get(region_id) or {}
        state = linguistic_triage.classify_editorial_state(record, decision=decision)
        if state["state"] != linguistic_triage.READY_FOR_AI_REVIEW:
            drifted.append({"region_id": region_id, "state": state["state"],
                            "reasons": state["reasons"]})
            continue
        text, origin = resolve_text_to_send(record, decision)
        if not text.strip():
            drifted.append({"region_id": region_id, "state": "empty_source_text",
                            "reasons": ["nothing_to_translate"]})
            continue
        items.append({
            "region_id": region_id,
            "page_id": record.get("page_id"),
            "page_number": record.get("page_number"),
            "text": text,
            "text_origin": origin,
            "ocr_source_text": record.get("source_text"),
            "current_translation": record.get("current_translation"),
            "classification_normalized": record.get("classification_normalized"),
            "human_decision": str(decision.get("decision") or ""),
        })
    if drifted:
        # Partially sending an approved set would spend money on a scope the
        # human never saw. Refuse the whole thing and say what moved.
        raise ValueError(f"authorized_scope_drifted:{json.dumps(drifted, ensure_ascii=False)}")
    return {"items": items, "region_ids": authorized,
            "pages": sorted({str(i["page_id"] or "") for i in items}),
            "estimated_requests": len(items)}


def _gate_for(item: dict[str, Any], translation: str) -> dict[str, Any]:
    return linguistic_triage.evaluate_linguistic_gate(
        source_text=item["text"], current_translation=translation,
        policy={"normalized_classification": item.get("classification_normalized") or ""})


def _retry_once_if_recoverable(entry: dict[str, Any], *, translator,
                                should_cancel: Callable[[], bool]) -> None:
    """At most one extra provider call for this region. Mutates ``entry``.

    Fails closed: any exception from the retry call, or a second rejection,
    leaves the region flagged for review rather than guessing a result.
    """
    gate = _gate_for(entry, entry["translation"])
    entry["linguistic_gate"] = {"status": gate["status"], "reason_codes": gate["reason_codes"]}
    entry["review_required"] = gate["status"] == linguistic_triage.FAILED
    entry["quality_retry_attempted"] = False
    if gate["status"] != linguistic_triage.FAILED:
        return

    recoverable_reasons = sorted(set(gate["reason_codes"]) & _RECOVERABLE_GATE_REASONS)
    if not recoverable_reasons or not hasattr(translator, "translate_strict"):
        return  # terminal reason (or a translator that never learned the retry call)

    if should_cancel():
        entry["quality_retry_skipped_reason"] = "cancellation_requested"
        return

    entry["quality_retry_attempted"] = True
    reason_code = recoverable_reasons[0]
    try:
        candidate = translator.translate_strict(
            entry["text"], previous_translation=entry["translation"],
            validation_reason=reason_code)
    except Exception as exc:  # noqa: BLE001 - one region's failure must not sink the run
        entry["quality_retry_error"] = type(exc).__name__
        return

    candidate = str(candidate or "").strip()
    retry_gate = _gate_for(entry, candidate)
    entry["quality_retry_gate"] = {"status": retry_gate["status"],
                                    "reason_codes": retry_gate["reason_codes"]}
    if retry_gate["status"] == linguistic_triage.FAILED:
        return  # still rejected: no third attempt, stays review_required

    entry["translation"] = candidate
    entry["translated"] = bool(candidate)
    entry["linguistic_gate"] = {"status": retry_gate["status"],
                                 "reason_codes": retry_gate["reason_codes"]}
    entry["review_required"] = False


def execute(output_dir: str, request: dict[str, Any], plan: dict[str, Any], *,
            translator, revision_id: str, confirm: bool = False,
            should_cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Send the planned texts and record the outcome. Additive writes only."""
    if not confirm:
        raise ValueError("explicit_confirmation_required")
    if not getattr(translator, "is_configured", False):
        raise ValueError("provider_not_configured")
    items = plan["items"]
    if not items:
        raise ValueError("nothing_to_send")
    should_cancel = should_cancel or (lambda: False)

    before = int((getattr(translator, "stats", {}) or {}).get("api_requests", 0))
    texts = [item["text"] for item in items]
    # The provider only ever sees the authorized texts, in order.
    translations = translator.translate_many(texts)
    if len(translations) != len(items):
        raise ValueError("provider_returned_mismatched_count")
    stats = dict(getattr(translator, "stats", {}) or {})

    results = []
    for item, translation in zip(items, translations):
        results.append({**item, "translation": str(translation or ""),
                        "translated": bool(str(translation or "").strip())})

    # Quality gate, region by region: a bad result from one region is never
    # allowed to touch another's text, so each retry is fully self-contained.
    for entry in results:
        _retry_once_if_recoverable(entry, translator=translator, should_cancel=should_cancel)

    record = {
        "execution_version": EXECUTION_VERSION,
        "authorization_request_id": str(request.get("authorization_request_id")),
        "revision_id": str(revision_id),
        "source_audit_hash": str(request.get("source_audit_hash") or ""),
        "executed_at": _now(),
        # Model identity only. A credential is never read here nor recorded.
        "provider_model": str(getattr(translator, "model", "")),
        "provider_base_url": str(getattr(translator, "base_url", "")),
        "api_requests": int(stats.get("api_requests", 0)) - before,
        "api_texts": int(stats.get("api_texts", 0)),
        "cache_hits": int(stats.get("cache_hits", 0)),
        "region_ids": plan["region_ids"],
        "pages": plan["pages"],
        "results": results,
        "applied_to_pdf": False,
        "applied_to_pages": False,
        "published": False,
    }

    out_dir = Path(output_dir) / "quality_revision" / "linguistic_audit" / str(revision_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"provider_execution_{record['authorization_request_id']}.json"
    target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    record["artifact_path"] = str(target)

    # Mark the request spent so it can never run twice.
    path = requests_path(output_dir)
    payload = _read_json(path, {})
    entries = [e for e in (payload.get("requests") or []) if isinstance(e, dict)]
    for entry in entries:
        if str(entry.get("authorization_request_id")) == record["authorization_request_id"]:
            entry["status"] = EXECUTED
            entry["provider_executed"] = True
            entry["executed_at"] = record["executed_at"]
            entry["execution_artifact"] = target.name
            entry["api_requests"] = record["api_requests"]
    path.write_text(json.dumps({"requests": entries, "updated_at": _now()},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    return record
