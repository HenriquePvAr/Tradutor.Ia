import os
import time
import ctypes
import gc
from concurrent.futures import ProcessPoolExecutor, as_completed

import cv2

import config
from adaptive_scheduler import (
    AdaptiveResourceScheduler,
    config_from_module,
    snapshot_from_psutil,
)
from ocr_engine import OCREngine
from ocr_memory_policy import choose_workers, snapshot as memory_snapshot
from pipeline_cache import deserialize_ocr_lines, serialize_ocr_lines


_WORKER_ENGINE = None


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_ulong),
        ("memory_load", ctypes.c_ulong),
        ("total_physical", ctypes.c_ulonglong),
        ("available_physical", ctypes.c_ulonglong),
        ("total_page_file", ctypes.c_ulonglong),
        ("available_page_file", ctypes.c_ulonglong),
        ("total_virtual", ctypes.c_ulonglong),
        ("available_virtual", ctypes.c_ulonglong),
        ("available_extended_virtual", ctypes.c_ulonglong),
    ]


def _available_memory_gb():
    if os.name != "nt":
        return None
    status = _MemoryStatus()
    status.length = ctypes.sizeof(_MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return status.available_physical / (1024**3)


def _process_rss_mb():
    try:
        import psutil  # type: ignore

        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except Exception:
        return 0.0


def _initialize_worker(ocr_lang):
    global _WORKER_ENGINE
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    cv2.setNumThreads(1)
    _WORKER_ENGINE = OCREngine(ocr_lang)


def _detect_in_worker(job):
    started = time.perf_counter()
    image = cv2.imread(job["image_path"])
    if image is None:
        return {
            "index": job["index"],
            "error": "image_load_failed",
            "elapsed_seconds": time.perf_counter() - started,
            "lines": [],
            "pid": os.getpid(),
        }

    try:
        lines = _WORKER_ENGINE.detect_lines(image, page=job["index"])
        serialized = serialize_ocr_lines(lines)
        metadata = dict(_WORKER_ENGINE.last_run_metadata or {})
        return {
            "index": job["index"],
            "error": None,
            "elapsed_seconds": time.perf_counter() - started,
            "lines": serialized,
            "ocr_metadata": metadata,
            "pid": os.getpid(),
            "worker_rss_mb": _process_rss_mb(),
        }
    except Exception as exc:
        return {
            "index": job["index"],
            "error": str(exc),
            "elapsed_seconds": time.perf_counter() - started,
            "lines": [],
            "pid": os.getpid(),
            "worker_rss_mb": _process_rss_mb(),
        }
    finally:
        image = None
        try:
            del lines
        except UnboundLocalError:
            pass
        gc.collect()


def _detect_sequential(jobs, ocr_lang, result_callback=None):
    engine = OCREngine(ocr_lang)
    results = {}
    for job in jobs:
        started = time.perf_counter()
        image = cv2.imread(job["image_path"])
        if image is None:
            results[job["index"]] = {
                "index": job["index"],
                "error": "image_load_failed",
                "elapsed_seconds": time.perf_counter() - started,
                "lines": [],
                "pid": os.getpid(),
            }
            if result_callback:
                result_callback(results[job["index"]])
            continue

        try:
            lines = engine.detect_lines(image, page=job["index"])
            results[job["index"]] = {
                "index": job["index"],
                "error": None,
                "elapsed_seconds": time.perf_counter() - started,
                "lines": lines,
                "ocr_metadata": engine.last_run_metadata,
                "pid": os.getpid(),
            }
        except Exception as exc:
            results[job["index"]] = {
                "index": job["index"],
                "error": str(exc),
                "elapsed_seconds": time.perf_counter() - started,
                "lines": [],
                "pid": os.getpid(),
            }
            if result_callback:
                result_callback(results[job["index"]])
            image = None
            try:
                del lines
            except UnboundLocalError:
                pass
            gc.collect()
    return results


def detect_ocr_jobs(jobs, ocr_lang, parallel=True, workers=2, result_callback=None):
    if not jobs:
        return {}, {
            "parallel_requested": bool(parallel),
            "parallel_used": False,
            "workers_requested": int(workers),
            "worker_pids": [],
            "fallback_reason": None,
        }

    workers = max(1, int(workers or 1))
    largest_pixels = 0
    for job in jobs[: min(len(jobs), 8)]:
        try:
            image = cv2.imread(job["image_path"], cv2.IMREAD_UNCHANGED)
            if image is not None:
                largest_pixels = max(largest_pixels, int(image.shape[0] * image.shape[1]))
            image = None
        except Exception:
            continue
    decision = choose_workers(
        workers,
        memory=memory_snapshot(),
        estimated_worker_peak_mb=getattr(config, "OCR_WORKER_INITIAL_PEAK_MB", 1800.0),
        reserve_mb=getattr(config, "TRADUTOR_OCR_MEMORY_RESERVE_MB", 4096.0),
        max_memory_mb=getattr(config, "TRADUTOR_MAX_MEMORY_MB", 0.0),
        engine_heavy=str(getattr(config, "OCR_ENGINE", "paddle")).lower() in {"paddle", "paddle_mobile", "rapidocr"},
        largest_image_pixels=largest_pixels,
    )
    workers = decision.workers
    available_memory_gb = _available_memory_gb()
    memory_fallback = (
        parallel
        and workers > 1
        and len(jobs) > 50
        and available_memory_gb is not None
        and available_memory_gb < 6.0
    )
    if memory_fallback:
        print(
            "Aviso: memoria livre insuficiente para OCR paralelo no capitulo "
            f"completo ({available_memory_gb:.2f} GB). Usando OCR sequencial."
        )
        return _detect_sequential(jobs, ocr_lang, result_callback), {
            "parallel_requested": True,
            "parallel_used": False,
            "workers_requested": workers,
            "worker_pids": [os.getpid()],
            "available_memory_gb": round(available_memory_gb, 3),
            "fallback_reason": "low_available_memory",
            "memory_policy": decision.__dict__,
        }

    if not parallel or workers == 1 or len(jobs) == 1:
        return _detect_sequential(jobs, ocr_lang, result_callback), {
            "parallel_requested": bool(parallel),
            "parallel_used": False,
            "workers_requested": workers,
            "worker_pids": [os.getpid()],
            "available_memory_gb": (
                round(available_memory_gb, 3)
                if available_memory_gb is not None
                else None
            ),
            "fallback_reason": None,
            "memory_policy": decision.__dict__,
        }

    results = {}
    worker_pids = set()
    fallback_reasons = []
    worker_peak_rss = {}
    adaptive_decisions = []
    scheduler = (
        AdaptiveResourceScheduler(config_from_module(config))
        if getattr(config, "ADAPTIVE_PARALLELISM", False)
        else None
    )
    current_workers = max(
        int(getattr(config, "MIN_OCR_WORKERS", 1)),
        min(workers, int(getattr(config, "MAX_OCR_WORKERS", workers))),
    )

    start = 0
    while start < len(jobs):
        remaining = len(jobs) - start
        chunk_workers = current_workers
        decision_payload = None
        if scheduler is not None:
            snapshot = snapshot_from_psutil()
            if snapshot is None:
                fallback_reasons.append("adaptive_metrics_unavailable")
                chunk_workers = max(1, min(workers, current_workers))
            else:
                decision = scheduler.decide(
                    snapshot,
                    current_workers=current_workers,
                    pending_jobs=remaining,
                )
                chunk_workers = max(1, min(workers, decision.target_workers))
                current_workers = chunk_workers
                decision_payload = {
                    "start_index": start,
                    "pending_jobs": remaining,
                    "target_workers": chunk_workers,
                    "pressure": decision.pressure,
                    "reason": decision.reason,
                    "safe_memory_budget_mb": decision.safe_memory_budget_mb,
                    "estimated_worker_peak_mb": decision.estimated_worker_peak_mb,
                }
                adaptive_decisions.append(decision_payload)
        # PaddleOCR retains native memory while processing. Recycling the pool
        # avoids long-chapter worker termination on Windows, and the bounded
        # chunk keeps futures/results from accumulating without limit.
        chunk_size = max(chunk_workers, chunk_workers * config.OCR_QUEUE_MULTIPLIER)
        chunk = jobs[start : start + chunk_size]
        unresolved = []
        try:
            with ProcessPoolExecutor(
                max_workers=chunk_workers,
                initializer=_initialize_worker,
                initargs=(ocr_lang,),
            ) as executor:
                future_map = {
                    executor.submit(_detect_in_worker, job): job for job in chunk
                }
                for future in as_completed(future_map):
                    job = future_map[future]
                    try:
                        payload = future.result()
                    except Exception as exc:
                        fallback_reasons.append(str(exc))
                        unresolved.append(job)
                        continue

                    payload["lines"] = deserialize_ocr_lines(payload.get("lines", []))
                    results[job["index"]] = payload
                    if result_callback:
                        result_callback(payload)
                    if payload.get("pid") is not None:
                        worker_pids.add(payload["pid"])
                        rss = float(payload.get("worker_rss_mb") or 0.0)
                        if rss:
                            worker_peak_rss[payload["pid"]] = max(
                                worker_peak_rss.get(payload["pid"], 0.0),
                                rss,
                            )
                            if scheduler is not None:
                                scheduler.update_worker_observation(rss)
        except Exception as exc:
            fallback_reasons.append(str(exc))
            unresolved = [
                job for job in chunk if job["index"] not in results
            ]

        if unresolved:
            print(
                "Aviso: um bloco do OCR paralelo falhou; "
                f"reprocessando {len(unresolved)} pagina(s) sequencialmente."
            )
            sequential = _detect_sequential(unresolved, ocr_lang, result_callback)
            results.update(sequential)
        start += len(chunk)

    missing = [job for job in jobs if job["index"] not in results]
    if missing:
        print(
            "Aviso: resultados OCR ausentes; "
            f"reprocessando {len(missing)} pagina(s) sequencialmente."
        )
        results.update(_detect_sequential(missing, ocr_lang, result_callback))

    return results, {
        "parallel_requested": True,
        "parallel_used": len(worker_pids) > 1,
        "workers_requested": workers,
        "worker_pids": sorted(worker_pids),
        "pool_chunk_size": max(1, current_workers * config.OCR_QUEUE_MULTIPLIER),
        "bounded_queue_multiplier": config.OCR_QUEUE_MULTIPLIER,
        "adaptive_enabled": bool(scheduler is not None),
        "adaptive_decisions": adaptive_decisions,
        "worker_peak_rss_mb": {
            str(pid): round(value, 3)
            for pid, value in sorted(worker_peak_rss.items())
        },
        "available_memory_gb": (
            round(available_memory_gb, 3)
            if available_memory_gb is not None
            else None
        ),
        "fallback_pages": sum(
            1 for payload in results.values() if payload.get("pid") == os.getpid()
        ),
        "fallback_reason": "; ".join(dict.fromkeys(fallback_reasons)) or None,
        "memory_policy": decision.__dict__,
    }
