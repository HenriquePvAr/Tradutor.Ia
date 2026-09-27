"""Minimal, crash-safe handoff for a job's held YK reservation.

The file contains identifiers/state only. Translation content never crosses this
process boundary for wallet settlement; the authenticated backend is authoritative.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

STATE_HELD = "held"
STATE_CONSUMED = "consumed"
STATE_RELEASED = "released"
TERMINAL_STATES = frozenset({STATE_CONSUMED, STATE_RELEASED})
_STATE_FILE = "yk_reservation.json"
_READY_FILE = "translation_ready.json"


def _state_path(output_dir: str | os.PathLike) -> Path:
    return Path(output_dir) / _STATE_FILE


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".yk_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_reservation(output_dir: str | os.PathLike) -> dict | None:
    try:
        value = json.loads(_state_path(output_dir).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return None
        # Be defensive when reading older local state: strip all fields except the
        # minimal settlement contract. Never persist/replay legacy finalize_items.
        allowed = {"schema_version", "job_id", "reservation_id", "request_id", "state", "idempotency_key"}
        return {key: value[key] for key in allowed if key in value}
    except (OSError, ValueError):
        return None


def record_reservation(output_dir: str | os.PathLike, *, job_id: str,
                       reservation_id: str, request_id: str = "", **_ignored: Any) -> dict:
    """Persist only the reservation/job identity immediately after reserve."""
    existing = read_reservation(output_dir)
    if existing and existing.get("state") in TERMINAL_STATES:
        if (existing.get("job_id"), existing.get("reservation_id")) != (str(job_id), str(reservation_id)):
            raise ValueError("reservation_identity_conflict")
        return existing
    safe_job_id, safe_reservation_id = str(job_id).strip(), str(reservation_id).strip()
    if not safe_job_id or not safe_reservation_id:
        raise ValueError("reservation_identity_missing")
    data = {"schema_version": 1, "job_id": safe_job_id,
            "reservation_id": safe_reservation_id,
            "request_id": str(request_id or (existing or {}).get("request_id") or "").strip(),
            "state": (existing or {}).get("state", STATE_HELD)}
    _atomic_write(_state_path(output_dir), data)
    return data


def _idempotency_key(job_id: str, reservation_id: str, action: str) -> str:
    digest = hashlib.sha256(f"{job_id}\0{reservation_id}".encode()).hexdigest()[:32]
    return f"yk-settlement-v1:{digest}:{action}"


def mark_translation_ready(output_dir: str | os.PathLike, *, job_id: str,
                           request_ids: list[str], item_count: int,
                           result_files: list[dict[str, str]]) -> dict:
    """Record a local-only recovery marker after all result batches are durable."""
    ctx = read_reservation(output_dir)
    if not ctx or ctx.get("job_id") != str(job_id) or ctx.get("state") not in {STATE_HELD, STATE_CONSUMED}:
        raise ValueError("translation_reservation_missing")
    if not request_ids or len(set(request_ids)) != len(request_ids) or item_count < 1:
        raise ValueError("translation_result_manifest_invalid")
    if not result_files or len(result_files) != len(request_ids):
        raise ValueError("translation_result_manifest_incomplete")
    ready_path = _state_path(output_dir).with_name(_READY_FILE)
    prior = {}
    try:
        prior = json.loads(ready_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if (isinstance(prior, dict) and prior.get("schema_version") == 1
            and prior.get("job_id") == str(job_id)
            and prior.get("reservation_id") == ctx["reservation_id"]):
        existing = {str(row.get("request_id")): row for row in prior.get("result_files", [])
                    if isinstance(row, dict) and row.get("request_id")}
        existing.update({str(row["request_id"]): row for row in result_files})
        result_files = list(existing.values())
        request_ids = list(existing)
        item_count = sum(int(row.get("item_count") or 0) for row in result_files)
    marker = {
        "schema_version": 1,
        "job_id": str(job_id),
        "reservation_id": ctx["reservation_id"],
        "request_ids": [str(value) for value in request_ids],
        "item_count": int(item_count),
        "result_files": result_files,
    }
    _atomic_write(ready_path, marker)
    return marker


def translation_ready(output_dir: str | os.PathLike, *, job_id: str | None = None) -> bool:
    try:
        marker = json.loads(_state_path(output_dir).with_name(_READY_FILE).read_text(encoding="utf-8"))
        ctx = read_reservation(output_dir)
        valid = bool(
            isinstance(marker, dict) and marker.get("schema_version") == 1
            and ctx and marker.get("job_id") == ctx.get("job_id")
            and marker.get("reservation_id") == ctx.get("reservation_id")
            and (job_id is None or marker.get("job_id") == str(job_id))
            and isinstance(marker.get("request_ids"), list) and marker["request_ids"]
            and isinstance(marker.get("result_files"), list)
            and len(marker["result_files"]) == len(marker["request_ids"])
            and int(marker.get("item_count") or 0) > 0
        )
        if not valid:
            return False
        cache_root = Path(output_dir) / ".translation_results"
        for row in marker["result_files"]:
            if not isinstance(row, dict) or row.get("request_id") not in marker["request_ids"]:
                return False
            cache_name = hashlib.sha256(
                f"{marker['job_id']}\0{row['request_id']}".encode("utf-8")
            ).hexdigest() + ".json"
            cache_path = cache_root / cache_name
            if not cache_path.is_file() or hashlib.sha256(cache_path.read_bytes()).hexdigest() != row.get("sha256"):
                return False
        return True
    except (OSError, ValueError, TypeError):
        return False


def settle(output_dir: str | os.PathLike, *, output_valid: bool, provider) -> dict[str, Any]:
    ctx = read_reservation(output_dir)
    if not ctx or not ctx.get("reservation_id") or not ctx.get("job_id"):
        return {"state": "no_reservation", "net_yk": 0}
    state = str(ctx.get("state") or STATE_HELD)
    if state == STATE_CONSUMED:
        return {"state": STATE_CONSUMED, "net_yk": 1}
    if state == STATE_RELEASED:
        return {"state": STATE_RELEASED, "net_yk": 0}
    if state != STATE_HELD:
        raise ValueError("reservation_state_invalid")
    # Economic commit is tied to durable translated provider output, not to the
    # later local export gate. Output failure after this point is recoverable and
    # must never turn into a client-requested refund.
    ready = translation_ready(output_dir, job_id=ctx["job_id"])
    # ``output_valid`` remains in the signature for older callers, but it is no
    # longer an economic signal: absence of persisted provider output means no
    # consume attempt. The server separately refuses release once all provider
    # requests completed.
    action = "consume" if ready else "release"
    key = _idempotency_key(ctx["job_id"], ctx["reservation_id"], action)
    result = provider.settle_reservation(ctx, action=action, idempotency_key=key)
    remote_state = str((result or {}).get("status") or (result or {}).get("state") or "")
    expected_state = STATE_CONSUMED if action == "consume" else STATE_RELEASED
    if remote_state != expected_state:
        raise RuntimeError("wallet_settlement_state_mismatch")
    ctx["state"] = expected_state
    ctx["idempotency_key"] = key
    _atomic_write(_state_path(output_dir), ctx)
    return {"state": expected_state, "net_yk": int(action == "consume")}
