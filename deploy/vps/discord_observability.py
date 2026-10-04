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
_delivered_total = 0
_last_delivery_status = "NONE"
_last_event_id = ""


def _webhook() -> str:
    value = os.environ.get("YOMU_DISCORD_WEBHOOK_URL", "").strip()
    if value:
        return value
    path = Path(os.environ.get("YOMU_DISCORD_WEBHOOK_FILE", "/home/ubuntu/yomu/.secrets/discord.env"))
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "YOMU_DISCORD_WEBHOOK_URL":
                return value.strip().strip('"').strip("'")
        return ""
    except OSError:
        return ""


def _content(event: dict[str, Any]) -> str:
    name = str(event.get("event_name", "UNKNOWN"))
    job = str(event.get("job_id", "unknown"))[:32]
    status = str(event.get("status", ""))[:40]
    if event.get("test_message"):
        return ("🧪 Yomu · Teste de observabilidade\n\nEnvironment: TEST\nWorker: Online\n"
                f"Browsers: {event.get('browser_count', 'UNKNOWN')}\nSlots: {event.get('slots', 'UNKNOWN')}\n"
                f"Concurrency: {event.get('max_concurrency', 'UNKNOWN')}\nQueue: {event.get('queue_depth', 'UNKNOWN')}\n"
                f"Version: {event.get('version', 'UNKNOWN')}")
    if name == "JOB_STARTED":
        return ("🟢 Yomu · Trabalho iniciado\n\nObra\nTeste sintético\n\nCapítulo\nObservability E2E\n\n"
                f"Modo\nApenas Clean\n\nPáginas\n{event.get('pages', 'UNKNOWN')}\n\nSolicitado por\nTeste local\n\nJob\n{job}")
    if name == "REMOTE_FALLBACK_STARTED":
        return ("🌐 Yomu · Fallback remoto\n\nElegíveis: " + str(event.get("eligible", "UNKNOWN")) +
                "\nLocal: " + str(event.get("local", "UNKNOWN")) +
                "\nProcessados: " + str(event.get("processed", "0 / UNKNOWN")) +
                "\nChat reuse: UNKNOWN")
    if name == "JOB_COMPLETED":
        return ("✅ Yomu · Concluído\n\nPáginas: " + str(event.get("pages", "UNKNOWN")) +
                "\nLocal: " + str(event.get("local", "UNKNOWN")) +
                "\nGemini: " + str(event.get("gemini", "UNKNOWN")) +
                "\nReview: " + str(event.get("review", "UNKNOWN")) +
                "\nErros: " + str(event.get("errors", "UNKNOWN")) +
                "\nProvider calls: 0\nYK: 0\nArtifacts: synthetic\nSafety: outside-mask 0; SFX damage 0")
    if name == "REMOTE_PROGRESS":
        return f"🔄 Yomu · Progresso\n\nJob: {job}\nMilestone: {event.get('percent', 'UNKNOWN')}%"
    detail = f" status={status}" if status else ""
    return f"[YOMU][OBSERVABILITY] {name} job={job}{detail}"


def _deliver(webhook: str, content: str) -> None:
    for attempt in range(3):
        response = requests.post(webhook, json={"content": content}, timeout=5)
        if response.status_code != 429 and response.status_code < 500:
            response.raise_for_status()
            return
        if attempt == 2:
            response.raise_for_status()
        retry_after = min(float(response.headers.get("Retry-After", "1") or 1), 5.0)
        time.sleep(max(0.0, retry_after))


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
    global _delivered_total, _last_delivery_status, _last_event_id
    raw = payload.get("events")
    if not isinstance(raw, list) or len(raw) > 32:
        raise HTTPException(400, "events must be a list of at most 32")
    accepted = [event for event in raw if isinstance(event, dict) and _accept(event)]
    webhook = _webhook()
    delivered = 0
    if webhook:
        for event in accepted:
            _deliver(webhook, _content(event))
            delivered += 1
    _delivered_total += delivered
    _last_delivery_status = "PASS" if delivered == len(accepted) else ("NOOP" if not accepted else "NOT_CONFIGURED")
    _last_event_id = str(accepted[-1].get("event_id", "")) if accepted else _last_event_id
    return {"accepted": len(accepted), "delivered": delivered, "webhook_configured": bool(webhook)}


@router.get("/v1/observability/health")
def observability_health():
    return {"api": "PASS", "schema_version": SCHEMA_VERSION, "webhook_configured": bool(_webhook()), "dedupe_entries": len(_seen), "delivered_total": _delivered_total, "last_delivery_status": _last_delivery_status, "last_event_id": _last_event_id}
