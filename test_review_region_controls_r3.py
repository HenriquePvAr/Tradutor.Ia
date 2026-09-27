"""R3 — durable manual region controls, with no OCR/provider/render execution."""
from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from PIL import Image
import pytest

from job_store import JobStore, TransitionError
from test_review_ordering_r1 import _bridge, _review_job, _close


def _fixture(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    output = tmp_path / "out"
    output.mkdir(parents=True, exist_ok=True)
    page_path = output / "p1.png"
    Image.new("RGB", (100, 120), "white").save(page_path)
    job_id = _review_job(bridge, output)
    report = {"pages": [{"index": 1, "output_path": str(page_path),
        "translation_terminal_items": [{"id": "B1", "region_id": "R1",
            "manual_review_required": True, "classification": "speech",
            "text": "THUMP", "translation": "TUM", "bounding_box": [10, 30, 20, 15]}]}]}
    monkeypatch.setattr(bridge, "_quality_report_data", lambda job: report)
    return bridge, job_id


def _item(bridge, job_id, key="p1:iB1"):
    return next(item for item in bridge.quality_review(job_id)["items"] if item["key"] == key)


def test_translate_ignore_auto_type_bbox_and_conflict(tmp_path, monkeypatch):
    bridge, job_id = _fixture(tmp_path, monkeypatch)
    try:
        original = _item(bridge, job_id)
        result = bridge.save_quality_review_region_control(
            job_id, original["key"], expected_version=0, actor_id="tester",
            translate_override="ignore", region_type_override="dialogue",
            bounding_box_override=[5, 6, 40, 25])
        updated = next(item for item in result["review"]["items"] if item["key"] == original["key"])
        assert updated["translate_mode_effective"] == "ignore"
        assert updated["translate_effective"] == "ignore"
        assert updated["region_type_original"] == "speech"
        assert updated["region_type_effective"] == "dialogue"
        assert updated["bounding_box_original"] == [10, 30, 20, 15]
        assert updated["bounding_box_effective"] == [5, 6, 40, 25]
        assert updated["region_control_version"] == 1
        with pytest.raises(ValueError, match="review_version_conflict"):
            bridge.save_quality_review_region_control(
                job_id, original["key"], expected_version=0, actor_id="tester",
                translate_override="translate", region_type_override=None,
                bounding_box_override=None)
        reset = bridge.save_quality_review_region_control(
            job_id, original["key"], expected_version=1, actor_id="tester",
            translate_override="auto", region_type_override=None, bounding_box_override=None)
        final = next(item for item in reset["review"]["items"] if item["key"] == original["key"])
        assert final["translate_mode_effective"] == "auto"
        assert final["region_type_effective"] == "speech"
        assert final["bounding_box_effective"] == [10, 30, 20, 15]
    finally:
        _close(bridge)


@pytest.mark.parametrize("box", [[-1, 0, 1, 1], [0, 0, 0, 4], [0, 0, 5, 0],
                                  [99, 0, 2, 2], [0, 119, 2, 2], [float("nan"), 1, 2, 2]])
def test_invalid_override_bbox_rejected(tmp_path, monkeypatch, box):
    bridge, job_id = _fixture(tmp_path, monkeypatch)
    try:
        with pytest.raises(ValueError, match="invalid_bbox"):
            bridge.save_quality_review_region_control(
                job_id, "p1:iB1", expected_version=0, actor_id="tester",
                translate_override="auto", region_type_override=None,
                bounding_box_override=box)
    finally:
        _close(bridge)


def test_manual_region_is_stable_sorted_editable_and_soft_deleted(tmp_path, monkeypatch):
    bridge, job_id = _fixture(tmp_path, monkeypatch)
    try:
        created = bridge.add_quality_review_manual_region(
            job_id, page_index=1, box=[10, 5, 30, 20], source_text="THREE DAYS LATER",
            target_text="TRÊS DIAS DEPOIS", region_type="narration",
            translate_override="translate", actor_id="tester")
        region_id = created["region"]["region_id"]
        item = next(entry for entry in created["review"]["items"] if entry["region_id"] == region_id)
        assert item["is_manual_region"] is True
        assert item["manual_region_id"] == region_id
        assert item["source_text_original"] == "THREE DAYS LATER"
        assert item["translate_mode_effective"] == "translate"
        assert [entry["region_id"] for entry in created["review"]["items"]] == [region_id, "p001:R1"]

        # Simulate a process/store restart: the Review must reconstruct the row
        # from SQLite rather than an in-memory list.
        db_path = bridge.store.db_path
        bridge.store.close()
        bridge.store = JobStore(db_path)
        reopened_item = _item(bridge, job_id, item["key"])
        assert reopened_item["region_id"] == region_id
        assert reopened_item["source_text_original"] == "THREE DAYS LATER"

        bridge.quality_review_edit(job_id, item["key"], expected_version=0, action="edited",
                                   translation="TRÊS DIAS DEPOIS", reason="", actor_id="tester",
                                   source_text="THREE DAYS LATER")
        edited = _item(bridge, job_id, item["key"])
        assert edited["target_text_effective"] == "TRÊS DIAS DEPOIS"
        db_path = bridge.store.db_path
        bridge.store.close()
        bridge.store = JobStore(db_path)
        reopened_edited = _item(bridge, job_id, item["key"])
        assert reopened_edited["source_text_effective"] == "THREE DAYS LATER"
        assert reopened_edited["target_text_effective"] == "TRÊS DIAS DEPOIS"

        with pytest.raises((TransitionError, ValueError), match="manual_region_not_found"):
            bridge.remove_quality_review_manual_region(
                job_id, "p001:R1", expected_version=0, actor_id="tester")

        removed = bridge.remove_quality_review_manual_region(
            job_id, region_id, expected_version=1, actor_id="tester")
        assert all(entry["region_id"] != region_id for entry in removed["review"]["items"])
        row = bridge.store._conn.execute(
            "SELECT * FROM quality_review_region_overrides WHERE job_id=? AND region_id=?",
            (job_id, region_id),
        ).fetchone()
        assert row is not None
        assert row["active"] == 0
    finally:
        _close(bridge)


def test_manual_region_requires_source_and_real_region_type(tmp_path, monkeypatch):
    bridge, job_id = _fixture(tmp_path, monkeypatch)
    try:
        with pytest.raises(ValueError, match="manual_region_source_required"):
            bridge.add_quality_review_manual_region(job_id, page_index=1, box=[1, 1, 8, 8],
                source_text="", actor_id="tester")
        with pytest.raises(ValueError, match="invalid_region_type"):
            bridge.add_quality_review_manual_region(job_id, page_index=1, box=[1, 1, 8, 8],
                source_text="A", region_type="made_up", actor_id="tester")
    finally:
        _close(bridge)


def test_manual_text_recovers_missed_balloon_and_outside_balloon_copy(tmp_path, monkeypatch):
    bridge, job_id = _fixture(tmp_path, monkeypatch)
    try:
        for box, source, target, kind in (
            ([4, 4, 25, 18], "I'M HERE.", "ESTOU AQUI.", "speech"),
            ([8, 70, 70, 12], "THREE DAYS LATER", "TRÊS DIAS DEPOIS", "narration"),
        ):
            bridge.add_quality_review_manual_region(
                job_id, page_index=1, box=box, source_text=source,
                target_text=target, region_type=kind, translate_override="translate",
                actor_id="tester")
        items = bridge.quality_review(job_id)["items"]
        manual = [item for item in items if item["is_manual_region"]]
        assert [item["source_text_original"] for item in manual] == ["I'M HERE.", "THREE DAYS LATER"]
        assert all(item["translate_effective"] == "translate" for item in manual)
    finally:
        _close(bridge)


def test_manual_ignore_sfx_and_force_translate_preserve_original_classification(tmp_path, monkeypatch):
    bridge, job_id = _fixture(tmp_path, monkeypatch)
    try:
        report = {"pages": [{"index": 1, "output_path": str(tmp_path / "out" / "p1.png"),
            "translation_terminal_items": [
                {"id":"SFX1", "region_id":"SFX1", "manual_review_required":True,
                 "classification":"sfx", "text":"THUMP", "translation":"TUM",
                 "bounding_box":[10, 30, 20, 15]},
                {"id":"CREDIT1", "region_id":"CREDIT1", "manual_review_required":True,
                 "classification":"credit", "text":"TEAM", "translation":"TEAM",
                 "preserved_original":True, "bounding_box":[10, 60, 30, 12]},
                {"id":"PRES1", "region_id":"PRES1", "manual_review_required":True,
                 "classification":"speech", "text":"HELLO", "translation":"HELLO",
                 "preserved_original":True, "bounding_box":[10, 80, 30, 12]},
            ]}]}
        monkeypatch.setattr(bridge, "_quality_report_data", lambda job: report)
        sfx = next(item for item in bridge.quality_review(job_id)["items"] if item["key"] == "p1:iSFX1")
        ignored = bridge.save_quality_review_region_control(
            job_id, sfx["key"], expected_version=0, actor_id="tester",
            translate_override="ignore", region_type_override=None, bounding_box_override=None)
        sfx_after = next(item for item in ignored["review"]["items"] if item["key"] == sfx["key"])
        assert sfx_after["region_type_original"] == "sfx"
        assert sfx_after["translate_effective"] == "ignore"

        credit = next(item for item in ignored["review"]["items"] if item["key"] == "p1:iCREDIT1")
        forced = bridge.save_quality_review_region_control(
            job_id, credit["key"], expected_version=0, actor_id="tester",
            translate_override="translate", region_type_override=None, bounding_box_override=None)
        credit_after = next(item for item in forced["review"]["items"] if item["key"] == credit["key"])
        assert credit_after["region_type_original"] == "credit"
        assert credit_after["translate_original"] == "ignore"
        assert credit_after["translate_effective"] == "translate"

        preserved = next(item for item in forced["review"]["items"] if item["key"] == "p1:iPRES1")
        assert preserved["translate_original"] == "ignore"
        assert preserved["translate_effective"] == "ignore"
    finally:
        _close(bridge)


def test_concurrent_saves_with_same_version_conflict(tmp_path, monkeypatch):
    bridge, job_id = _fixture(tmp_path, monkeypatch)
    other = None
    try:
        item = _item(bridge, job_id)
        run_id = bridge.store.get_job(job_id)["run_id"]
        other = JobStore(bridge.store.db_path)
        gate = Barrier(2)

        def save(store, mode):
            gate.wait()
            try:
                store.save_review_region_override(
                    job_id=job_id, run_id=run_id, page_index=1, region_id=item["region_id"],
                    expected_version=0, actor_id="tester", source_text_original=item["source_text_original"],
                    target_text_original=item["target_text_original"], region_type_original="speech",
                    bbox_original=item["bounding_box_original"], translate_override=mode,
                    is_manual=False,
                )
                return "saved"
            except TransitionError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda args: save(*args), [(bridge.store, "ignore"), (other, "translate")]))
        assert sorted(results) == ["review_version_conflict", "saved"]
    finally:
        if other:
            other.close()
        _close(bridge)
