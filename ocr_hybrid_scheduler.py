"""Dynamic-queue OCR scheduler: splits pages across RapidOCR and NVIDIA OCR.

Modes (config.OCR_EXECUTION_MODE, or passed explicitly):
  rapidocr -- default, unchanged behavior: every page goes through RapidOCR.
  nvidia   -- every page goes through NVIDIA OCR; fails closed if unconfigured.
  hybrid   -- a dynamic work queue: whichever engine (RapidOCR worker pool or
              NVIDIA worker pool) is free next pulls the next unclaimed page.
              Each page is OCRed exactly once, by whichever engine took it.

This module does not touch region/group/recovery -- it only produces, per
page, a list of ``ocr_engine.OCRLine`` (tagged with ``engine``) plus telemetry.
Downstream code is unchanged: it keeps consuming OCRLine lists exactly as it
does today for the rapidocr-only path.
"""
from __future__ import annotations

import queue
import threading
import time

import cv2

import config
from ocr_contract import OCRPageResult
from ocr_engine import OCREngine


class OcrExecutionModeError(RuntimeError):
    """Raised when the requested OCR execution mode cannot run as configured."""


def _now_ms():
    return time.perf_counter() * 1000.0


def _rapidocr_page(engine: OCREngine, job) -> OCRPageResult:
    image = cv2.imread(job["image_path"])
    if image is None:
        return OCRPageResult(page_id=job["index"], engine="rapidocr", width=0, height=0, regions=[])
    lines = engine.detect_lines(image, page=job["index"])
    height, width = image.shape[0], image.shape[1]
    return OCRPageResult.from_ocr_lines(
        lines, page_id=job["index"], engine="rapidocr", width=width, height=height
    )


def _nvidia_page(provider, job) -> OCRPageResult:
    image = cv2.imread(job["image_path"])
    if image is None:
        return OCRPageResult(page_id=job["index"], engine="nvidia", width=0, height=0, regions=[])
    return provider.recognize_page(image, context={"page_id": job["index"]})


def run_rapidocr_only(jobs, ocr_lang):
    """Mode 'rapidocr': every page through RapidOCR. Preserves current
    single-engine behavior; used as the default/no-config-change path."""
    engine = OCREngine(ocr_lang)
    results = {}
    page_records = []
    started_wall = _now_ms()
    for job in jobs:
        page_start = _now_ms()
        results[job["index"]] = _rapidocr_page(engine, job)
        page_end = _now_ms()
        page_records.append(_page_record(job["index"], "rapidocr", page_start, page_end, results[job["index"]]))
    telemetry = _build_telemetry("rapidocr", page_records, started_wall, _now_ms())
    return results, telemetry


def run_nvidia_only(jobs, provider):
    """Mode 'nvidia': every page through NVIDIA OCR. Fails closed (raises)
    rather than silently returning an empty result if unconfigured."""
    if not provider.is_configured():
        raise OcrExecutionModeError(
            "nvidia_ocr_execution_mode_requires_configured_provider"
        )
    results = {}
    page_records = []
    started_wall = _now_ms()
    for job in jobs:
        page_start = _now_ms()
        try:
            page_result = _nvidia_page(provider, job)
        except Exception as exc:
            reason = getattr(exc, "reason_code", None) or "ocr_provider_outcome_unknown"
            print(f"OCR_PROVIDER_OUTCOME_UNKNOWN page={job['index']} reason={reason}")
            page_result = OCRPageResult(
                page_id=job["index"], engine="nvidia", width=0, height=0, regions=[], error=reason
            )
        results[job["index"]] = page_result
        page_end = _now_ms()
        page_records.append(_page_record(job["index"], "nvidia", page_start, page_end, page_result))
    telemetry = _build_telemetry("nvidia", page_records, started_wall, _now_ms())
    return results, telemetry


