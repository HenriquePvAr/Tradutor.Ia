"""In-process progress registry for source analysis, so the UI can show live activity
while ``/api/ui/source/analyze`` is still running instead of a frozen "Analisando...".

The analyze route already emits coarse diagnostic events (resolver start/end, fallback
decision, candidate counts). We map those to a small user-facing stage + message keyed by
``trace_id`` and expose the latest snapshot through a lightweight poll endpoint. This is
purely observational: it never changes discovery/download behavior.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any

_LOCK = threading.Lock()
_MAX_TRACES = 256  # bounded so long-lived processes never leak trace records
_TRACES: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

# Event -> (stage, default message). Events not listed keep the current stage but still
# refresh the heartbeat, so any activity keeps the UI from looking stuck.
_STAGE_BY_EVENT = {
    "SOURCE_ACTION_BEGIN": ("validating", "Validando o endereço…"),
    "SOURCE_ANALYSIS_INPUT": ("validating", "Validando o endereço…"),
    "SOURCE_FALLBACK_DECISION": ("analyzing", "Analisando a estrutura da fonte…"),
    "DYNAMIC_RESOLVER_START": ("opening", "Abrindo o capítulo no navegador…"),
    "DYNAMIC_RESOLVER_END": ("located", "Páginas localizadas"),
}


def _coerce_count(fields: dict[str, Any]) -> int | None:
    for key in ("candidates_found", "candidate_count", "accepted", "total", "current", "slots_total"):
        value = fields.get(key)
        if value is None:
            continue
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count >= 0:
            return count
    return None


_CANCELLED: "OrderedDict[str, float]" = OrderedDict()


def start(trace_id: str) -> None:
    trace_id = str(trace_id or "")
    if not trace_id:
        return
    now = time.time()
    with _LOCK:
        _TRACES[trace_id] = {
            "stage": "validating", "message": "Validando o endereço…",
            "candidates_found": None, "started_at": now, "updated_at": now, "done": False,
        }
        _TRACES.move_to_end(trace_id)
        _CANCELLED.pop(trace_id, None)  # a fresh run clears any stale cancel flag
        while len(_TRACES) > _MAX_TRACES:
            _TRACES.popitem(last=False)


def request_cancel(trace_id: str) -> bool:
    """Flag an in-flight analysis for cooperative cancellation. Returns whether it was known."""
    trace_id = str(trace_id or "")
    if not trace_id:
        return False
    with _LOCK:
        _CANCELLED[trace_id] = time.time()
        _CANCELLED.move_to_end(trace_id)
        while len(_CANCELLED) > _MAX_TRACES:
            _CANCELLED.popitem(last=False)
        return trace_id in _TRACES


def is_cancelled(trace_id: str) -> bool:
    trace_id = str(trace_id or "")
    with _LOCK:
        return trace_id in _CANCELLED


def record(trace_id: str, event: str, **fields: Any) -> None:
    trace_id = str(trace_id or "")
    if not trace_id:
        return
    now = time.time()
    with _LOCK:
        entry = _TRACES.get(trace_id)
        if entry is None:
            entry = {"stage": "validating", "message": "Validando o endereço…",
                     "candidates_found": None, "started_at": now, "updated_at": now, "done": False}
            _TRACES[trace_id] = entry
        mapped = _STAGE_BY_EVENT.get(str(event or ""))
        if mapped:
            entry["stage"], entry["message"] = mapped
        count = _coerce_count(fields)
        if count is not None:
            entry["candidates_found"] = count
            if entry["stage"] in ("analyzing", "opening", "located"):
                entry["message"] = f"Localizando páginas — {count} encontrada(s)"
        entry["updated_at"] = now
        _TRACES.move_to_end(trace_id)


def finish(trace_id: str) -> None:
    trace_id = str(trace_id or "")
    with _LOCK:
        entry = _TRACES.get(trace_id)
        if entry is not None:
            entry["done"] = True
            entry["updated_at"] = time.time()


def snapshot(trace_id: str) -> dict[str, Any] | None:
    trace_id = str(trace_id or "")
    with _LOCK:
        entry = _TRACES.get(trace_id)
        if entry is None:
            return None
        return {
            "stage": entry["stage"],
            "message": entry["message"],
            "candidates_found": entry["candidates_found"],
            "elapsed_ms": int(max(0.0, time.time() - entry["started_at"]) * 1000),
            "done": bool(entry["done"]),
        }
