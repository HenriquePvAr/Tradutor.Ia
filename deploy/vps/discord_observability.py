"""Loopback-only observability ingress and Discord dispatcher for the VPS."""
from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import requests
from fastapi import APIRouter, HTTPException

SCHEMA_VERSION = "yomu-observability-v1"
EVENTS = {"WORKER_READY", "JOB_CREATED", "JOB_STARTED", "JOB_ANALYZED", "REMOTE_FALLBACK_QUEUED", "REMOTE_FALLBACK_STARTED", "REMOTE_PROGRESS", "REVIEW_REQUIRED", "JOB_COMPLETED", "JOB_FAILED", "REMOTE_AUTH_REQUIRED", "WORKER_DEGRADED"}
router = APIRouter()
_seen: OrderedDict[str, float] = OrderedDict()
_lock = threading.Lock()


def _webhook() -> str:
    value = os.environ.get("YOMU_DISCORD_WEBHOOK_URL", "").strip()
    if value:
        return value
    path = Path(os.environ.get("YOMU_DISCORD_WEBHOOK_FILE", "/etc/yomu/discord_webhook_url"))
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _content(event: dict[str, Any]) -> str:
    name = str(event.get("event_name", "UNKNOWN"))
    job = str(event.get("job_id", "unknown"))[:32]
    status = str(event.get("status", ""))[:40]
    detail = f" status={status}" if status else ""
    return f"[YOMU][OBSERVABILITY] {name} job={job}{detail}"


def _accept(event: dict[str, Any]) -> bool:
    if event.get("schema_version") != SCHEMA_VERSION or event.get("event_name") not in EVENTS:
        raise HTTPException(400, "invalid observability event")
    event_id = str(event.get("event_id") or "")
    if not event_id:
        raise HTTPException(400, "missing event_id")
    with _lock:
        if event_id in _seen:
            return False
        _seen[event_id] = time.time()
        while len(_seen) > 4096:
            _seen.popitem(last=False)
    return True


@router.post("/v1/observability/events", status_code=202)
def ingest(payload: dict[str, Any]):
    raw = payload.get("events")
    if not isinstance(raw, list) or len(raw) > 32:
        raise HTTPException(400, "events must be a list of at most 32")
    accepted = [event for event in raw if isinstance(event, dict) and _accept(event)]
    webhook = _webhook()
    delivered = 0
    if webhook:
        for event in accepted:
            response = requests.post(webhook, json={"content": _content(event)}, timeout=5)
            response.raise_for_status()
            delivered += 1
    return {"accepted": len(accepted), "delivered": delivered, "webhook_configured": bool(webhook)}


@router.get("/v1/observability/health")
def observability_health():
    return {"api": "PASS", "schema_version": SCHEMA_VERSION, "webhook_configured": bool(_webhook()), "dedupe_entries": len(_seen)}
