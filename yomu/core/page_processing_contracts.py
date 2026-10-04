"""Small page-local contracts used when isolating pipeline stages.

These contracts intentionally contain only serialisable/page-owned values.  The
current benchmark pipeline still runs serially; callers can adopt these result
objects incrementally without allowing a page worker to mutate job aggregates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class PageProgressEvent:
    """Progress produced by one page, before job-level aggregation."""

    page_index: int
    stage: str
    completed_units: int = 0
    total_units: int = 1
    message: str = ""
    code: str = ""
    local_fraction: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def fraction(self) -> float:
        if self.local_fraction is not None:
            value = float(self.local_fraction)
        else:
            total = max(1, int(self.total_units))
            value = float(self.completed_units) / total
        return max(0.0, min(1.0, value))


@dataclass
class PageProgressAggregator:
    """Main-thread/job-owned progress reducer.

    Pages may complete in any order.  The reducer derives progress from
    completion fractions, never from page indexes, and clamps the emitted
    value so an out-of-order completion cannot regress the UI/job writer.
    """

    total_pages: int
    emit: Callable[[dict[str, Any]], Any] | None = None
    _page_fractions: dict[int, float] = field(default_factory=dict, init=False)
    _completed_pages: set[int] = field(default_factory=set, init=False)
    _last_progress: float = field(default=0.0, init=False)
    _terminal: bool = field(default=False, init=False)
    _global_writes: int = field(default=0, init=False)

    @property
    def last_progress(self) -> float:
        return self._last_progress

    @property
    def global_writes(self) -> int:
        return self._global_writes

    def consume(self, events: list[PageProgressEvent] | tuple[PageProgressEvent, ...], *,
                cancelled: bool = False, terminal: bool = False) -> list[dict[str, Any]]:
        """Reduce page events and emit only after the page has closed."""
        if self._terminal:
            return []
        if cancelled:
            return []
        for event in events:
            index = int(event.page_index)
            self._page_fractions[index] = max(
                self._page_fractions.get(index, 0.0), event.fraction()
            )
            if self._page_fractions[index] >= 1.0:
                self._completed_pages.add(index)
        total = max(1, int(self.total_pages))
        aggregate = sum(self._page_fractions.values()) / total
        progress = max(self._last_progress, min(1.0, aggregate))
        self._last_progress = progress
        payload = {
            "progress": progress,
            "completed_pages": len(self._completed_pages),
            "total_pages": max(0, int(self.total_pages)),
            "page_indexes": sorted(self._completed_pages),
            "events": [event for event in events],
        }
        if events and self.emit is not None:
            self.emit(payload)
            self._global_writes += 1
        if terminal:
            self._terminal = True
        return [payload] if events else []


@dataclass(frozen=True)
class PageProcessingContext:
    page_index: int
    source_path: str
    ocr_result: Any = None
    config_snapshot: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PreTranslationPageResult:
    page_index: int
    groups: tuple[Any, ...] = ()
    translation_items: tuple[Any, ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)
    ocr_line_provenance: tuple[Mapping[str, Any], ...] = ()
    diagnostics: tuple[Mapping[str, Any], ...] = ()
    counters: Mapping[str, int] = field(default_factory=dict)
    timings: Mapping[str, float] = field(default_factory=dict)
    progress_events: tuple[PageProgressEvent, ...] = ()
    page_state: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    analyzable: bool = False
    page_recorder: Any = field(default=None, repr=False, compare=False)
    page_profiler: Any = field(default=None, repr=False, compare=False)
    error: str | None = None


@dataclass
class PreTranslationPageAccumulator:
    """Mutable scratch state for one page before main-thread aggregation."""

    page_index: int
    groups: list[Any] = field(default_factory=list)
    translation_items: list[Any] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    ocr_line_provenance: list[Mapping[str, Any]] = field(default_factory=list)
    diagnostics: list[Mapping[str, Any]] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    progress_events: list[PageProgressEvent] = field(default_factory=list)
    page_state: dict[str, Any] = field(default_factory=dict)
    analyzable: bool = False
    page_recorder: Any = field(default=None, repr=False)
    page_profiler: Any = field(default=None, repr=False)
    error: str | None = None

    def to_result(self) -> PreTranslationPageResult:
        """Freeze page-owned collections before they leave the page boundary."""
        return PreTranslationPageResult(
            page_index=int(self.page_index),
            groups=tuple(self.groups),
            translation_items=tuple(self.translation_items),
            provenance=dict(self.provenance),
            ocr_line_provenance=tuple(dict(item) for item in self.ocr_line_provenance),
            diagnostics=tuple(dict(item) for item in self.diagnostics),
            counters=dict(self.counters),
            timings=dict(self.timings),
            progress_events=tuple(self.progress_events),
            page_state=dict(self.page_state),
            analyzable=bool(self.analyzable),
            page_recorder=self.page_recorder,
            page_profiler=self.page_profiler,
            error=self.error,
        )


@dataclass(frozen=True)
class PostTranslationPageResult:
    page_index: int
    output_path: str = ""
    quality: Mapping[str, Any] = field(default_factory=dict)
    diagnostics: tuple[Mapping[str, Any], ...] = ()
    counters: Mapping[str, int] = field(default_factory=dict)
    timings: Mapping[str, float] = field(default_factory=dict)
    page_state: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    page_recorder: Any = field(default=None, repr=False, compare=False)
    error: str | None = None


def merge_page_counters(target: dict[str, int], result: Any) -> None:
    """Merge page-owned integer counters on the aggregation thread."""
    for key, value in dict(getattr(result, "counters", {}) or {}).items():
        target[str(key)] = int(target.get(str(key), 0)) + int(value)


def merge_page_diagnostics(target: list[Mapping[str, Any]], result: Any) -> None:
    """Append diagnostics in caller-supplied page order, never in workers."""
    target.extend(tuple(getattr(result, "diagnostics", ()) or ()))
