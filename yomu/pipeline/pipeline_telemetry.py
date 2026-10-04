"""Opt-in, privacy-safe pipeline telemetry and local aggregation.

The normal pipeline remains unchanged when ``YOMU_PIPELINE_TELEMETRY`` is not
enabled.  Events contain identifiers, counters and timings only; source and
translation text are deliberately not accepted by the emitter.
"""
from __future__ import annotations

import json
import os
import statistics
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = "pipeline-telemetry-v1"
FLAG = "YOMU_PIPELINE_TELEMETRY"
MAX_EVENTS = 20_000
_SENSITIVE = ("text", "token", "secret", "cookie", "password", "authorization", "jwt", "payload")


def enabled() -> bool:
    return str(os.environ.get(FLAG, "")).strip().lower() in {"1", "true", "yes", "on"}


def _safe_fields(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in fields.items():
        lk = str(key).lower()
        if any(part in lk for part in _SENSITIVE):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[str(key)] = value
        elif isinstance(value, (list, tuple)) and all(isinstance(x, (str, int, float, bool)) for x in value):
            out[str(key)] = list(value)
    return out


class TelemetrySink:
    def __init__(self, path: str | Path, *, run_id: str | None = None, job_id: str | None = None):
        self.path = Path(path)
        self.run_id = run_id or uuid.uuid4().hex
        self.job_id = job_id or ""
        self._lock = threading.Lock()
        self._count = 0
        self._buffer: list[str] = []

    def emit(self, event_name: str, **fields: Any) -> None:
        if not enabled() or self._count >= MAX_EVENTS:
            return
        event = {
            "schema_version": SCHEMA_VERSION,
            "event_name": str(event_name),
            "timestamp": time.time(),
            "monotonic_ns": time.monotonic_ns(),
            "run_id": self.run_id,
            "job_id": self.job_id,
            **_safe_fields(fields),
        }
        with self._lock:
            if self._count >= MAX_EVENTS:
                return
            self._buffer.append(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
            self._count += 1
            if len(self._buffer) >= 128:
                self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._buffer:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(self._buffer) + "\n")
        self._buffer.clear()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    @contextmanager
    def span(self, stage: str, *, page_index: int | None = None, region_id: str | None = None, **fields: Any) -> Iterator[None]:
        started = time.perf_counter()
        self.emit("SPAN_START", stage=stage, page_index=page_index, region_id=region_id, **fields)
        status = "success"
        error_class = None
        try:
            yield
        except Exception as exc:
            status = "error"
            error_class = type(exc).__name__
            raise
        finally:
            self.emit("SPAN_END", stage=stage, page_index=page_index, region_id=region_id,
                      duration_ms=max(0.0, (time.perf_counter() - started) * 1000.0),
                      status=status, error_class=error_class, **fields)


def aggregate_events(path: str | Path) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    malformed = 0
    p = Path(path)
    if p.is_file():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                item = json.loads(line)
                if isinstance(item, dict) and item.get("schema_version") == SCHEMA_VERSION:
                    events.append(item)
                else:
                    malformed += 1
            except (TypeError, ValueError, json.JSONDecodeError):
                malformed += 1
    durations: dict[str, list[float]] = {}
    starts: dict[tuple[str, int | None, str | None], int] = {}
    unmatched_end = 0
    for event in events:
        name = event.get("event_name")
        key = (str(event.get("stage") or ""), event.get("page_index"), event.get("region_id"))
        if name == "SPAN_START":
            starts[key] = starts.get(key, 0) + 1
        elif name in {"SPAN_END", "STAGE_MEASURED"}:
            if not starts.get(key):
                if name == "SPAN_END":
                    unmatched_end += 1
            else:
                starts[key] -= 1
            try:
                durations.setdefault(key[0], []).append(max(0.0, float(event.get("duration_ms", 0))))
            except (TypeError, ValueError):
                pass
    stage_summary = {}
    for stage, values in durations.items():
        stage_summary[stage] = {
            "count": len(values), "min_ms": min(values), "max_ms": max(values),
            "mean_ms": statistics.fmean(values), "p50_ms": statistics.median(values),
            "p95_ms": sorted(values)[max(0, int(len(values) * 0.95) - 1)],
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "event_count": len(events),
        "malformed_events": malformed,
        "unmatched_start_events": sum(starts.values()),
        "unmatched_end_events": unmatched_end,
        "stage_summary": stage_summary,
        "wall_clock_distinct_from_cumulative_work": True,
    }


def summarize_timing_report(report: dict[str, Any]) -> dict[str, Any]:
    stage_seconds = {str(k): float(v) for k, v in (report.get("stage_seconds") or {}).items()
                     if isinstance(v, (int, float))}
    total = float(report.get("total_seconds") or 0.0)
    return {
        "schema_version": SCHEMA_VERSION,
        "wall_clock_ms": round(total * 1000, 3),
        "cumulative_work_ms": round(sum(stage_seconds.values()) * 1000, 3),
        "stage_total_ms": {k: round(v * 1000, 3) for k, v in stage_seconds.items()},
        "stage_percent_of_wall_clock": {k: round((v / total) * 100, 3) if total > 0 else 0.0 for k, v in stage_seconds.items()},
        "page_count": len(report.get("page_timings") or {}),
        "retry_counts": {k: int(v) for k, v in (report.get("counters") or {}).items() if "retry" in str(k).lower()},
        "review_counts": {"review_regions": int((report.get("quality_validation") or {}).get("review_regions", 0) or 0)},
    }
