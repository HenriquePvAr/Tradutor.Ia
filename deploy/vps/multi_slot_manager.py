"""Atomic persistent ownership for exactly two Gemini browser slots."""
from __future__ import annotations

import json
import hashlib
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path


SLOT_STATES = frozenset({"IDLE", "ACQUIRED", "UPLOADING", "PROMPTING", "GENERATING", "RECOVERING", "VALIDATING", "RELEASING", "NEEDS_ROTATION", "BROKEN"})


@dataclass
class Slot:
    slot_id: str
    browser_id: str = "browser-1"
    page_id: str = ""
    chat_id: str = ""
    chat_url: str = ""
    state: str = "IDLE"
    current_job_id: str = ""
    current_request_id: str = ""
    generation_seq: int = 0
    rotation_count: int = 0
    created_at: float = 0.0
    last_used_at: float = 0.0
    healthy: bool = True


class NoFreeSlot(RuntimeError):
    pass


class MultiSlotManager:
    def __init__(self, root: str | Path, max_slots: int = 2):
        if max_slots != 2:
            raise ValueError("this mission supports exactly two slots")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "slots-v2.json"
        self.max_slots = max_slots
        self.lock = threading.RLock()
        self.slots: dict[str, Slot] = self._load()

    def _load(self) -> dict[str, Slot]:
        slots = {f"slot-{i}": Slot(slot_id=f"slot-{i}", created_at=time.time()) for i in range(1, self.max_slots + 1)}
        old = self.root.parent / "chat-slots" / "slot-1.json"
        if old.exists() and not self.path.exists():
            try:
                data = json.loads(old.read_text(encoding="utf-8"))
                slots["slot-1"].chat_id = str(data.get("chat_id") or "")
                slots["slot-1"].chat_url = str(data.get("chat_url") or "")
                slots["slot-1"].generation_seq = int(data.get("generation_seq") or 0)
                slots["slot-1"].rotation_count = int(data.get("rotation_count") or 0)
            except (OSError, TypeError, ValueError):
                slots["slot-1"].healthy = False
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                for slot_id, defaults in slots.items():
                    slots[slot_id] = Slot(**{key: data.get(slot_id, {}).get(key, value) for key, value in asdict(defaults).items()})
            except (OSError, TypeError, ValueError):
                slots["slot-1"].healthy = False
        return slots

    def save(self) -> None:
        temp = self.path.with_suffix(".tmp")
        data = {slot_id: asdict(slot) for slot_id, slot in self.slots.items()}
        temp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        temp.replace(self.path)

    def acquire(self, job_id: str, request_id: str) -> Slot:
        with self.lock:
            for slot in self.slots.values():
                if slot.healthy and slot.state == "IDLE" and not slot.current_job_id:
                    slot.state = "ACQUIRED"
                    slot.current_job_id = str(job_id)
                    slot.current_request_id = str(request_id)
                    slot.last_used_at = time.time()
                    self.save()
                    return slot
            raise NoFreeSlot("all slots are busy or unhealthy")

    def transition(self, slot_id: str, state: str) -> None:
        if state not in SLOT_STATES:
            raise ValueError(f"invalid_slot_state:{state}")
        with self.lock:
            self.slots[slot_id].state = state
            self.save()

    def release(self, slot_id: str, state: str = "IDLE") -> None:
        if state not in SLOT_STATES:
            raise ValueError(f"invalid_slot_state:{state}")
        with self.lock:
            slot = self.slots[slot_id]
            slot.state = state
            slot.current_job_id = ""
            slot.current_request_id = ""
            slot.last_used_at = time.time()
            self.save()

    def mark_broken(self, slot_id: str, reason: str = "") -> None:
        with self.lock:
            slot = self.slots[slot_id]
            slot.healthy = False
            slot.state = "BROKEN"
            slot.current_job_id = ""
            slot.current_request_id = ""
            self.save()

    def register_page(self, slot_id: str, page_id: str) -> None:
        with self.lock:
            self.slots[slot_id].page_id = str(page_id)
            self.save()

    def observe_chat(self, slot_id: str, url: str) -> str:
        match = re.search(r"/app/([A-Za-z0-9_-]+)", str(url))
        chat_id = match.group(1) if match else ""
        with self.lock:
            if chat_id:
                self.slots[slot_id].chat_id = chat_id
                self.slots[slot_id].chat_url = str(url)
            self.save()
        return chat_id

    def next_generation(self, slot_id: str) -> int:
        with self.lock:
            self.slots[slot_id].generation_seq += 1
            self.save()
            return self.slots[slot_id].generation_seq

    def mark_rotation(self, slot_id: str, reason: str) -> None:
        with self.lock:
            self.slots[slot_id].state = "NEEDS_ROTATION"
            self.slots[slot_id].rotation_count += 1
            self.save()

    def snapshot(self) -> dict:
        with self.lock:
            result = {}
            for slot_id, slot in self.slots.items():
                value = asdict(slot)
                value["chat_url"] = ""
                result[slot_id] = value
            return result

    def metrics(self, browser_count: int = 0, queue_depth: int = 0) -> dict:
        snap = self.snapshot()
        for value in snap.values():
            chat_id = value.pop("chat_id", "")
            value["chat_fingerprint"] = hashlib.sha256(chat_id.encode()).hexdigest()[:16] if chat_id else ""
        return {
            "browser_count": browser_count,
            "slot_count": self.max_slots,
            "healthy_slots": sum(1 for slot in snap.values() if slot["healthy"]),
            "active_slots": sum(1 for slot in snap.values() if slot["current_job_id"]),
            "max_concurrency": self.max_slots,
            "queue_depth": queue_depth,
            "chat_reuse_enabled": True,
            "chat_rotation_count": sum(int(slot["rotation_count"]) for slot in snap.values()),
            "slots": snap,
        }
