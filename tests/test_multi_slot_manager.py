import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from deploy.vps.multi_slot_manager import MultiSlotManager, NoFreeSlot


def test_initializes_exactly_two_slots_and_redacts_chat_url(tmp_path):
    manager = MultiSlotManager(tmp_path / "slots", max_slots=2)
    manager.observe_chat("slot-1", "https://gemini.google.com/app/chat-a")
    snapshot = manager.snapshot()
    assert set(snapshot) == {"slot-1", "slot-2"}
    assert snapshot["slot-1"]["chat_id"] == "chat-a"
    assert snapshot["slot-1"]["chat_url"] == ""


def test_atomic_acquire_assigns_each_slot_once_and_queues_third(tmp_path):
    manager = MultiSlotManager(tmp_path / "slots", max_slots=2)

    def acquire(index):
        try:
            return manager.acquire(f"job-{index}", f"request-{index}").slot_id
        except NoFreeSlot:
            return "QUEUED"

    with ThreadPoolExecutor(max_workers=3) as pool:
        assigned = list(pool.map(acquire, range(3)))
    assert sorted(item for item in assigned if item != "QUEUED") == ["slot-1", "slot-2"]
    assert assigned.count("QUEUED") == 1
    assert len({slot.current_job_id for slot in manager.slots.values()}) == 2


def test_generation_sequences_are_independent_and_persist(tmp_path):
    root = tmp_path / "slots"
    manager = MultiSlotManager(root, max_slots=2)
    assert manager.next_generation("slot-1") == 1
    assert manager.next_generation("slot-1") == 2
    assert manager.next_generation("slot-2") == 1
    reloaded = MultiSlotManager(root, max_slots=2)
    assert reloaded.slots["slot-1"].generation_seq == 2
    assert reloaded.slots["slot-2"].generation_seq == 1


def test_failure_and_rotation_do_not_affect_other_slot(tmp_path):
    manager = MultiSlotManager(tmp_path / "slots", max_slots=2)
    manager.mark_broken("slot-1", "fixture")
    manager.mark_rotation("slot-2", "fixture")
    assert manager.slots["slot-1"].healthy is False
    assert manager.slots["slot-1"].state == "BROKEN"
    assert manager.slots["slot-2"].healthy is True
    assert manager.slots["slot-2"].state == "NEEDS_ROTATION"

