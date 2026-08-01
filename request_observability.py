"""Opt-in, local-only HTTP request accounting for controlled UI validation."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable


AsgiApp = Callable[[dict[str, Any], Callable[..., Awaitable[Any]], Callable[..., Awaitable[Any]]], Awaitable[None]]


class StructuredRequestAuditMiddleware:
    """Write one sanitized JSON record per completed HTTP request.

    The caller supplies an already validated local destination. Query strings,
    headers, bodies, client addresses and response content are never inspected.
    """

    def __init__(self, app: AsgiApp, *, destination: Path | None = None) -> None:
        self.app = app
        self.destination = Path(destination).resolve() if destination else None
        self._lock = threading.Lock()

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or self.destination is None:
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = 500

        async def audited_send(message):
            nonlocal status
            if message.get("type") == "http.response.start":
                status = int(message.get("status") or 500)
            await send(message)

        try:
            await self.app(scope, receive, audited_send)
        finally:
            record = {
                "at": time.time(),
                "method": str(scope.get("method") or "").upper()[:12],
                "path": str(scope.get("path") or "/")[:240],
                "status": status,
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            }
            self.destination.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            with self._lock:
                with self.destination.open("a", encoding="utf-8") as stream:
                    stream.write(line)


def configured_request_audit_destination(repo_root: Path, environ: dict[str, str]) -> Path | None:
    """Resolve an opt-in destination, constrained to this checkout's `.runtime`."""
    if environ.get("TRADUTOR_REQUEST_OBSERVABILITY") != "1":
        return None
    raw = str(environ.get("TRADUTOR_REQUEST_AUDIT_PATH") or "").strip()
    if not raw:
        raise RuntimeError("request_observability_path_required")
    destination = Path(raw).expanduser().resolve()
    allowed = (Path(repo_root).resolve() / ".runtime").resolve()
    try:
        destination.relative_to(allowed)
    except ValueError as exc:
        raise RuntimeError("request_observability_path_not_local") from exc
    return destination
