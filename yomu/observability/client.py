"""Non-blocking, deduplicated client for the private VPS observability API."""
from __future__ import annotations

import queue
import threading
import time
from collections import OrderedDict
from typing import Any

import requests

from .events import build_event


class ObservabilityClient:
    def __init__(self, base_url: str, *, timeout: float = 3.0, progress_interval: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.progress_interval = progress_interval
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=256)
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._last_progress: dict[str, float] = {}
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="yomu-observability", daemon=True)
        self._thread.start()

    def emit(self, event_name: str, *, job_id: str = "", run_id: str = "", request_id: str = "", **fields: Any) -> bool:
        event = build_event(event_name, job_id=job_id, run_id=run_id, request_id=request_id, **fields)
        key = str(event.get("event_id"))
        with self._lock:
            if key in self._seen:
                return False
            now = time.monotonic()
            if event_name == "REMOTE_PROGRESS" and job_id:
                if now - self._last_progress.get(job_id, 0.0) < self.progress_interval:
                    return False
                self._last_progress[job_id] = now
            self._seen[key] = now
            while len(self._seen) > 2048:
                self._seen.popitem(last=False)
        try:
            self._queue.put_nowait(event)
            return True
        except queue.Full:
            return False

    def close(self, timeout: float = 2.0) -> None:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            event = self._queue.get()
            if event is None:
                return
            try:
                requests.post(f"{self.base_url}/v1/observability/events", json={"events": [event]}, timeout=self.timeout)
            except requests.RequestException:
                # Observability is strictly best effort and must never stop a job.
                pass
