"""Bounded producer/consumer orchestration for serial OCR jobs.

The OCR implementation remains in :func:`ocr_parallel.detect_ocr_job`; this
module only owns the bounded handoff, close/error propagation and ordering.
"""

from __future__ import annotations

import queue
import threading
import time

from ocr_engine import OCREngine
from ocr_parallel import detect_ocr_job


_CLOSE = object()


def run_ocr_stream(
    jobs,
    ocr_lang,
    *,
    capacity=2,
    cancel_event=None,
    event_callback=None,
    engine_factory=OCREngine,
    result_callback=None,
    progress_callback=None,
):
    """Consume an iterable of jobs with one serial OCR consumer.

    ``jobs`` is never materialized. The producer blocks on the bounded queue,
    and exactly one consumer invokes the shared single-job primitive.
    """
    capacity = max(1, int(capacity))
    if cancel_event is None:
        cancel_event = threading.Event()
    work = queue.Queue(maxsize=capacity)
    results = {}
    producer_error = []
    consumer_error = []
    metrics = {
        "queue_capacity": capacity,
        "queue_depth_max": 0,
        "producer_blocked_ms": 0.0,
        "consumer_idle_ms": 0.0,
        "jobs_produced": 0,
        "jobs_started": 0,
        "jobs_completed": 0,
        "max_in_flight": 1,
    }
    metrics_lock = threading.Lock()

    def emit(name, **payload):
        if event_callback:
            event_callback(name, payload)

    def put_bounded(item):
        page_index = item.get("index") if isinstance(item, dict) else None
        emit("OCR_QUEUE_PUT_START", page_index=page_index)
        blocked_started = None
        while True:
            if cancel_event.is_set():
                raise RuntimeError("ocr_stream_cancelled")
            if blocked_started is None:
                try:
                    work.put_nowait(item)
                    break
                except queue.Full:
                    blocked_started = time.perf_counter()
                    emit("OCR_QUEUE_WAIT")
                    continue
            started = time.perf_counter()
            try:
                work.put(item, timeout=0.05)
                break
            except queue.Full:
                metrics["producer_blocked_ms"] += (time.perf_counter() - started) * 1000.0
        if blocked_started is not None:
            metrics["producer_blocked_ms"] += (time.perf_counter() - blocked_started) * 1000.0
        with metrics_lock:
            metrics["queue_depth_max"] = max(metrics["queue_depth_max"], work.qsize())
        emit("OCR_QUEUE_PUT_END", page_index=page_index, queue_depth=work.qsize())

    def produce():
        try:
            for job in jobs:
                if cancel_event.is_set():
                    break
                put_bounded(job)
                metrics["jobs_produced"] += 1
                emit("OCR_JOB_READY", page_index=job.get("index"))
            put_bounded(_CLOSE)
            emit("OCR_STREAM_CLOSED")
        except BaseException as exc:
            producer_error.append(exc)
            cancel_event.set()
            try:
                work.put(_CLOSE, timeout=0.2)
            except queue.Full:
                pass

    def consume():
        engine = engine_factory(ocr_lang, engine="rapidocr", fallback_engine="")
        while True:
            if cancel_event.is_set() and not producer_error:
                return
            idle_started = time.perf_counter()
            item = work.get()
            metrics["consumer_idle_ms"] += (time.perf_counter() - idle_started) * 1000.0
            if item is _CLOSE:
                return
            metrics["jobs_started"] += 1
            emit("OCR_START", page_index=item.get("index"))
            try:
                result = detect_ocr_job(
                    item,
                    engine,
                    result_callback=result_callback,
                    progress_callback=progress_callback,
                )
                results[result["index"]] = result
                metrics["jobs_completed"] += 1
                emit("OCR_END", page_index=result.get("index"), error=bool(result.get("error")))
            except BaseException as exc:
                consumer_error.append(exc)
                cancel_event.set()
                return

    producer = threading.Thread(target=produce, name="ocr-stream-producer", daemon=True)
    consumer = threading.Thread(target=consume, name="ocr-stream-consumer", daemon=True)
    producer.start()
    consumer.start()
    producer.join()
    consumer.join()
    if producer_error:
        raise producer_error[0]
    if consumer_error:
        raise consumer_error[0]
    if cancel_event.is_set() and metrics["jobs_completed"] < metrics["jobs_produced"]:
        raise RuntimeError("ocr_stream_cancelled")
    return results, metrics
