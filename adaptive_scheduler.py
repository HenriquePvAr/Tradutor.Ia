"""Conservative adaptive worker sizing helpers.

This module is intentionally independent from the real machine state in its
core calculations so unit tests can mock metrics instead of depending on the
developer's RAM/CPU at test time.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any


@dataclass(frozen=True)
class SchedulerConfig:
    min_workers: int = 1
    max_workers: int = 2
    memory_safety_margin_percent: float = 20.0
    min_system_reserve_gb: float = 4.0
    pipeline_reserve_gb: float = 1.0
    worker_estimated_peak_mb: float = 1800.0
    memory_pressure_elevated_percent: float = 72.0
    memory_pressure_high_percent: float = 82.0
    memory_pressure_critical_percent: float = 90.0
    scale_up_cooldown_seconds: float = 20.0
    scale_down_cooldown_seconds: float = 8.0
    cpu_high_percent: float = 92.0


@dataclass(frozen=True)
class ResourceSnapshot:
    available_memory_mb: float
    total_memory_mb: float
    ram_percent: float
    cpu_percent: float
    swap_used_mb: float = 0.0
    physical_cores: int = 1
    logical_cores: int = 1


@dataclass(frozen=True)
class SchedulerDecision:
    target_workers: int
    pressure: str
    reason: str
    safe_memory_budget_mb: float
    estimated_worker_peak_mb: float


class AdaptiveResourceScheduler:
    def __init__(self, config: SchedulerConfig, now_func=time.monotonic) -> None:
        self.config = config
        self.now_func = now_func
        self.target_workers = max(1, int(config.min_workers))
        self.estimated_worker_peak_mb = max(128.0, float(config.worker_estimated_peak_mb))
        self._last_scale_up = -1e9
        self._last_scale_down = -1e9

    def update_worker_observation(self, peak_rss_mb: float) -> None:
        peak = float(peak_rss_mb or 0.0)
        if peak <= 0:
            return
        # Slow moving average with upward bias for safety.
        if peak > self.estimated_worker_peak_mb:
            self.estimated_worker_peak_mb = peak * 0.85 + self.estimated_worker_peak_mb * 0.15
        else:
            self.estimated_worker_peak_mb = peak * 0.25 + self.estimated_worker_peak_mb * 0.75

    def decide(
        self,
        snapshot: ResourceSnapshot,
        *,
        current_workers: int,
        pending_jobs: int,
    ) -> SchedulerDecision:
        current_workers = max(1, int(current_workers or 1))
        pending_jobs = max(0, int(pending_jobs or 0))
        pressure = classify_memory_pressure(
            snapshot.ram_percent,
            self.config.memory_pressure_elevated_percent,
            self.config.memory_pressure_high_percent,
            self.config.memory_pressure_critical_percent,
        )
        safe_budget = safe_memory_budget_mb(
            snapshot.available_memory_mb,
            snapshot.total_memory_mb,
            self.config.memory_safety_margin_percent,
            self.config.min_system_reserve_gb,
            self.config.pipeline_reserve_gb,
        )
        by_memory = max(1, int(safe_budget // max(1.0, self.estimated_worker_peak_mb)))
        by_cpu = max(1, min(int(snapshot.physical_cores or 1), int(self.config.max_workers)))
        capacity = max(
            int(self.config.min_workers),
            min(int(self.config.max_workers), by_memory, by_cpu, max(1, pending_jobs)),
        )
        now = self.now_func()
        target = min(current_workers, capacity)
        reason = "hold"

        if pressure == "CRITICAL":
            target = int(self.config.min_workers)
            reason = "critical_memory_pressure"
            self._last_scale_down = now
        elif pressure == "HIGH" or snapshot.cpu_percent >= self.config.cpu_high_percent:
            target = max(int(self.config.min_workers), min(current_workers, capacity))
            if target < current_workers and now - self._last_scale_down >= self.config.scale_down_cooldown_seconds:
                reason = "scale_down_high_pressure"
                self._last_scale_down = now
            else:
                target = current_workers
                reason = "hold_high_pressure"
        elif pressure == "ELEVATED":
            target = min(current_workers, capacity)
            reason = "hold_elevated_pressure"
        elif capacity > current_workers and pending_jobs > current_workers:
            if now - self._last_scale_up >= self.config.scale_up_cooldown_seconds:
                target = current_workers + 1
                reason = "scale_up_gradual"
                self._last_scale_up = now
            else:
                target = current_workers
                reason = "scale_up_cooldown"
        elif capacity < current_workers:
            if now - self._last_scale_down >= self.config.scale_down_cooldown_seconds:
                target = max(int(self.config.min_workers), capacity)
                reason = "scale_down_capacity"
                self._last_scale_down = now
            else:
                target = current_workers
                reason = "scale_down_cooldown"
        else:
            target = min(current_workers, capacity)

        target = max(int(self.config.min_workers), min(int(self.config.max_workers), int(target)))
        self.target_workers = target
        return SchedulerDecision(
            target_workers=target,
            pressure=pressure,
            reason=reason,
            safe_memory_budget_mb=round(safe_budget, 3),
            estimated_worker_peak_mb=round(self.estimated_worker_peak_mb, 3),
        )


def classify_memory_pressure(
    ram_percent: float,
    elevated: float = 72.0,
    high: float = 82.0,
    critical: float = 90.0,
) -> str:
    value = float(ram_percent or 0.0)
    if value >= critical:
        return "CRITICAL"
    if value >= high:
        return "HIGH"
    if value >= elevated:
        return "ELEVATED"
    return "NORMAL"


def safe_memory_budget_mb(
    available_memory_mb: float,
    total_memory_mb: float,
    margin_percent: float,
    min_system_reserve_gb: float,
    pipeline_reserve_gb: float,
) -> float:
    available = max(0.0, float(available_memory_mb or 0.0))
    total = max(0.0, float(total_memory_mb or 0.0))
    reserve = max(
        float(min_system_reserve_gb or 0.0) * 1024.0,
        total * max(0.0, float(margin_percent or 0.0)) / 100.0,
    )
    reserve += max(0.0, float(pipeline_reserve_gb or 0.0) * 1024.0)
    return max(0.0, available - reserve)


def snapshot_from_psutil() -> ResourceSnapshot | None:
    try:
        import psutil  # type: ignore

        virtual = psutil.virtual_memory()
        swap = psutil.swap_memory()
        return ResourceSnapshot(
            available_memory_mb=virtual.available / (1024 * 1024),
            total_memory_mb=virtual.total / (1024 * 1024),
            ram_percent=float(virtual.percent),
            cpu_percent=float(psutil.cpu_percent(interval=None)),
            swap_used_mb=swap.used / (1024 * 1024),
            physical_cores=psutil.cpu_count(logical=False) or 1,
            logical_cores=psutil.cpu_count(logical=True) or 1,
        )
    except Exception:
        return None


def config_from_module(config: Any) -> SchedulerConfig:
    return SchedulerConfig(
        min_workers=max(1, int(getattr(config, "MIN_OCR_WORKERS", 1))),
        max_workers=max(1, int(getattr(config, "MAX_OCR_WORKERS", getattr(config, "OCR_WORKERS", 2)))),
        memory_safety_margin_percent=float(getattr(config, "MEMORY_SAFETY_MARGIN_PERCENT", 20.0)),
        min_system_reserve_gb=float(getattr(config, "MIN_SYSTEM_RESERVE_GB", 4.0)),
        pipeline_reserve_gb=float(getattr(config, "PIPELINE_MEMORY_RESERVE_GB", 1.0)),
        worker_estimated_peak_mb=float(getattr(config, "OCR_WORKER_INITIAL_PEAK_MB", 1800.0)),
        memory_pressure_elevated_percent=float(getattr(config, "MEMORY_PRESSURE_ELEVATED_PERCENT", 72.0)),
        memory_pressure_high_percent=float(getattr(config, "MEMORY_PRESSURE_HIGH_PERCENT", 82.0)),
        memory_pressure_critical_percent=float(getattr(config, "MEMORY_PRESSURE_CRITICAL_PERCENT", 90.0)),
        scale_up_cooldown_seconds=float(getattr(config, "WORKER_SCALE_UP_COOLDOWN_SECONDS", 20.0)),
        scale_down_cooldown_seconds=float(getattr(config, "WORKER_SCALE_DOWN_COOLDOWN_SECONDS", 8.0)),
        cpu_high_percent=float(getattr(config, "CPU_PRESSURE_HIGH_PERCENT", 92.0)),
    )
