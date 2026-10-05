"""Safe operational metrics for the one-browser/one-slot Gemini worker."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

try:
    from .slot_manager import SlotManager
except ImportError:  # runtime copy on the VPS worker root
    from slot_manager import SlotManager


def snapshot(root: str | Path) -> dict:
    root = Path(root)
    slot = SlotManager(root / ".runtime" / "chat-slots")
    try:
        with urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=1) as response:
            browser_count = sum(1 for item in json.load(response) if item.get("type") == "page")
    except Exception:
        browser_count = 0
    pending = root / "queue" / "pending"
    queue_depth = sum(1 for item in pending.iterdir() if item.is_file()) if pending.exists() else 0
    state = slot.snapshot()
    return {
        "browser_count": browser_count,
        "slot_count": 1,
        "active_slots": 0 if not state.get("current_job_id") else 1,
        "max_concurrency": 1,
        "queue_depth": queue_depth,
        "chat_reuse_enabled": True,
        "chat_rotation_count": int(state.get("rotation_count", 0) or 0),
        "chat_generation_seq": int(state.get("generation_seq", 0) or 0),
        "slot_state": state.get("state", "UNKNOWN"),
    }
