"""Bounded stdout contract for canonical child-to-parent progress events.

The child reports operation facts only. Job/run/trace identity, event ID, sequence,
and receive timestamp are bound by the parent before persistence.
"""

from __future__ import annotations

import json
import re
import sys
import threading
from typing import Any

PROGRESS_V1_PREFIX = "YOMU_PROGRESS_V1 "
_PROGRESS_OUTPUT_LOCK = threading.Lock()
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ERROR_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_ALLOWED_FIELDS = {
    "schema_version", "stage_id", "phase", "status", "stage_current",
    "stage_total", "completed_page_ids", "current_page", "operation",
    "error_code", "retryable", "terminal",
}


def emit_progress_v1(
    *,
    stage_id: str,
    phase: str,
    status: str,
    stage_current: int,
    stage_total: int,
    completed_page_ids: list[str] | tuple[str, ...] = (),
    current_page: int | None = None,
    operation: str = "",
    error_code: str = "",
    retryable: bool = False,
) -> None:
    """Write one bounded machine event to the existing stdout process stream."""
    payload = {
        "schema_version": 1,
        "stage_id": stage_id,
        "phase": phase,
        "status": status,
        "stage_current": int(stage_current),
        "stage_total": int(stage_total),
        "completed_page_ids": list(completed_page_ids),
        "current_page": current_page,
        "operation": operation,
        "error_code": error_code,
        "retryable": bool(retryable),
        "terminal": False,
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    if len(encoded) > 8192:
        raise ValueError("progress_event_too_large")
    with _PROGRESS_OUTPUT_LOCK:
        sys.stdout.write(PROGRESS_V1_PREFIX + encoded + "\n")
        sys.stdout.flush()


def parse_progress_v1_line(line: str) -> dict[str, Any] | None:
    """Parse child facts only; reject identity/sequence claims and malformed data."""
    if not isinstance(line, str) or not line.startswith(PROGRESS_V1_PREFIX):
        return None
    encoded = line[len(PROGRESS_V1_PREFIX):]
    if len(encoded) > 8192:
        return None

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("non_finite_json_number")

    try:
        payload = json.loads(encoded, object_pairs_hook=unique_object,
                             parse_constant=reject_constant)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != _ALLOWED_FIELDS:
        return None
    if (isinstance(payload.get("schema_version"), bool)
            or payload.get("schema_version") != 1
            or payload.get("terminal") is not False):
        return None
    stage_id, phase, status = payload.get("stage_id"), payload.get("phase"), payload.get("status")
    if not isinstance(stage_id, str) or not _IDENTIFIER_RE.fullmatch(stage_id):
        return None
    if not isinstance(phase, str) or not _IDENTIFIER_RE.fullmatch(phase):
        return None
    if not isinstance(status, str) or status not in {
        "running", "completed", "failed", "cancelled", "waiting",
    }:
        return None
    current, total = payload.get("stage_current"), payload.get("stage_total")
    if (isinstance(current, bool) or not isinstance(current, int) or current < 0
            or isinstance(total, bool) or not isinstance(total, int) or total < 0
            or (total and current > total)):
        return None
    pages = payload.get("completed_page_ids")
    if (not isinstance(pages, list) or len(pages) > 4096
            or any(not isinstance(item, str) or not item or len(item) > 120 for item in pages)
            or len(set(pages)) != len(pages)):
        return None
    page = payload.get("current_page")
    if page is not None and (isinstance(page, bool) or not isinstance(page, int) or page < 1):
        return None
    operation = payload.get("operation")
    if not isinstance(operation, str) or len(operation) > 80:
        return None
    error_code = payload.get("error_code")
    if not isinstance(error_code, str) or (error_code and not _ERROR_RE.fullmatch(error_code)):
        return None
    if not isinstance(payload.get("retryable"), bool):
        return None
    return payload
