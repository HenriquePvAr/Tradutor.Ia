"""Bounded, owner-scoped progress records for in-flight source analysis."""
from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

_LOCK = threading.Lock()
_MAX_TRACES = 256
_RETENTION_SECONDS = 15 * 60
_TRACES: "OrderedDict[tuple[str, str], dict[str, Any]]" = OrderedDict()


@dataclass(frozen=True, slots=True)
class SourceAnalysisTask:
    """In-process callback identity bound to one owner and trace generation."""

    owner_id: str
    trace_id: str
    task_id: str


class DuplicateSourceAnalysisTrace(RuntimeError):
    """This owner's trace ID is already retained."""


class SourceAnalysisRegistryFull(RuntimeError):
    """The bounded registry has no completed entry safe to evict."""


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


def _prune_locked(now: float) -> None:
    expired = [
        key for key, entry in _TRACES.items()
        if entry["done"] and now - float(entry["finished_at"] or entry["updated_at"])
        >= _RETENTION_SECONDS
    ]
    for key in expired:
        _TRACES.pop(key, None)


def _evict_completed_locked() -> bool:
    for key, entry in _TRACES.items():
        if entry["done"]:
            del _TRACES[key]
            return True
    return False


def start(owner_id: str, trace_id: str) -> SourceAnalysisTask:
    """Reserve an owner/trace pair and return its immutable callback identity."""
    owner = str(owner_id or "").strip()
    trace = str(trace_id or "").strip()
    if not owner or not trace or len(owner) > 256 or len(trace) > 80:
        raise ValueError("invalid_source_analysis_identity")
    now = time.time()
    key = (owner, trace)
    task = SourceAnalysisTask(owner, trace, uuid.uuid4().hex)
    with _LOCK:
        _prune_locked(now)
        if key in _TRACES:
            raise DuplicateSourceAnalysisTrace("source_analysis_trace_conflict")
        while len(_TRACES) >= _MAX_TRACES:
            if not _evict_completed_locked():
                raise SourceAnalysisRegistryFull("source_analysis_registry_full")
        _TRACES[key] = {
            "task_id": task.task_id,
            "stage": "validating",
            "message": "Validando o endereço…",
            "candidates_found": None,
            "started_at": now,
            "updated_at": now,
            "finished_at": None,
            "done": False,
            "cancelled": False,
        }
        _TRACES.move_to_end(key)
    return task


def _entry_for_task_locked(task: SourceAnalysisTask) -> dict[str, Any] | None:
    if not isinstance(task, SourceAnalysisTask):
        return None
    entry = _TRACES.get((task.owner_id, task.trace_id))
    if entry is None or entry.get("task_id") != task.task_id:
        return None
    return entry


def request_cancel(owner_id: str, trace_id: str) -> bool:
    """Flag only this owner's active task; unknown IDs create no state."""
    key = (str(owner_id or "").strip(), str(trace_id or "").strip())
    now = time.time()
    with _LOCK:
        _prune_locked(now)
        entry = _TRACES.get(key)
        if entry is None or entry["done"]:
            return False
        entry["cancelled"] = True
        entry["updated_at"] = now
        _TRACES.move_to_end(key)
        return True


def is_cancelled(task: SourceAnalysisTask) -> bool:
    with _LOCK:
        entry = _entry_for_task_locked(task)
        return bool(entry and not entry["done"] and entry["cancelled"])


def record(task: SourceAnalysisTask, event: str, **fields: Any) -> bool:
    """Apply a callback only while its server-owned generation remains current."""
    now = time.time()
    with _LOCK:
        entry = _entry_for_task_locked(task)
        if entry is None or entry["done"]:
            return False
        mapped = _STAGE_BY_EVENT.get(str(event or ""))
        if mapped:
            entry["stage"], entry["message"] = mapped
        count = _coerce_count(fields)
        if count is not None:
            entry["candidates_found"] = count
            if entry["stage"] in ("analyzing", "opening", "located"):
                entry["message"] = f"Localizando páginas — {count} encontrada(s)"
        entry["updated_at"] = now
        _TRACES.move_to_end((task.owner_id, task.trace_id))
        return True


def finish(task: SourceAnalysisTask) -> bool:
    """Finish only the generation that owns this callback."""
    now = time.time()
    with _LOCK:
        entry = _entry_for_task_locked(task)
        if entry is None or entry["done"]:
            return False
        entry["done"] = True
        entry["cancelled"] = False
        entry["finished_at"] = now
        entry["updated_at"] = now
        _TRACES.move_to_end((task.owner_id, task.trace_id))
        return True


def snapshot(owner_id: str, trace_id: str) -> dict[str, Any] | None:
    """Return progress only for the authenticated owner/trace pair."""
    now = time.time()
    key = (str(owner_id or "").strip(), str(trace_id or "").strip())
    with _LOCK:
        _prune_locked(now)
        entry = _TRACES.get(key)
        if entry is None:
            return None
        return {
            "stage": entry["stage"],
            "message": entry["message"],
            "candidates_found": entry["candidates_found"],
            "elapsed_ms": int(max(0.0, now - entry["started_at"]) * 1000),
            "done": bool(entry["done"]),
        }
