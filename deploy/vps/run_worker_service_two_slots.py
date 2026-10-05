"""Two worker threads, two owned pages/chats, one Chrome process."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from research.gemini_vps_web_worker.worker import Worker
from multi_slot_manager import MultiSlotManager
from two_slot_browser import TwoSlotGeminiProvider
from two_slot_config import MAX_BROWSER_SLOTS, MAX_CONCURRENCY


root = Path(os.environ["YOMU_WORKER_ROOT"])
if MAX_BROWSER_SLOTS != MAX_CONCURRENCY or MAX_CONCURRENCY != 2:
    raise RuntimeError("this experiment requires exactly two browser slots and concurrency")
slots = MultiSlotManager(root / ".runtime" / "chat-slots-v2", max_slots=MAX_BROWSER_SLOTS)


def loop():
    provider = TwoSlotGeminiProvider(root / "browser-profile-chrome", slots)
    provider.attach_cdp("http://127.0.0.1:9222")
    worker = Worker(root, provider)
    while True:
        worker.run_once()
        time.sleep(0.1)


threads = [threading.Thread(target=loop, name=f"yomu-slot-{i}", daemon=False) for i in range(2)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
