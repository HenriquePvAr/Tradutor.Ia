"""Memory-aware OCR worker sizing and sanitized telemetry.

The OCR engines used by this project are native, model-backed processes.  A
worker count that is harmless for a lightweight OCR backend can therefore
double the resident set of a chapter runner.  This module keeps the policy
pure and injectable so tests can exercise it without depending on the host's
actual RAM.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Callable


@dataclass(frozen=True)
class MemorySnapshot:
    available_memory_mb: float
    total_memory_mb: float
    process_rss_mb: float = 0.0


@dataclass(frozen=True)
class OcrMemoryDecision:
    workers: int
    requested_workers: int
    memory_mode: str
    memory_pressure: str
    reason: str
    available_memory_mb: float
    process_rss_mb: float
    reserve_mb: float
    estimated_worker_peak_mb: float


def snapshot(provider: Callable[[], MemorySnapshot | None] | None = None) -> MemorySnapshot | None:
    if provider is not None:
        return provider()
    try:
        import psutil  # type: ignore

        virtual = psutil.virtual_memory()
        rss = psutil.Process(os.getpid()).memory_info().rss
        return MemorySnapshot(
            available_memory_mb=virtual.available / (1024 * 1024),
            total_memory_mb=virtual.total / (1024 * 1024),
            process_rss_mb=rss / (1024 * 1024),
        )
    except Exception:
        return None


def choose_workers(
    requested_workers: int,
    *,
    memory: MemorySnapshot | None,
    estimated_worker_peak_mb: float = 1800.0,
    reserve_mb: float = 4096.0,
    max_memory_mb: float = 0.0,
    engine_heavy: bool = True,
    largest_image_pixels: int = 0,
) -> OcrMemoryDecision:
    requested = max(1, int(requested_workers or 1))
    peak = max(256.0, float(estimated_worker_peak_mb or 1800.0))
    reserve = max(256.0, float(reserve_mb or 0.0))
    available = float(memory.available_memory_mb) if memory else 0.0
    process_rss = float(memory.process_rss_mb) if memory else 0.0
    total = float(memory.total_memory_mb) if memory else 0.0
    if max_memory_mb > 0:
        available = min(available, max(0.0, float(max_memory_mb) - process_rss))
    pressure = "unknown"
    if memory and total > 0:
        used_percent = max(0.0, min(100.0, (total - available) * 100.0 / total))
        pressure = "critical" if used_percent >= 90 else "high" if used_percent >= 82 else "elevated" if used_percent >= 72 else "normal"
    capacity = max(1, int(max(0.0, available - reserve) // peak)) if memory else 1
    reasons: list[str] = []
    if memory is None:
        reasons.append("metrics_unavailable")
    if engine_heavy:
        # Paddle/RapidOCR model instances are intentionally never duplicated by
        # default. An explicit opt-in can still request more workers later.
        capacity = min(capacity, 1)
        reasons.append("heavy_engine")
    if largest_image_pixels >= 8_000_000:
        capacity = min(capacity, 1)
        reasons.append("large_image")
    workers = max(1, min(requested, capacity))
    if workers < requested:
        reasons.append("memory_budget")
    mode = "reduced" if workers == 1 and (requested > 1 or reasons) else "normal"
    return OcrMemoryDecision(
        workers=workers,
        requested_workers=requested,
        memory_mode=mode,
        memory_pressure=pressure,
        reason=";".join(dict.fromkeys(reasons)) or "within_budget",
        available_memory_mb=round(max(0.0, available), 3),
        process_rss_mb=round(max(0.0, process_rss), 3),
        reserve_mb=round(reserve, 3),
        estimated_worker_peak_mb=round(peak, 3),
    )
