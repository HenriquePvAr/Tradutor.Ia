"""Sanitized, loopback-only readiness probe for the optional remote clean worker."""
from __future__ import annotations

import os
from urllib.parse import urlsplit

import requests

from yomu.runtime.transport_errors import classify_transport_error


def _loopback_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("REMOTE_CONFIG_ERROR")
    return f"http://{parsed.netloc}"


def probe_remote_clean_readiness(http_get=None) -> dict[str, object]:
    """Probe API/schema without credentials, redirects, job submission, or provider calls."""
    http_get = http_get or requests.get
    configured = (os.getenv("YOMU_GEMINI_REMOTE_URL") or "http://127.0.0.1:18777").strip()
    try:
        base_url = _loopback_base_url(configured)
    except (TypeError, ValueError):
        return _unavailable("REMOTE_CONFIG_ERROR")

    try:
        health_response = http_get(f"{base_url}/health", timeout=2, allow_redirects=False)
        if health_response.status_code != 200:
            return _unavailable(f"HTTP_ERROR_{health_response.status_code}")
        health = health_response.json()
        if not isinstance(health, dict):
            return _unavailable("INVALID_HEALTH_RESPONSE")
        schema_response = http_get(
            f"{base_url}/v1/hard-clean/schema", timeout=2, allow_redirects=False
        )
        if schema_response.status_code != 200:
            return _unavailable(f"HTTP_ERROR_{schema_response.status_code}")
        schema = schema_response.json()
        if not isinstance(schema, dict):
            return _unavailable("INVALID_SCHEMA_RESPONSE")
    except requests.exceptions.RequestException as exc:
        return _unavailable(classify_transport_error(exc))
    except (ValueError, TypeError):
        return _unavailable("INVALID_REMOTE_RESPONSE")

    api_up = health.get("api") == "UP"
    queue_up = health.get("queue") == "UP"
    worker = health.get("worker_health")
    worker = worker if isinstance(worker, dict) else {}
    worker_ready = health.get("worker") == "UP"
    consumers = worker.get("ready_consumers", 0)
    consumer_ready = isinstance(consumers, int) and not isinstance(consumers, bool) and consumers > 0
    schemas = schema.get("supported_schemas")
    schema_ready = (schema.get("schema_version") == "safe-clean.v2"
                    and isinstance(schemas, list)
                    and "safe-clean.v2" in schemas
                    and "remote-visual-recovery.v1" in schemas
                    and schema.get("translation_enabled") is False)
    if not api_up:
        error_code = "API_UNAVAILABLE"
    elif not queue_up:
        error_code = "QUEUE_UNAVAILABLE"
    elif not worker_ready or not consumer_ready:
        error_code = "WORKER_UNAVAILABLE"
    elif not schema_ready:
        error_code = "SCHEMA_MISMATCH"
    else:
        error_code = None
    dispatch_ready = error_code is None
    return {
        "status": "available" if dispatch_ready else "degraded",
        "api": "UP" if api_up else "DOWN",
        "queue": "UP" if queue_up else "DOWN",
        "worker": "UP" if worker_ready else "DOWN",
        "consumer": "READY" if consumer_ready else "UNAVAILABLE",
        "schema": "COMPATIBLE" if schema_ready else "INCOMPATIBLE",
        "remote_error_code": error_code,
        "ready_for_remote_dispatch": dispatch_ready,
        # UI availability means only that the browser UI answered; it proves no login.
        "gemini_ui_state": "UI_AVAILABLE" if worker.get("gemini_ui_available") is True else "UNKNOWN",
        "provider_auth": "NOT_VERIFIED",
        "ready_for_real_generation": False,
    }


def _unavailable(code: str) -> dict[str, object]:
    return {
        "status": "unavailable",
        "api": "UNKNOWN",
        "queue": "UNKNOWN",
        "worker": "UNKNOWN",
        "consumer": "UNKNOWN",
        "schema": "UNKNOWN",
        "remote_error_code": code,
        "ready_for_remote_dispatch": False,
        "gemini_ui_state": "UNKNOWN",
        "provider_auth": "NOT_VERIFIED",
        "ready_for_real_generation": False,
    }
