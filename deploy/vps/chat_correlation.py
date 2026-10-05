"""Pure DOM-delta correlation helpers; old blobs are never accepted."""
from __future__ import annotations


def fresh_sources(baseline: tuple[str, ...] | list[str], current: list[str] | tuple[str, ...]) -> list[str]:
    old = set(baseline)
    return [source for source in current if source and source not in old]


def require_fresh_source(baseline: tuple[str, ...] | list[str], current: list[str] | tuple[str, ...]) -> str:
    fresh = fresh_sources(baseline, current)
    if not fresh:
        raise RuntimeError("stale_output_rejected")
    return fresh[-1]
