"""Single-slot ownership and persistent Gemini chat metadata."""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path


SLOT_STATES = frozenset({"IDLE", "ACQUIRED", "UPLOADING", "PROMPTING", "GENERATING", "RECOVERING", "VALIDATING", "RELEASING", "NEEDS_ROTATION", "BROKEN"})


@dataclass
class BrowserSlot:
    slot_id: str = "slot-1"
    browser_id: str = "browser-1"
    page_id: str = ""
    chat_id: str = ""
    chat_url: str = ""
    state: str = "IDLE"
    current_job_id: str = ""
    current_request_id: str = ""
    generation_seq: int = 0
    last_used_at: float = 0.0
    created_at: float = 0.0
    rotation_count: int = 0


class SlotBusy(RuntimeError):
    pass


class SlotManager:
    def __init__(self, root: str | Path):
        self.path = Path(root) / "slot-1.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.slot = self._load()

    def _load(self) -> BrowserSlot:
        if not self.path.exists():
            return BrowserSlot(created_at=time.time())
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return BrowserSlot(**{k: data.get(k, v) for k, v in asdict(BrowserSlot()).items()})
        except (OSError, TypeError, ValueError):
            return BrowserSlot(created_at=time.time(), state="RECOVERY_REQUIRED")

    def save(self) -> None:
        with self._lock:
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(asdict(self.slot), sort_keys=True), encoding="utf-8")
            temp.replace(self.path)

    def acquire(self, job_id: str, request_id: str) -> BrowserSlot:
        with self._lock:
            if self.slot.current_job_id and self.slot.current_job_id != job_id:
                raise SlotBusy("slot-1 is owned by another job")
            if self.slot.state in {"BROKEN", "NEEDS_ROTATION", "RECOVERY_REQUIRED"}:
                raise SlotBusy(f"slot-1 unavailable: {self.slot.state}")
            self.slot.current_job_id = str(job_id)
            self.slot.current_request_id = str(request_id)
            self.slot.state = "ACQUIRED"
            self.slot.last_used_at = time.time()
            self.save()
            return self.slot

    def transition(self, state: str) -> None:
        if state not in SLOT_STATES:
            raise ValueError(f"invalid_slot_state:{state}")
        with self._lock:
            self.slot.state = state
            self.save()

    def release(self) -> None:
        with self._lock:
            self.slot.state = "IDLE"
            self.slot.current_job_id = ""
            self.slot.current_request_id = ""
            self.slot.last_used_at = time.time()
            self.save()

    def observe_chat(self, url: str, *, page_id: str = "") -> str:
        match = re.search(r"/app/([A-Za-z0-9_-]+)", str(url))
        chat_id = match.group(1) if match else ""
        with self._lock:
            if chat_id:
                self.slot.chat_id = chat_id
                self.slot.chat_url = str(url)
            if page_id:
                self.slot.page_id = page_id
            self.save()
        return chat_id

    def next_generation(self) -> int:
        with self._lock:
            self.slot.generation_seq += 1
            self.save()
            return self.slot.generation_seq

    def mark_rotation(self, reason: str) -> None:
        with self._lock:
            self.slot.state = "NEEDS_ROTATION"
            self.slot.rotation_count += 1
            self.save()

    def snapshot(self) -> dict:
        with self._lock:
            data = asdict(self.slot)
            data["chat_url"] = ""  # URL is operational state, never observability output.
            return data
