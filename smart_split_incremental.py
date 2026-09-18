"""Incremental smart-split state machine.

The splitter deliberately keeps the legacy look-ahead rules.  A source image is
only transferred to the manifest after the prefix of the vertical buffer is
provably closed; the residual is emitted only by ``finish``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image

from page_manifest import FinalPageManifestItem, _stable_id
from pdf import (
    MAX_LOGICAL_PAGE_HEIGHT,
    _append_vertical,
    _expanded_semantic_split,
    _find_safe_horizontal_split,
    _local_protected_regions,
    protected_vertical_intervals,
)


@dataclass(frozen=True)
class SourceChunk:
    source_item_id: str
    top: int
    bottom: int


class SourceOrderBuffer:
    """Bounded logical-order gate for out-of-order downloader completions."""

    def __init__(self, first_index: int = 1, max_items: int = 64) -> None:
        self.next_index = first_index
        self.max_items = max_items
        self._pending: dict[int, tuple[str, object]] = {}
        self.max_pending = 0

    def add(self, logical_index: int, source_item_id: str, value: object) -> tuple[tuple[str, object], ...]:
        if logical_index < self.next_index or logical_index in self._pending:
            raise ValueError(f"duplicate or stale source index: {logical_index}")
        self._pending[logical_index] = (str(source_item_id), value)
        self.max_pending = max(self.max_pending, len(self._pending))
        if len(self._pending) > self.max_items:
            raise BufferError("source reorder buffer limit exceeded")
        ready = []
        while self.next_index in self._pending:
            ready.append(self._pending.pop(self.next_index))
            self.next_index += 1
        return tuple(ready)

    @property
    def pending(self) -> int:
        return len(self._pending)


class IncrementalSmartSplitter:
    """Stateful equivalent of ``prepare_smart_webtoon_pages``.

    ``push`` never flushes an unsafe prefix.  ``finish`` is the only operation
    that may finalize the EOF residual.  The optional ``split_finder`` exists
    solely for deterministic tests; production uses the legacy detector.
    """

    def __init__(
        self,
        split_folder: str | Path,
        *,
        target_height: int = 1800,
        min_height: int = 1050,
        max_height: int = 2400,
        protected_regions: Iterable[tuple[int, int]] | None = None,
        split_finder: Callable = _find_safe_horizontal_split,
    ) -> None:
        self.folder = Path(split_folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        for previous in self.folder.glob("page_*.png"):
            previous.unlink(missing_ok=True)
        self.target_height = target_height
        self.min_height = min_height
        self.max_height = max_height
        self.protected_regions = tuple(protected_regions or ())
        self.split_finder = split_finder
        self._buffer: Image.Image | None = None
        self._chunks: list[SourceChunk] = []
        self._consumed = 0
        self._next_index = 1
        self._split_children: dict[str, int] = {}
        self._finished = False
        self._cancelled = False
        self._error: Exception | None = None
        self.telemetry = {"source_reorder_max": 0, "vertical_buffer_max": 0,
                          "premature_residual_emission": 0}

    @property
    def done(self) -> bool:
        return self._finished

    @property
    def pending_height(self) -> int:
        return self._buffer.height if self._buffer is not None else 0

    def push(self, source_item_id: str, image_or_path: Image.Image | str | Path) -> tuple[FinalPageManifestItem, ...]:
        if self._finished:
            raise RuntimeError("splitter already finished")
        if self._cancelled:
            raise RuntimeError("splitter cancelled")
        if self._error is not None:
            raise RuntimeError("splitter is in error state") from self._error
        try:
            if isinstance(image_or_path, Image.Image):
                image = image_or_path.convert("RGB")
            else:
                with Image.open(image_or_path) as opened:
                    image = opened.convert("RGB")
            top = self.pending_height
            previous = self._buffer
            self._buffer = _append_vertical(previous, image)
            image.close()
            self._chunks.append(SourceChunk(str(source_item_id), top, self.pending_height))
            self.telemetry["vertical_buffer_max"] = max(self.telemetry["vertical_buffer_max"], self.pending_height)
            return tuple(self._drain(is_last_source=False))
        except Exception as exc:
            self._error = exc
            raise

    def finish(self) -> tuple[FinalPageManifestItem, ...]:
        if self._cancelled:
            return ()
        if self._finished:
            return ()
        if self._error is not None:
            raise RuntimeError("splitter is in error state") from self._error
        self._finished = True
        emitted = self._drain(is_last_source=True)
        if self._buffer is not None and self._buffer.height:
            emitted.append(self._emit(self._buffer.height, reason="chapter_end"))
            self._buffer = None
            self._chunks.clear()
        return tuple(emitted)

    def cancel(self) -> None:
        if self._finished:
            return
        self._cancelled = True
        if self._buffer is not None:
            self._buffer.close()
        self._buffer = None
        self._chunks.clear()

    def _drain(self, *, is_last_source: bool) -> list[FinalPageManifestItem]:
        emitted: list[FinalPageManifestItem] = []
        while self._buffer is not None and self._buffer.height >= self.max_height:
            hard_max = self.max_height + self.target_height
            search_max = min(self._buffer.height - 1, hard_max)
            protected = protected_vertical_intervals(
                self._buffer,
                _local_protected_regions(self.protected_regions, self._consumed, self._buffer.height),
            )
            split_y, metrics = self.split_finder(
                self._buffer, target_height=self.target_height,
                min_height=self.min_height, max_height=search_max, protected=protected,
            )
            if not metrics.get("gutter") and self._buffer.height < hard_max:
                break
            extended = hard_max + min(self.target_height // 2, 900)
            if not metrics.get("gutter") and not is_last_source and self._buffer.height < extended:
                break
            if not metrics.get("gutter") and self._buffer.height >= hard_max:
                overshoot_max = min(self._buffer.height - 1, extended)
                if overshoot_max > hard_max:
                    split_y2, metrics2 = self.split_finder(
                        self._buffer, target_height=hard_max, min_height=hard_max + 1,
                        max_height=overshoot_max, protected=protected,
                    )
                    if metrics2.get("gutter"):
                        split_y, metrics = split_y2, metrics2
            if not metrics.get("safe_band") and self.split_finder is _find_safe_horizontal_split:
                split_y, metrics = _expanded_semantic_split(
                    self._buffer, target_height=self.target_height,
                    min_height=self.min_height, hard_max_height=hard_max,
                    protected=protected, fallback=(split_y, metrics),
                )
            if not metrics.get("safe_band") and self._buffer.height < MAX_LOGICAL_PAGE_HEIGHT:
                break
            emitted.append(self._emit(split_y, reason=str(metrics.get("reason") or "safe_split")))
        return emitted

    def _emit(self, split_y: int, *, reason: str) -> FinalPageManifestItem:
        assert self._buffer is not None
        split_y = int(split_y)
        page = self._buffer.crop((0, 0, self._buffer.width, split_y))
        remainder = self._buffer.crop((0, split_y, self._buffer.width, self._buffer.height))
        page_path = self.folder / f"page_{self._next_index:03}.png"
        page.save(page_path, "PNG")
        page.close()
        source_ranges = []
        for chunk in self._chunks:
            overlap_top = max(0, chunk.top)
            overlap_bottom = min(split_y, chunk.bottom)
            if overlap_bottom > overlap_top:
                source_ranges.append(f"{chunk.source_item_id}:{overlap_top}-{overlap_bottom}")
        primary = self._chunks[0].source_item_id if self._chunks else str(self._next_index)
        split_index = self._split_children.get(primary, 0)
        self._split_children[primary] = split_index + 1
        item = FinalPageManifestItem(
            logical_page_index=self._next_index,
            stable_page_id=_stable_id(primary, split_index, self._next_index, source_ranges),
            source_item_id=primary,
            split_index=split_index,
            path=str(page_path),
            metadata=tuple(sorted({"reason": reason, "source_ranges": ";".join(source_ranges),
                                    "width": str(page.width), "height": str(page.height)}.items())),
            source_ranges=tuple(source_ranges),
        )
        self._next_index += 1
        self._consumed += split_y
        self._buffer.close()
        self._buffer = remainder
        new_chunks = []
        for chunk in self._chunks:
            if chunk.bottom > split_y:
                new_chunks.append(SourceChunk(chunk.source_item_id, max(0, chunk.top - split_y), chunk.bottom - split_y))
        self._chunks = new_chunks
        return item
