"""Read-only clean-machine preflight for performance benchmarks."""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass
from typing import Any, Callable


HEAVY_FOREGROUND_PROCESS_NAMES = {
    "steam.exe",
    "epicgameslauncher.exe",
    "riotclientservices.exe",
    "battle.net.exe",
    "ea app.exe",
    "eadesktop.exe",
    "robloxplayerbeta.exe",
    "fortniteclient-win64-shipping.exe",
    "leagueclient.exe",
    "valorant-win64-shipping.exe",
    "cs2.exe",
    "gta5.exe",
    "eldenring.exe",
    "minecraft.exe",
    "javaw.exe",
}


@dataclass(frozen=True)
class SystemLoadSnapshot:
    cpu_percent: float
    ram_percent: float
    available_ram_mb: int
    heavy_process_names: tuple[str, ...]


def _process_names() -> list[str]:
    try:
        import psutil
    except ImportError:
        return []
    names: set[str] = set()
    for proc in psutil.process_iter(["name"]):
        try:
            name = str((proc.info or {}).get("name") or "").strip()
        except (OSError, psutil.Error):
            continue
        if name:
            names.add(name)
    return sorted(names, key=str.casefold)


def capture_system_load_snapshot() -> SystemLoadSnapshot:
    try:
        import psutil
    except ImportError:
        return SystemLoadSnapshot(
            cpu_percent=0.0,
            ram_percent=0.0,
            available_ram_mb=0,
            heavy_process_names=(),
        )
    cpu = float(psutil.cpu_percent(interval=0.1))
    memory = psutil.virtual_memory()
    names = _process_names()
    heavy = tuple(
        name
        for name in names
        if name.casefold() in HEAVY_FOREGROUND_PROCESS_NAMES
    )
    return SystemLoadSnapshot(
        cpu_percent=round(cpu, 2),
        ram_percent=round(float(memory.percent), 2),
        available_ram_mb=int(memory.available / (1024 * 1024)),
        heavy_process_names=heavy,
    )


def clean_benchmark_preflight(
    *,
    duration_seconds: float = 30.0,
    sample_interval_seconds: float = 5.0,
    sampler: Callable[[], SystemLoadSnapshot] = capture_system_load_snapshot,
) -> dict[str, Any]:
    """Observe a short stability window and report whether a benchmark may start."""

    duration = max(0.0, float(duration_seconds))
    interval = max(0.1, float(sample_interval_seconds))
    deadline = time.monotonic() + duration
    snapshots: list[SystemLoadSnapshot] = []
    while True:
        snapshots.append(sampler())
        if time.monotonic() >= deadline or duration == 0.0:
            break
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))

    cpu_values = [snap.cpu_percent for snap in snapshots]
    ram_values = [snap.ram_percent for snap in snapshots]
    heavy = sorted({
        name
        for snap in snapshots
        for name in snap.heavy_process_names
    }, key=str.casefold)
    median_cpu = statistics.median(cpu_values) if cpu_values else 0.0
    max_cpu = max(cpu_values) if cpu_values else 0.0
    median_ram = statistics.median(ram_values) if ram_values else 0.0
    min_available_ram = min(
        (snap.available_ram_mb for snap in snapshots),
        default=0,
    )
    warnings = []
    if heavy:
        warnings.append("heavy_foreground_workload_detected")
    if median_cpu >= 75.0 or max_cpu >= 90.0:
        warnings.append("cpu_load_high")
    if median_ram >= 90.0 or min_available_ram and min_available_ram < 2048:
        warnings.append("ram_pressure_high")

    return {
        "status": "fail" if warnings else "pass",
        "reason_code": (
            "BENCHMARK_ENVIRONMENT_NOT_CLEAN" if warnings else "benchmark_environment_clean"
        ),
        "sample_count": len(snapshots),
        "duration_seconds": duration,
        "median_cpu_percent": round(float(median_cpu), 2),
        "max_cpu_percent": round(float(max_cpu), 2),
        "median_ram_percent": round(float(median_ram), 2),
        "min_available_ram_mb": int(min_available_ram),
        "heavy_process_names": heavy,
        "warnings": warnings,
    }
