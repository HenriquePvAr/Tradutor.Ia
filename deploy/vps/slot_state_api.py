"""Redacted two-slot health metrics; never returns chat URLs or credentials."""
from __future__ import annotations

import json
import urllib.request
import json
from pathlib import Path

try:
    from .multi_slot_manager import MultiSlotManager
except ImportError:
    from multi_slot_manager import MultiSlotManager


def snapshot(root: str | Path) -> dict:
    root = Path(root)
    manager = MultiSlotManager(root / ".runtime" / "chat-slots-v2", max_slots=2)
    try:
        with urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=1) as response:
            browser_pages = [item for item in json.load(response) if item.get("type") == "page"]
            browser_count = 1 if browser_pages else 0
    except Exception:
        browser_count = 0
    pending = root / "queue" / "pending"
    queue_depth = sum(1 for item in pending.iterdir() if item.is_file()) if pending.exists() else 0
    result = manager.metrics(browser_count=browser_count, queue_depth=queue_depth)
    config_path = root / ".runtime" / "perf-config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {"configured_max_concurrency": 1, "effective_max_concurrency": 1}
    result["configured_max_concurrency"] = int(config.get("configured_max_concurrency", 1))
    result["effective_max_concurrency"] = int(config.get("effective_max_concurrency", 1))
    result["max_concurrency"] = result["effective_max_concurrency"]
    return result
