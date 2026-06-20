import os
import time
import ctypes
from concurrent.futures import ProcessPoolExecutor, as_completed

import cv2

from ocr_engine import OCREngine
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
        return {
            "index": job["index"],
            "error": None,
            "elapsed_seconds": time.perf_counter() - started,
            "lines": serialize_ocr_lines(lines),
            "ocr_metadata": _WORKER_ENGINE.last_run_metadata,
            "pid": os.getpid(),
        }
    except Exception as exc:
        return {
            "index": job["index"],
            "error": str(exc),
            "elapsed_seconds": time.perf_counter() - started,
            "lines": [],
            "pid": os.getpid(),
        }


def _detect_sequential(jobs, ocr_lang):
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
    return results


def detect_ocr_jobs(jobs, ocr_lang, parallel=True, workers=2):
    if not jobs:
        return {}, {
            "parallel_requested": bool(parallel),
            "parallel_used": False,
            "workers_requested": int(workers),
            "worker_pids": [],
            "fallback_reason": None,
        }

    workers = max(1, int(workers or 1))
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
        return _detect_sequential(jobs, ocr_lang), {
            "parallel_requested": True,
            "parallel_used": False,
            "workers_requested": workers,
            "worker_pids": [os.getpid()],
            "available_memory_gb": round(available_memory_gb, 3),
            "fallback_reason": "low_available_memory",
        }

    if not parallel or workers == 1 or len(jobs) == 1:
        return _detect_sequential(jobs, ocr_lang), {
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
        }

    results = {}
    worker_pids = set()
    fallback_reasons = []
    # PaddleOCR retains native memory while processing. Recycling the pool
    # avoids long-chapter worker termination on Windows without lowering
    # the requested two-worker concurrency.
    chunk_size = max(workers, workers * 10)

    for start in range(0, len(jobs), chunk_size):
        chunk = jobs[start : start + chunk_size]
        unresolved = []
        try:
            with ProcessPoolExecutor(
                max_workers=workers,
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
                    if payload.get("pid") is not None:
                        worker_pids.add(payload["pid"])
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
            sequential = _detect_sequential(unresolved, ocr_lang)
            results.update(sequential)

    missing = [job for job in jobs if job["index"] not in results]
    if missing:
        print(
            "Aviso: resultados OCR ausentes; "
            f"reprocessando {len(missing)} pagina(s) sequencialmente."
        )
        results.update(_detect_sequential(missing, ocr_lang))

    return results, {
        "parallel_requested": True,
        "parallel_used": len(worker_pids) > 1,
        "workers_requested": workers,
        "worker_pids": sorted(worker_pids),
        "pool_chunk_size": chunk_size,
        "available_memory_gb": (
            round(available_memory_gb, 3)
            if available_memory_gb is not None
            else None
        ),
        "fallback_pages": sum(
            1 for payload in results.values() if payload.get("pid") == os.getpid()
        ),
        "fallback_reason": "; ".join(dict.fromkeys(fallback_reasons)) or None,
    }
