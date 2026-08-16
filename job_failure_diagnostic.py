"""Sanitized, structured evidence for a job failure that has no console.

The production worker is launched detached with ``stdout``/``stderr`` set to ``DEVNULL``,
so an exception ending a job in ``source_analysis`` used to leave nothing behind but a
generic ``reason_code``.  This module turns that exception into a small bounded JSON
record - class, exception chain root, traceback, origin frame, process/runtime context -
written next to the per-job log and referenced from the job row's ``error_trace_path``.

It answers *what* failed, *where*, *with which exception* and *in which process*, without
persisting an environment dump, a command line, or anything a secret can hide in.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from ui_helpers import sanitize_diagnostic_text

SCHEMA_VERSION = 1
KIND = "job_failure_diagnostic"
# The taxonomy's catch-all: a reason code that proves nothing about the exception.
GENERIC_REASON_CODES = frozenset({"source_analysis_failed"})
REASON_CLASSIFIER_SOURCE = "down._pipeline_exception_code"

_MAX_FRAMES = 12
_MAX_MESSAGE = 500
_REPO_ROOT = Path(__file__).resolve().parent


def _safe(text: Any) -> str:
    return sanitize_diagnostic_text(str(text or ""))[:_MAX_MESSAGE]


def _root_exception(exc: BaseException) -> BaseException:
    """Follow ``__cause__``/``__context__`` so a generic wrapper cannot hide the truth."""
    seen: set[int] = {id(exc)}
    current = exc
    while True:
        following = current.__cause__ or current.__context__
        if following is None or id(following) in seen:
            return current
        seen.add(id(following))
        current = following


def _process_mode() -> str:
    return "pythonw" if Path(sys.executable).name.casefold().startswith("pythonw") else "python"


def _is_repository_frame(filename: str) -> bool:
    try:
        return Path(filename).resolve().is_relative_to(_REPO_ROOT)
    except (OSError, ValueError):
        return False


def build_failure_diagnostic(
    exc: BaseException,
    *,
    job_id: str,
    run_id: str = "",
    reason_code: str,
    stage: str = "source_analysis",
    worker_pid: int | None = None,
    reason_classifier_source: str = REASON_CLASSIFIER_SOURCE,
) -> dict[str, Any]:
    """Return the persistable record for ``exc``.  Never raises on odd exceptions."""
    frames = traceback.extract_tb(exc.__traceback__)
    origin = next(
        (frame for frame in reversed(frames) if _is_repository_frame(frame.filename)),
        frames[-1] if frames else None,
    )
    root = _root_exception(exc)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "stage": stage,
        "job_id": str(job_id or ""),
        "run_id": str(run_id or ""),
        "timestamp": time.time(),
        "reason_code": str(reason_code or ""),
        # A generic reason must stay generic, but it must not read as "no evidence".
        "reason_detail": (
            "unclassified_exception" if reason_code in GENERIC_REASON_CODES
            else "classified_exception"
        ),
        "reason_classifier_source": reason_classifier_source,
        "exception_class": type(exc).__name__,
        "exception_module": type(exc).__module__,
        "safe_message": _safe(exc),
        "root_exception_class": type(root).__name__,
        "root_exception_module": type(root).__module__,
        "root_safe_message": _safe(root),
        "traceback_summary": [
            _safe(f"{frame.filename}:{frame.lineno} in {frame.name}")
            for frame in frames[-_MAX_FRAMES:]
        ],
        "origin_file": _safe(origin.filename) if origin else "",
        "origin_function": origin.name if origin else "",
        "origin_line": int(origin.lineno or 0) if origin else 0,
        "process_executable": _safe(sys.executable),
        "process_mode": _process_mode(),
        "cwd": _safe(os.getcwd()),
        "worker_pid": int(worker_pid) if worker_pid is not None else None,
    }


def write_failure_diagnostic(path: Path, payload: dict[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def load_failure_diagnostic(job: dict[str, Any]) -> dict[str, Any]:
    """Read back the diagnostic for a job row.

    A run that failed before this record existed cannot gain one retroactively, so it is
    reported as unavailable rather than reconstructed.
    """
    reason_code = str((job or {}).get("reason_code") or "")
    unavailable = {
        "available": False,
        "reason_code": reason_code,
        "exception_class": None,
    }
    path = str((job or {}).get("error_trace_path") or "")
    if not path:
        return unavailable
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return unavailable
    if not isinstance(payload, dict) or payload.get("kind") != KIND:
        return unavailable
    return {"available": True, **payload}
