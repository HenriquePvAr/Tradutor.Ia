"""Stable, privacy-safe event contract shared by the desktop and VPS."""
from __future__ import annotations

import hashlib
import re
import time
import uuid
from typing import Any

SCHEMA_VERSION = "yomu-observability-v1"
EVENTS = frozenset({
    "WORKER_READY", "JOB_CREATED", "JOB_STARTED", "JOB_ANALYZED",
    "REMOTE_FALLBACK_QUEUED", "REMOTE_FALLBACK_STARTED", "REMOTE_PROGRESS",
    "REVIEW_REQUIRED", "JOB_COMPLETED", "JOB_FAILED", "REMOTE_AUTH_REQUIRED",
    "WORKER_DEGRADED",
})
_FORBIDDEN = re.compile(r"(token|secret|cookie|password|authorization|webhook|prompt|email|ssh|credential|payload|raw_text|translated_text)", re.I)
_SAFE = re.compile(r"^[A-Za-z0-9._:/-]{1,180}$")


def _identity(value: Any, *, fallback: str = "unknown") -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    return text if _SAFE.fullmatch(text) else hashlib.sha256(text.encode()).hexdigest()[:16]


def redact_event(event: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in event.items():
        if _FORBIDDEN.search(str(key)):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[str(key)] = value
    return out


def build_event(event_name: str, *, job_id: str = "", run_id: str = "", request_id: str = "", **fields: Any) -> dict[str, Any]:
    name = str(event_name).upper()
    if name not in EVENTS:
        raise ValueError(f"unsupported_event:{name}")
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_id": uuid.uuid4().hex,
        "event_name": name,
        "timestamp": time.time(),
        "job_id": _identity(job_id),
        "run_id": _identity(run_id),
        "request_id": _identity(request_id),
        **fields,
    }
    return redact_event(event)
