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
from typing import Any

import audit_decisions
import linguistic_triage

EXECUTION_VERSION = "1"
_REQUESTS_NAME = "provider_authorization_requests.json"

READY = "ready_for_human_authorization"
EXECUTED = "executed"


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


def execute(output_dir: str, request: dict[str, Any], plan: dict[str, Any], *,
            translator, revision_id: str, confirm: bool = False) -> dict[str, Any]:
    """Send the planned texts and record the outcome. Additive writes only."""
    if not confirm:
        raise ValueError("explicit_confirmation_required")
    if not getattr(translator, "is_configured", False):
        raise ValueError("provider_not_configured")
    items = plan["items"]
    if not items:
        raise ValueError("nothing_to_send")

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
