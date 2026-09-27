"""R2 — persistent human source (OCR) + translation overrides in Quality Review.

Proves a reviewer can correct the detected source text and the translation of an
existing region, that the values persist across a fresh Review fetch and a fully
reopened store, that originals stay auditable, that optimistic versioning rejects a
stale write, and that none of this calls the provider or spends YK (guaranteed by
the offline network guard — any provider/network call would raise).
"""
from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import pytest

import ui_bridge
from job_store import JobStatus
from test_review_ordering_r1 import _bridge, _review_job, _close


REPORT = {"pages": [{"index": 1, "output_path": "p1.png", "translation_terminal_items": [
    {"id": "B1", "region_id": "R1", "manual_review_required": True,
     "text": "1 AM HERE", "translation": "1 ESTOU AQUI", "bounding_box": [10, 20, 30, 40]},
]}]}
KEY = "p1:iB1"


def _seed(bridge, tmp_path, monkeypatch):
    job_id = _review_job(bridge, tmp_path / "out")
    monkeypatch.setattr(bridge, "_quality_report_data", lambda job: REPORT)
    return job_id


def _item(bridge, job_id):
    review = bridge.quality_review(job_id)
    return next(it for it in review["items"] if it["key"] == KEY)


def test_save_source_override_persists_and_marks_translation_stale(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    try:
        job_id = _seed(bridge, tmp_path, monkeypatch)
        bridge.quality_review_edit(job_id, KEY, expected_version=0, action="edited",
                                   translation="1 ESTOU AQUI", reason="", actor_id="u1",
                                   source_text="I AM HERE")
        it = _item(bridge, job_id)
        assert it["source_text_original"] == "1 AM HERE"
        assert it["source_text_effective"] == "I AM HERE"
        assert it["source_text_edited"] is True
        assert it["target_text_edited"] is False
        assert it["translation_stale"] is True
        assert it["revision_version"] == 1
    finally:
        _close(bridge)


def test_save_target_override_persists(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    try:
        job_id = _seed(bridge, tmp_path, monkeypatch)
        bridge.quality_review_edit(job_id, KEY, expected_version=0, action="edited",
                                   translation="EU ESTOU AQUI", reason="", actor_id="u1",
                                   source_text="1 AM HERE")  # source unchanged
        it = _item(bridge, job_id)
        assert it["target_text_original"] == "1 ESTOU AQUI"
        assert it["target_text_effective"] == "EU ESTOU AQUI"
        assert it["target_text_edited"] is True
        assert it["source_text_edited"] is False
        assert it["translation_stale"] is False
    finally:
        _close(bridge)


def test_save_both_overrides(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    try:
        job_id = _seed(bridge, tmp_path, monkeypatch)
        bridge.quality_review_edit(job_id, KEY, expected_version=0, action="edited",
                                   translation="EU ESTOU AQUI", reason="", actor_id="u1",
                                   source_text="I AM HERE")
        it = _item(bridge, job_id)
        assert it["source_text_effective"] == "I AM HERE"
        assert it["target_text_effective"] == "EU ESTOU AQUI"
        assert it["source_text_edited"] and it["target_text_edited"]
        assert it["translation_stale"] is False  # both current
    finally:
        _close(bridge)


def test_i_to_1_manual_correction(tmp_path, monkeypatch):
    report = {"pages": [{"index": 1, "output_path": "p1.png", "translation_terminal_items": [
        {"id": "B1", "region_id": "R1", "manual_review_required": True,
         "text": "1", "translation": "1", "bounding_box": [1, 1, 5, 5]}]}]}
    bridge = _bridge(tmp_path)
    try:
        job_id = _review_job(bridge, tmp_path / "out")
        monkeypatch.setattr(bridge, "_quality_report_data", lambda job: report)
        bridge.quality_review_edit(job_id, KEY, expected_version=0, action="edited",
                                   translation="1", reason="", actor_id="u1",
                                   source_text="I")
        it = _item(bridge, job_id)
        assert it["source_text_original"] == "1"
        assert it["source_text_effective"] == "I"
        assert it["source_text_edited"] is True
    finally:
        _close(bridge)


def test_original_values_preserved_after_override(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    try:
        job_id = _seed(bridge, tmp_path, monkeypatch)
        bridge.quality_review_edit(job_id, KEY, expected_version=0, action="edited",
                                   translation="EU ESTOU AQUI", reason="", actor_id="u1",
                                   source_text="I AM HERE")
        it = _item(bridge, job_id)
        # Originals stay auditable regardless of the override layer.
        assert it["source_text_original"] == "1 AM HERE"
        assert it["target_text_original"] == "1 ESTOU AQUI"
    finally:
        _close(bridge)


def test_version_increments_on_distinct_saves(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    try:
        job_id = _seed(bridge, tmp_path, monkeypatch)
        bridge.quality_review_edit(job_id, KEY, expected_version=0, action="edited",
                                   translation="v1", reason="", actor_id="u1", source_text="I AM HERE")
        assert _item(bridge, job_id)["revision_version"] == 1
        bridge.quality_review_edit(job_id, KEY, expected_version=1, action="edited",
                                   translation="v2", reason="", actor_id="u1", source_text="I AM HERE")
        assert _item(bridge, job_id)["revision_version"] == 2
    finally:
        _close(bridge)


def test_stale_version_is_rejected(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    try:
        job_id = _seed(bridge, tmp_path, monkeypatch)
        bridge.quality_review_edit(job_id, KEY, expected_version=0, action="edited",
                                   translation="v1", reason="", actor_id="u1", source_text="I AM HERE")
        with pytest.raises(ValueError, match="review_version_conflict"):
            bridge.quality_review_edit(job_id, KEY, expected_version=0, action="edited",
                                       translation="v2", reason="", actor_id="u1", source_text="X")
    finally:
        _close(bridge)


def test_refresh_persists_overrides(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    try:
        job_id = _seed(bridge, tmp_path, monkeypatch)
        bridge.quality_review_edit(job_id, KEY, expected_version=0, action="edited",
                                   translation="EU ESTOU AQUI", reason="", actor_id="u1",
                                   source_text="I AM HERE")
        # Second, independent fetch (a refresh) reflects the persisted override.
        it = _item(bridge, job_id)
        assert it["source_text_effective"] == "I AM HERE"
        assert it["target_text_effective"] == "EU ESTOU AQUI"
    finally:
        _close(bridge)


def test_persist_after_full_store_reopen(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    job_id = _seed(bridge, tmp_path, monkeypatch)
    bridge.quality_review_edit(job_id, KEY, expected_version=0, action="edited",
                               translation="EU ESTOU AQUI", reason="", actor_id="u1",
                               source_text="I AM HERE")
    _close(bridge)  # closes store + stops patches: nothing left in memory

    reopened = _bridge(tmp_path)  # same runtime root -> same jobs.sqlite3 on disk
    try:
        monkeypatch.setattr(reopened, "_quality_report_data", lambda job: REPORT)
        it = _item(reopened, job_id)
        assert it["source_text_original"] == "1 AM HERE"
        assert it["source_text_effective"] == "I AM HERE"
        assert it["target_text_effective"] == "EU ESTOU AQUI"
        assert it["revision_version"] == 1
    finally:
        _close(reopened)


def test_source_text_column_migrates_onto_existing_store(tmp_path):
    # A store created fresh must carry the source_text column (migration v13).
    from job_store import JobStore
    store = JobStore(tmp_path / "jobs.sqlite3")
    try:
        cols = {row["name"] for row in store._conn.execute(
            "PRAGMA table_info(quality_review_item_revisions)")}
        assert "source_text" in cols
    finally:
        store.close()
