"""Deterministic aggregation for phase/resource benchmark reports."""
from __future__ import annotations

import math
import statistics
from collections import defaultdict


def _stats(values):
    values = [float(value) for value in values if value is not None]
    if not values:
        return {"count": 0, "mean": None, "median": None, "stddev": None, "min": None, "max": None}
    return {"count": len(values), "mean": statistics.mean(values), "median": statistics.median(values),
            "stddev": statistics.stdev(values) if len(values) > 1 else 0.0, "min": min(values), "max": max(values)}


def summarize_runs(runs: list[dict]) -> dict:
    durations = [run["total_seconds"] for run in runs]
    phases = defaultdict(list)
    for run in runs:
        for key, value in run.get("phase_means_ms", {}).items():
            phases[key].append(value)
    return {"run_count": len(runs), "total_seconds": _stats(durations),
            "requests_per_minute": (len(runs) * 60.0 / statistics.mean(durations)) if durations else None,
            "phase_means_ms": {key: _stats(value) for key, value in phases.items()},
            "completed": sum(int(run.get("completed", 0)) for run in runs),
            "review": sum(int(run.get("review", 0)) for run in runs),
            "errors": sum(int(run.get("errors", 0)) for run in runs)}


def compare(c1: dict, c2: dict) -> dict:
    c1_mean = c1["total_seconds"]["mean"]
    c2_mean = c2["total_seconds"]["mean"]
    speedup = c1_mean / c2_mean if c1_mean and c2_mean else None
    c1_phases, c2_phases = c1.get("phase_means_ms", {}), c2.get("phase_means_ms", {})
    ratios = {}
    for key in sorted(set(c1_phases) & set(c2_phases)):
        left, right = c1_phases[key].get("mean"), c2_phases[key].get("mean")
        ratios[key] = right / left if left else None
    return {"speedup": speedup, "phase_contention_ratio_c2_over_c1": ratios,
            "classification": "C2_WIN" if speedup and speedup > 1.1 else "C2_SAFE_BUT_NOT_FASTER" if speedup else "INCONCLUSIVE"}
