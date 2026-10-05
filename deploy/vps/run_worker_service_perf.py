"""Controlled performance worker; default remains one active slot."""
from __future__ import annotations

import json
import os
import signal
import threading
import time
from pathlib import Path

from multi_slot_manager import MultiSlotManager
from perf_worker import PerformanceWorker
from resource_sampler import ResourceSampler
from two_slot_browser import TwoSlotGeminiProvider


root = Path(os.environ["YOMU_WORKER_ROOT"])
configured = int(os.environ.get("YOMU_MAX_CONCURRENCY", "1"))
if configured not in (1, 2):
    raise RuntimeError("performance experiment supports only MAX_CONCURRENCY=1 or 2")
runtime = root / ".runtime"
(runtime / "perf").mkdir(parents=True, exist_ok=True)
(runtime / "perf-config.json").write_text(json.dumps({
    "configured_max_concurrency": configured,
    "effective_max_concurrency": configured,
    "mode": f"C{configured}",
}, sort_keys=True), encoding="utf-8")
slots = MultiSlotManager(runtime / "chat-slots-v2", max_slots=2)
sampler = ResourceSampler(runtime / "perf" / f"samples-{os.getpid()}.jsonl", interval_seconds=2.0).start()
stop = threading.Event()


def shutdown(*_args):
    stop.set()
    summary = sampler.stop()
    (runtime / "perf" / f"summary-{os.getpid()}.json").write_text(json.dumps(summary, sort_keys=True), encoding="utf-8")


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)


def loop():
    provider = TwoSlotGeminiProvider(root / "browser-profile-chrome", slots)
    provider.attach_cdp("http://127.0.0.1:9222")
    worker = PerformanceWorker(root, provider)
    while not stop.is_set():
        if not worker.run_once():
            stop.wait(0.1)


threads = [threading.Thread(target=loop, name=f"yomu-perf-slot-{i}", daemon=False) for i in range(configured)]
for thread in threads:
    thread.start()
while not stop.is_set():
    time.sleep(0.5)
for thread in threads:
    thread.join(timeout=30)
