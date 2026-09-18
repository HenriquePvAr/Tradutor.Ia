"""Small opt-in streaming bridge for page-local pipeline stages.

This module deliberately does not alter translation/provider semantics.  It owns only
page input -> preprocess -> OCR -> render handoff; callers choose whether a chapter
can use it.  Every item retains its logical identity and final output is reordered.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


_END = object()


@dataclass(frozen=True)
class StreamPage:
    page_index: int
    page_id: str
    source_identity: str
    input_data: Any


@dataclass
class StreamingMetrics:
    queue_depth_max: int = 0
    reorder_buffer_depth_max: int = 0
    producer_blocked_ms: float = 0.0
    consumer_idle_ms: float = 0.0
    queue_wait_ms: float = 0.0
    first_input_ready_ms: float | None = None
    first_preprocess_start_ms: float | None = None
    first_ocr_start_ms: float | None = None
    first_ocr_complete_ms: float | None = None
    first_render_ms: float | None = None
    pages_completed: int = 0
    out_of_order_completions: int = 0


class StreamingStageError(RuntimeError):
    def __init__(self, stage: str, page_index: int, cause: BaseException):
        super().__init__(f"{stage} failed for page {page_index}: {type(cause).__name__}")
        self.stage = stage
        self.page_index = page_index
        self.cause = cause


def _put_bounded(target, value, *, stop, metrics, started):
    while True:
        if stop.is_set():
            return False
        try:
            target.put(value, timeout=0.02)
            waited = time.perf_counter() - started
            if waited > 0.02:
                metrics.producer_blocked_ms += waited * 1000.0
                metrics.queue_wait_ms += waited * 1000.0
            return True
        except queue.Full:
            continue


def run_streaming(
    pages: Iterable[StreamPage],
    *,
    preprocess: Callable[[StreamPage], Any],
    ocr: Callable[[StreamPage, Any], Any],
    render: Callable[[StreamPage, Any, Any], Any],
    queue_capacity: int = 2,
    preprocess_workers: int = 2,
    ocr_workers: int = 1,
    cancel_event: threading.Event | None = None,
    event_callback: Callable[[str, dict], None] | None = None,
) -> tuple[list[dict], StreamingMetrics]:
    """Run page-local stages with bounded queues and deterministic output order."""
    if queue_capacity < 1:
        raise ValueError("queue_capacity must be positive")
    if preprocess_workers < 1 or ocr_workers < 1:
        raise ValueError("worker counts must be positive")
    started = time.perf_counter()
    stop = cancel_event or threading.Event()
    metrics = StreamingMetrics()
    input_q: queue.Queue = queue.Queue(maxsize=queue_capacity)
    ocr_q: queue.Queue = queue.Queue(maxsize=queue_capacity)
    render_q: queue.Queue = queue.Queue(maxsize=queue_capacity)
    failures: list[StreamingStageError] = []
    failure_lock = threading.Lock()
    completed: dict[int, dict] = {}
    completed_lock = threading.Lock()
    expected_indices: list[int] = []

    def emit(name: str, **data):
        if event_callback:
            event_callback(name, data)

    def fail(stage: str, page: StreamPage, exc: BaseException):
        with failure_lock:
            failures.append(StreamingStageError(stage, page.page_index, exc))
        stop.set()
        emit("STREAM_STAGE_ERROR", stage=stage, page_index=page.page_index,
             error=type(exc).__name__)

    def preprocess_worker():
        while not stop.is_set():
            try:
                item = input_q.get(timeout=0.02)
            except queue.Empty:
                continue
            if item is _END:
                input_q.task_done(); return
            page = item
            try:
                now = (time.perf_counter() - started) * 1000.0
                metrics.first_preprocess_start_ms = (
                    now if metrics.first_preprocess_start_ms is None
                    else metrics.first_preprocess_start_ms)
                emit("PAGE_PREPROCESS_START", page_index=page.page_index)
                value = preprocess(page)
                if not _put_bounded(ocr_q, (page, value), stop=stop,
                                    metrics=metrics, started=time.perf_counter()):
                    return
            except BaseException as exc:  # stage boundary converts to sanitized error
                fail("preprocess", page, exc)
            finally:
                input_q.task_done()

    def ocr_worker():
        while not stop.is_set():
            try:
                item = ocr_q.get(timeout=0.02)
            except queue.Empty:
                continue
            if item is _END:
                ocr_q.task_done(); return
            page, preprocessed = item
            try:
                now = (time.perf_counter() - started) * 1000.0
                metrics.first_ocr_start_ms = now if metrics.first_ocr_start_ms is None else metrics.first_ocr_start_ms
                emit("PAGE_OCR_START", page_index=page.page_index)
                value = ocr(page, preprocessed)
                now = (time.perf_counter() - started) * 1000.0
                metrics.first_ocr_complete_ms = now if metrics.first_ocr_complete_ms is None else metrics.first_ocr_complete_ms
                if not _put_bounded(render_q, (page, preprocessed, value), stop=stop,
                                    metrics=metrics, started=time.perf_counter()):
                    return
            except BaseException as exc:
                fail("ocr", page, exc)
            finally:
                ocr_q.task_done()

    def render_worker():
        while not stop.is_set():
            try:
                item = render_q.get(timeout=0.02)
            except queue.Empty:
                continue
            if item is _END:
                render_q.task_done(); return
            page, preprocessed, ocr_value = item
            try:
                emit("PAGE_RENDER_START", page_index=page.page_index)
                output = render(page, preprocessed, ocr_value)
                now = (time.perf_counter() - started) * 1000.0
                metrics.first_render_ms = now if metrics.first_render_ms is None else metrics.first_render_ms
                with completed_lock:
                    completed[page.page_index] = {
                        "page_index": page.page_index, "page_id": page.page_id,
                        "source_identity": page.source_identity, "output": output,
                    }
                    metrics.reorder_buffer_depth_max = max(
                        metrics.reorder_buffer_depth_max, len(completed))
                emit("PAGE_RENDER_END", page_index=page.page_index)
            except BaseException as exc:
                fail("render", page, exc)
            finally:
                render_q.task_done()

    pre_threads = [threading.Thread(target=preprocess_worker, daemon=True, name=f"stream-pre-{i}")
                   for i in range(preprocess_workers)]
    ocr_threads = [threading.Thread(target=ocr_worker, daemon=True, name=f"stream-ocr-{i}")
                   for i in range(ocr_workers)]
    render_thread = threading.Thread(target=render_worker, daemon=True, name="stream-render")
    for thread in pre_threads + ocr_threads + [render_thread]: thread.start()

    try:
        for page in pages:
            if stop.is_set(): break
            expected_indices.append(page.page_index)
            ready = (time.perf_counter() - started) * 1000.0
            metrics.first_input_ready_ms = ready if metrics.first_input_ready_ms is None else metrics.first_input_ready_ms
            emit("PAGE_INPUT_READY", page_index=page.page_index)
            if not _put_bounded(input_q, page, stop=stop, metrics=metrics, started=time.perf_counter()):
                break
            metrics.queue_depth_max = max(metrics.queue_depth_max, input_q.qsize(), ocr_q.qsize(), render_q.qsize())
    finally:
        for _ in pre_threads: _put_bounded(input_q, _END, stop=stop, metrics=metrics, started=time.perf_counter())
        for thread in pre_threads: thread.join(timeout=2.0)
        for _ in ocr_threads: _put_bounded(ocr_q, _END, stop=stop, metrics=metrics, started=time.perf_counter())
        for thread in ocr_threads: thread.join(timeout=2.0)
        _put_bounded(render_q, _END, stop=stop, metrics=metrics, started=time.perf_counter())
        render_thread.join(timeout=2.0)

    if any(thread.is_alive() for thread in pre_threads + ocr_threads + [render_thread]):
        stop.set()
        raise RuntimeError("streaming stage did not terminate")
    if failures:
        raise failures[0]
    ordered = [completed[index] for index in sorted(expected_indices) if index in completed]
    metrics.pages_completed = len(ordered)
    metrics.out_of_order_completions = max(0, len(completed) - len(ordered))
    return ordered, metrics
