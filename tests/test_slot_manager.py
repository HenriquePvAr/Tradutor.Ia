import json

import pytest

from deploy.vps.slot_manager import SlotBusy, SlotManager


def test_slot_acquire_release_and_generation(tmp_path):
    manager = SlotManager(tmp_path)
    manager.acquire("job-a", "request-a")
    with pytest.raises(SlotBusy):
        manager.acquire("job-b", "request-b")
    assert manager.next_generation() == 1
    manager.observe_chat("https://gemini.google.com/app/chat-1", page_id="page-1")
    manager.release()
    restored = SlotManager(tmp_path)
    assert restored.slot.chat_id == "chat-1"
    assert restored.slot.generation_seq == 1
    assert restored.slot.state == "IDLE"


def test_slot_snapshot_never_exposes_chat_url(tmp_path):
    manager = SlotManager(tmp_path)
    manager.observe_chat("https://gemini.google.com/app/chat-secret", page_id="page-1")
    assert manager.snapshot()["chat_url"] == ""
    assert json.loads((tmp_path / "slot-1.json").read_text())["chat_id"] == "chat-secret"