def run_hybrid(
    jobs,
    ocr_lang,
    provider,
    *,
    rapidocr_workers=None,
    nvidia_workers=None,
    cancel_event=None,
):
    """Mode 'hybrid': dynamic queue shared by a RapidOCR worker pool and an
    NVIDIA worker pool. Whichever pool is free next claims the next page --
    no odd/even split, no page OCRed by both engines.

    ``cancel_event`` (a ``threading.Event``, optional) is the deterministic
    cancellation contract: a caller running this on a worker thread sets it
    from elsewhere to ask every worker to stop claiming new pages. A request
    already in flight (RapidOCR crop or the NVIDIA HTTP call) is never killed
    -- it runs to its own completion or existing timeout -- but its result is
    discarded once cancellation was seen, and no worker starts another. When
    no ``cancel_event`` is given one is created internally so the default,
    uncancelled call behaves exactly as before.
    """
    rapidocr_workers = max(1, int(rapidocr_workers or getattr(config, "RAPIDOCR_WORKERS", 1)))
    nvidia_workers = max(1, int(nvidia_workers or getattr(config, "NVIDIA_OCR_WORKERS", 1)))
    nvidia_available = provider.is_configured()
    cancel_event = cancel_event if cancel_event is not None else threading.Event()

    work_queue: "queue.Queue" = queue.Queue()
    for job in jobs:
        work_queue.put(job)

    results = {}
    page_records = []
    lock = threading.Lock()
    nvidia_healthy = {"value": nvidia_available}
    started_wall = _now_ms()

    def record(index, engine_name, page_start, page_end, page_result):
        with lock:
            results[index] = page_result
            page_records.append(_page_record(index, engine_name, page_start, page_end, page_result))

    def rapidocr_worker():
        engine = OCREngine(ocr_lang)
        while True:
            if cancel_event.is_set():
                return
            try:
                job = work_queue.get_nowait()
            except queue.Empty:
                return
            page_start = _now_ms()
            try:
                page_result = _rapidocr_page(engine, job)
            finally:
                work_queue.task_done()
            if cancel_event.is_set():
                # A page already claimed and OCRed is finished, never killed
                # mid-read -- but a cancelled run must not surface its result.
                continue
            record(job["index"], "rapidocr", page_start, _now_ms(), page_result)

    def nvidia_worker():
        while True:
            if not nvidia_healthy["value"] or cancel_event.is_set():
                return
            try:
                job = work_queue.get_nowait()
            except queue.Empty:
                return
            page_start = _now_ms()
            try:
                page_result = _nvidia_page(provider, job)
            except Exception as exc:
                work_queue.task_done()
                if cancel_event.is_set():
                    # Cancelled while this request was in flight: the existing
                    # timeout already ended it, and the job is dropped rather
                    # than requeued -- there is no worker left that should
                    # pick it back up.
                    continue
                retryable = bool(getattr(exc, "retryable", False))
                print(f"OCR_PROVIDER_FAILED engine=nvidia page={job['index']} reason={type(exc).__name__}")
                if retryable:
                    # Safe to hand the page back to the shared queue -- a
                    # RapidOCR worker (or another NVIDIA worker, if it
                    # recovers) will pick it up. Mark NVIDIA unhealthy so no
                    # more *new* pages are pulled by this pool once a failure
                    # is seen, per mission §17.
                    print(f"OCR_PAGE_REQUEUED page={job['index']}")
                    nvidia_healthy["value"] = False
                    work_queue.put(job)
                else:
                    reason = getattr(exc, "reason_code", None) or "ocr_provider_outcome_unknown"
                    print(f"OCR_PROVIDER_OUTCOME_UNKNOWN page={job['index']} reason={reason}")
                    record(
                        job["index"],
                        "nvidia",
                        page_start,
                        _now_ms(),
                        OCRPageResult(
                            page_id=job["index"], engine="nvidia", width=0, height=0, regions=[], error=reason
                        ),
                    )
                continue
            work_queue.task_done()
            if cancel_event.is_set():
                # The in-flight NVIDIA call above returned (or timed out)
                # after cancellation was requested: discard per contract,
                # never requeue or record it, never start another.
                continue
            record(job["index"], "nvidia", page_start, _now_ms(), page_result)

    threads = [threading.Thread(target=rapidocr_worker, daemon=True) for _ in range(rapidocr_workers)]
    if nvidia_available:
        threads += [threading.Thread(target=nvidia_worker, daemon=True) for _ in range(nvidia_workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    if cancel_event.is_set():
        # A cancelled run never starts new OCR work for whatever is left in
        # the queue: drain it unprocessed so queue.join() (present or not)
        # never blocks on this pool, and no worker stays alive after this
        # point -- every thread above already returned.
        while True:
            try:
                work_queue.get_nowait()
            except queue.Empty:
                break
            work_queue.task_done()
    elif not work_queue.empty():
        # Anything still unclaimed (e.g. NVIDIA pool went unhealthy with no
        # RapidOCR worker left to drain the queue) is finished sequentially on
        # RapidOCR so the job never deadlocks.
        fallback_engine = OCREngine(ocr_lang)
        while True:
            try:
                job = work_queue.get_nowait()
            except queue.Empty:
                break
            page_start = _now_ms()
            page_result = _rapidocr_page(fallback_engine, job)
            work_queue.task_done()
            record(job["index"], "rapidocr", page_start, _now_ms(), page_result)

    telemetry = _build_telemetry("hybrid", page_records, started_wall, _now_ms())
    return results, telemetry


def run_ocr(jobs, ocr_lang, *, mode=None, provider=None, cancel_event=None):
    """Single dispatch entry point. ``mode`` defaults to
    ``config.OCR_EXECUTION_MODE`` (default 'rapidocr' -- unchanged behavior).
    ``cancel_event`` only affects mode 'hybrid'; see ``run_hybrid``."""
    mode = str(mode or getattr(config, "OCR_EXECUTION_MODE", "rapidocr")).strip().lower()
    if mode == "rapidocr":
        return run_rapidocr_only(jobs, ocr_lang)
    if provider is None:
        from nvidia_ocr_provider import NvidiaOCRProvider

        provider = NvidiaOCRProvider()
    if mode == "nvidia":
        return run_nvidia_only(jobs, provider)
    if mode == "hybrid":
        return run_hybrid(jobs, ocr_lang, provider, cancel_event=cancel_event)
    raise OcrExecutionModeError(f"unknown_ocr_execution_mode:{mode}")


def _page_record(index, engine_name, start_ms, end_ms, page_result):
    return {
        "page": index,
        "ocr_engine": engine_name,
        "start_ms": round(start_ms, 3),
        "end_ms": round(end_ms, 3),
        "duration_ms": round(end_ms - start_ms, 3),
        "region_count": page_result.region_count,
    }


def _build_telemetry(mode, page_records, wall_start_ms, wall_end_ms):
    rapidocr_records = [r for r in page_records if r["ocr_engine"] == "rapidocr"]
    nvidia_records = [r for r in page_records if r["ocr_engine"] == "nvidia"]
    rapidocr_total_ms = sum(r["duration_ms"] for r in rapidocr_records)
    nvidia_total_ms = sum(r["duration_ms"] for r in nvidia_records)
    sequential_sum_ms = rapidocr_total_ms + nvidia_total_ms
    wall_clock_ms = round(wall_end_ms - wall_start_ms, 3)
    savings_ms = round(sequential_sum_ms - wall_clock_ms, 3)
    speedup = round(sequential_sum_ms / wall_clock_ms, 3) if wall_clock_ms > 0 else 0.0
    return {
        "OCR_EXECUTION_MODE": mode,
        # RAPIDOCR_FULL_PAGE_* / NVIDIA_FULL_PAGE_* are the mission-13 telemetry
        # names surfaced through run_stats; the shorter *_PAGES/*_CALLS aliases
        # without FULL_PAGE are kept for the existing test_nvidia_ocr_hybrid.py
        # assertions -- same numbers, two key spellings, nothing recomputed.
        "RAPIDOCR_FULL_PAGE_PAGES": len(rapidocr_records),
        "NVIDIA_FULL_PAGE_PAGES": len(nvidia_records),
        "RAPIDOCR_FULL_PAGE_CALLS": len(rapidocr_records),
        "NVIDIA_FULL_PAGE_CALLS": len(nvidia_records),
        "RAPIDOCR_FULL_PAGE_MS": round(rapidocr_total_ms, 3),
        "NVIDIA_FULL_PAGE_MS": round(nvidia_total_ms, 3),
        "RAPIDOCR_PAGES": len(rapidocr_records),
        "NVIDIA_OCR_PAGES": len(nvidia_records),
        "RAPIDOCR_CALLS": len(rapidocr_records),
        "NVIDIA_OCR_CALLS": len(nvidia_records),
        "RAPIDOCR_TOTAL_MS": round(rapidocr_total_ms, 3),
        "NVIDIA_OCR_TOTAL_MS": round(nvidia_total_ms, 3),
        # Regional recovery (apply_speech_container_reocr / rapidocr region
        # retry) runs downstream of this module, in benchmark_pipeline; it is
        # not double-counted here. benchmark_pipeline fills these two in from
        # its own existing regional-recovery bookkeeping (mission SS13).
        "RAPIDOCR_REGIONAL_RECOVERY_CALLS": 0,
        "RAPIDOCR_REGIONAL_RECOVERY_MS": 0.0,
        "OCR_WALL_CLOCK_MS": wall_clock_ms,
        "OCR_SEQUENTIAL_SUM_MS": round(sequential_sum_ms, 3),
        "OCR_PARALLEL_SAVINGS_MS": savings_ms,
        "OCR_PARALLEL_SPEEDUP": speedup,
        "pages": page_records,
    }
