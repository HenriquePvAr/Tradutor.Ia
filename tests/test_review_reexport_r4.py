from __future__ import annotations

from review_reexport import _apply_snapshot, build_review_snapshot, validate_review_snapshot


def _snapshot(region: dict) -> dict:
    return {"pages": [{"page_index": 1}], "regions": [region]}


def test_review_snapshot_freezes_effective_values_and_versions():
    job = {"id": "a" * 32, "run_id": "b" * 32}
    report = {"pages": [{"index": 1, "image_path": "page.png", "output_path": "translated.png",
                         "debug_data": {"items": []}}]}
    review = {"items": [{"type": "region", "page_index": 1, "region_id": "p001:r1",
        "source_text_effective": "corrected", "target_text_effective": "manual target",
        "translate_mode_effective": "translate", "translate_effective": "translate",
        "region_type_effective": "dialogue", "bounding_box_effective": [1, 2, 3, 4],
        "revision_version": 4, "region_control_version": 2}]}
    snapshot = build_review_snapshot(job=job, report=report, review=review)
    region = snapshot["regions"][0]
    assert snapshot["job_id"] == job["id"] and snapshot["run_id"] == job["run_id"]
    assert region["region_id"] == "r1"
    assert region["effective_source_text"] == "corrected"
    assert region["effective_target_text"] == "manual target"
    assert region["revision_version"] == 4 and region["region_control_version"] == 2
    assert snapshot["additional_yk_required"] == 0


def test_review_snapshot_blocks_stale_translated_region():
    blockers = validate_review_snapshot(_snapshot({
        "page_index": 1, "region_id": "REGION_1", "active": True,
        "translate_effective": "translate", "translation_stale": True,
        "effective_target_text": "target", "effective_bounding_box": [1, 2, 3, 4],
    }))
    assert [item["reason"] for item in blockers] == ["review_translation_stale"]


def test_ignore_does_not_require_target_and_keeps_auto_group_out_of_translation():
    snap = _snapshot({"page_index": 1, "region_id": "REGION_1", "active": True,
        "translate_effective": "ignore", "effective_translate_mode": "ignore",
        "effective_source_text": "THUMP", "effective_region_type": "sfx",
        "effective_target_text": "", "effective_bounding_box": [1, 2, 3, 4]})
    assert validate_review_snapshot(snap) == []
    page = {"debug_data": {"items": [{"region_id": "REGION_1", "text": "THUMP",
        "translation": "", "classification": "sfx", "bounding_box": [4, 5, 6, 7]}]}}
    result = _apply_snapshot(page, 1, snap)
    item = result["debug_data"]["items"][0]
    assert item["review_translate_override"] == "ignore"
    assert item["bounding_box"] == [1, 2, 3, 4]
    assert page["debug_data"]["items"][0]["bounding_box"] == [4, 5, 6, 7]


def test_target_bbox_and_type_overrides_reach_render_input():
    snap = _snapshot({"page_index": 1, "region_id": "REGION_1", "active": True,
        "effective_source_text": "I AM HERE", "effective_target_text": "EU ESTOU AQUI",
        "effective_translate_mode": "translate", "translate_effective": "translate",
        "effective_region_type": "dialogue", "effective_bounding_box": [10, 20, 30, 40]})
    source_item = {"region_id": "REGION_1", "text": "1 AM HERE", "translation": "OLÁ",
        "classification": "unknown", "bounding_box": [1, 2, 3, 4]}
    result = _apply_snapshot({"debug_data": {"items": [source_item]}}, 1, snap)
    item = result["debug_data"]["items"][0]
    assert item["text"] == "I AM HERE"
    assert item["translation"] == "EU ESTOU AQUI"
    assert item["classification"] == "dialogue"
    assert item["bounding_box"] == [10, 20, 30, 40]
    assert item["sent_to_translation"] is True
    assert source_item["translation"] == "OLÁ"


def test_manual_region_is_added_and_inactive_region_is_omitted():
    page = {"debug_data": {"items": []}}
    active = {"page_index": 1, "region_id": "manual:abc", "active": True,
        "is_manual_region": True, "effective_source_text": "THREE DAYS LATER",
        "effective_target_text": "TRÊS DIAS DEPOIS", "effective_translate_mode": "translate",
        "effective_region_type": "story", "effective_bounding_box": [5, 6, 70, 20]}
    result = _apply_snapshot(page, 1, _snapshot(active))
    assert len(result["debug_data"]["items"]) == 1
    assert result["debug_data"]["items"][0]["region_id"] == "manual:abc"
    assert result["debug_data"]["items"][0]["translation"] == "TRÊS DIAS DEPOIS"
    inactive = {**active, "active": False}
    assert _apply_snapshot(page, 1, _snapshot(inactive))["debug_data"]["items"] == []


def test_invalid_bbox_and_missing_target_are_blockers():
    blockers = validate_review_snapshot(_snapshot({"page_index": 1, "region_id": "manual:x",
        "active": True, "translate_effective": "translate", "effective_target_text": "",
        "effective_bounding_box": [-1, 0, 0, 10]}))
    assert {item["reason"] for item in blockers} == {"review_target_required", "review_bbox_invalid"}
