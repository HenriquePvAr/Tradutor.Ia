"""Resolve lazy reader slots incrementally, preserving DOM order.

A lazy reader hands out placeholder elements first: the slot exists and holds its position,
and the real page arrives only after the viewport approaches it. Reading once therefore sees
a fraction of the chapter, and treating the rest as rejected silently shrinks it.

This resolver visits pending regions, re-reads the same DOM indices and updates only those
slots, until nothing is pending or the budget runs out. Every browser operation is injected,
so the algorithm is testable without a browser and without any network.

It never fetches image bytes: it observes element state only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from chapter_source import (
    SLOT_PENDING, SLOT_REJECTED, SLOT_RESOLVED, classify_reader_slots, pending_indices,
    slot_counts,
)

# Diagnostic warnings this resolver may raise. They describe observation, never downloads.
READER_DOM_CHANGED = "reader_dom_changed"
LAZY_RESOLUTION_TIMEOUT = "lazy_resolution_timeout"
LAZY_RESOLUTION_ROUNDS = "lazy_resolution_max_rounds"

COUNTER_STAGE = "source_lazy_resolution"


@dataclass
class ResolverLimits:
    max_rounds: int = 120
    stable_rounds: int = 3
    timeout_seconds: float = 180.0
    settle_seconds: float = 0.6


@dataclass
class ResolutionResult:
    slots: list[dict[str, Any]]
    rounds: int = 0
    reached_reader_end: bool = False
    stabilized: bool = False
    timed_out: bool = False
    cancelled: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return slot_counts(self.slots)

    @property
    def indices_pending(self) -> list[int]:
        return pending_indices(self.slots)

    @property
    def resolved_candidates(self) -> list[dict[str, Any]]:
        """Resolved pages in reader order — never in the order they happened to load."""
        return [slot["candidate"] for slot in self.slots if slot["state"] == SLOT_RESOLVED]

    def public(self) -> dict[str, Any]:
        counts = self.counts
        return {
            "counter_stage": COUNTER_STAGE,
            "slots_total": counts["total"],
            "slots_resolved": counts["resolved"],
            "slots_pending": counts["pending"],
            "slots_rejected": counts["rejected"],
            "indices_pending": self.indices_pending[:50],
            "rounds": self.rounds,
            "reached_reader_end": self.reached_reader_end,
            "stabilized": self.stabilized,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "warnings": list(self.warnings),
        }


class Cancelled(RuntimeError):
    """The caller asked to stop; no selection is produced."""


def resolve_lazy_reader_slots(
    *,
    adapter: Any,
    read_slots: Callable[[], Iterable[dict[str, Any]]],
    scroll_to: Callable[[int], Any],
    reader_bounds: Callable[[], tuple[int, int]],
    limits: ResolverLimits | None = None,
    cancel_check: Callable[[], bool] | None = None,
    on_progress: Callable[[dict[str, Any]], Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Any] = time.sleep,
) -> ResolutionResult:
    """Visit pending regions until the reader resolves, or the budget ends.

    ``read_slots`` returns the reader's current candidates in DOM order; ``scroll_to`` moves
    the viewport to a vertical position; ``reader_bounds`` gives the reader's top and bottom.
    None of them fetch anything.
    """
    limits = limits or ResolverLimits()
    started = clock()
    warnings: list[str] = []

    def check_cancel() -> None:
        if cancel_check and cancel_check():
            raise Cancelled()

    def emit(slots: list[dict[str, Any]], message: str) -> None:
        if not on_progress:
            return
        counts = slot_counts(slots)
        on_progress({
            "counter_stage": COUNTER_STAGE,
            "current": counts["resolved"],
            "total": counts["total"],
            "pending": counts["pending"],
            "message": message,
        })

    rounds = 0
    stable = 0
    reached_end = False
    timed_out = False
    cancelled = False
    slots: list[dict[str, Any]] = []
    baseline_total = 0

    try:
        # Inside the try: cancelling before the first read is a cancelled run, not an
        # exception the caller has to catch.
        check_cancel()
        slots = classify_reader_slots(read_slots(), adapter=adapter)
        baseline_total = len(slots)
        emit(slots, "Páginas do leitor identificadas")

        while rounds < limits.max_rounds:
            counts = slot_counts(slots)
            if counts["pending"] == 0:
                # Everything settled; the reader end is implied by having no slot left.
                reached_end = True
                break
            if clock() - started > limits.timeout_seconds:
                timed_out = True
                warnings.append(LAZY_RESOLUTION_TIMEOUT)
                break

            check_cancel()
            top, bottom = reader_bounds()
            # Aim at the first pending slot's own position when known, else walk the reader.
            target = _next_target(slots, top, bottom, rounds, limits)
            scroll_to(target)
            reached_end = reached_end or target >= bottom
            check_cancel()
            sleep(limits.settle_seconds)
            check_cancel()

            rounds += 1
            before = counts["resolved"]
            slots, changed = _merge_reread(slots, read_slots(), adapter=adapter)
            if changed and READER_DOM_CHANGED not in warnings:
                # An unexpected insertion/removal invalidates index identity; say so rather
                # than silently renumbering the chapter.
                warnings.append(READER_DOM_CHANGED)
            after = slot_counts(slots)["resolved"]
            emit(slots, f"Carregando páginas do leitor: {after}/{len(slots)}")

            if after > before:
                stable = 0
            elif reached_end:
                stable += 1
                if stable >= limits.stable_rounds:
                    break
        else:
            warnings.append(LAZY_RESOLUTION_ROUNDS)
    except Cancelled:
        cancelled = True

    if len(slots) != baseline_total and READER_DOM_CHANGED not in warnings:
        warnings.append(READER_DOM_CHANGED)

    result = ResolutionResult(
        slots=slots, rounds=rounds, reached_reader_end=reached_end,
        stabilized=slot_counts(slots)["pending"] == 0 or stable >= limits.stable_rounds,
        timed_out=timed_out, cancelled=cancelled, warnings=warnings)
    emit(slots, "Cobertura do leitor validada" if not cancelled else "Cancelado")
    return result


def _next_target(slots: list[dict[str, Any]], top: int, bottom: int, rounds: int,
                 limits: ResolverLimits) -> int:
    """Vertical position to visit next: the first pending slot, else a step down."""
    for slot in slots:
        if slot["state"] != SLOT_PENDING:
            continue
        y = slot["candidate"].get("y")
        if isinstance(y, (int, float)) and y > 0:
            return int(y)
        break
    span = max(1, bottom - top)
    step = max(1, span // max(1, limits.max_rounds // 4))
    return min(bottom, top + step * (rounds + 1))


def _merge_reread(previous: list[dict[str, Any]], observed: Iterable[dict[str, Any]], *,
                  adapter: Any) -> tuple[list[dict[str, Any]], bool]:
    """Update slots by DOM index, keeping resolutions and definitive rejections.

    A slot that already resolved never regresses because a later read saw it differently,
    and the output order always follows the index rather than the resolution order.
    """
    fresh = {slot["index"]: slot for slot in classify_reader_slots(observed, adapter=adapter)}
    merged: list[dict[str, Any]] = []
    for slot in previous:
        candidate_new = fresh.get(slot["index"])
        if candidate_new is None or slot["state"] == SLOT_RESOLVED:
            merged.append(slot)
            continue
        if slot["state"] == SLOT_REJECTED and candidate_new["state"] != SLOT_RESOLVED:
            merged.append(slot)
            continue
        merged.append(candidate_new)
    known = {slot["index"] for slot in previous}
    extra = [slot for index, slot in fresh.items() if index not in known]
    merged.extend(extra)
    merged.sort(key=lambda slot: slot["index"])
    return merged, bool(extra) or len(fresh) != len(previous)
