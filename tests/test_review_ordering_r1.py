"""R1 — Review ordering + page identity + bbox contract (backend).

Covers the tester feedback root cause: pendings appeared out of order, the page
identity was ambiguous, and the region bbox was absent from the DTO. Ordering is
physical (page_index → bbox.y → bbox.x → region_id), category never controls order,
Smart Split is no longer forced to the end, and the preview route resolves to an
explicit state instead of silently falling back to another page.
"""
from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import os
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import ui_bridge
from job_store import JobStatus


def _bridge(tmp_path: Path) -> ui_bridge.UiBridge:
    patches = (
        patch.dict(os.environ, {"TRADUTOR_TEST_RUNTIME_ROOT": str(tmp_path)}),
        patch.object(ui_bridge, "env_status", lambda *a, **k: {
            "env_exists": True, "nvidia_configured": True,
        }),
        patch.object(ui_bridge, "_current_commit", lambda: "offline"),
        patch.object(ui_bridge, "_current_branch", lambda: "test"),
    )
    for item in patches:
        item.start()
    bridge = ui_bridge.UiBridge()
    bridge._test_patches = patches
    return bridge


def _review_job(bridge: ui_bridge.UiBridge, output_dir: Path,
                status: str = JobStatus.REVIEW_REQUIRED) -> str:
    output_dir.mkdir(parents=True, exist_ok=True)
    job_id = bridge.store.create_job(
        source_url="https://example.invalid/synthetic",
        output_dir=str(output_dir),
        command=["python", "fake_pipeline.py"],
        configuration={"job_type": "translation", "community_owner_id": "owner-a",
                       "local_test_only": True},
        initial_status=JobStatus.QUEUED,
    )
    bridge.store.claim_next_job("fixture-worker", 1)
    bridge.store.transition(job_id, JobStatus.STARTING, expected_worker="fixture-worker")
    bridge.store.transition(job_id, JobStatus.RUNNING, expected_worker="fixture-worker")
    if status != JobStatus.RUNNING:
        bridge.store.transition(job_id, status, expected_worker="fixture-worker")
    return job_id


def _terminal_item(item_id, region_id, box, **extra):
    base = {"id": item_id, "region_id": region_id, "manual_review_required": True,
            "text": f"src {item_id}", "translation": f"pt {item_id}"}
    if box is not None:
        base["bounding_box"] = box
    base.update(extra)
    return base


def _close(bridge: ui_bridge.UiBridge) -> None:
    bridge.store.close()
    for item in getattr(bridge, "_test_patches", ()):
        item.stop()


def _pages_of(items):
    return [it["page_index"] for it in items]


def test_pages_are_ordered_numerically_not_by_report_order(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    try:
        job_id = _review_job(bridge, tmp_path / "out")
        report = {"pages": [
            {"index": 10, "output_path": "p10.png",
             "translation_terminal_items": [_terminal_item("B10", "R10", [1, 1, 5, 5])]},
            {"index": 2, "output_path": "p2.png",
             "translation_terminal_items": [_terminal_item("B2", "R2", [1, 1, 5, 5])]},
            {"index": 1, "output_path": "p1.png",
             "translation_terminal_items": [_terminal_item("B1", "R1", [1, 1, 5, 5])]},
        ]}
        monkeypatch.setattr(bridge, "_quality_report_data", lambda job: report)
        review = bridge.quality_review(job_id)
        assert _pages_of(review["items"]) == [1, 2, 10]
    finally:
        _close(bridge)


def test_same_page_orders_by_y_then_x(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    try:
        job_id = _review_job(bridge, tmp_path / "out")
        report = {"pages": [{"index": 1, "output_path": "p1.png",
            "translation_terminal_items": [
                _terminal_item("A", "RA", [500, 900, 10, 10]),
                _terminal_item("B", "RB", [100, 100, 10, 10]),
                _terminal_item("C", "RC", [300, 100, 10, 10]),
            ]}]}
        monkeypatch.setattr(bridge, "_quality_report_data", lambda job: report)
        review = bridge.quality_review(job_id)
        order = [(it["bounding_box"][1], it["bounding_box"][0]) for it in review["items"]]
        assert order == [(100, 100), (100, 300), (900, 500)]
    finally:
        _close(bridge)


def test_category_does_not_control_order(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    try:
        job_id = _review_job(bridge, tmp_path / "out")
        # One page, four different categories, deliberately out of physical order.
        report = {"pages": [{"index": 1, "output_path": "p1.png",
            "translation_terminal_items": [_terminal_item("T", "RT", [0, 500, 10, 10])],
            "text_overflow_items": [_terminal_item("O", "RO", [0, 50, 10, 10],
                                                   manual_review_required=False,
                                                   text_overflow_ratio=1.5)],
            "visual_validation_failures": [_terminal_item("V", "RV", [0, 300, 10, 10],
                manual_review_required=False,
                visual_validation={"visual_validation_passed": False})],
            "suspicious_groups": [_terminal_item("S", "RS", [0, 200, 10, 10],
                                                 manual_review_required=False,
                                                 quality_reasons=["semantic"])],
        }]}
        monkeypatch.setattr(bridge, "_quality_report_data", lambda job: report)
        review = bridge.quality_review(job_id)
        ys = [it["bounding_box"][1] for it in review["items"]]
        assert ys == [50, 200, 300, 500]
    finally:
        _close(bridge)


def test_smart_split_joins_physical_order_not_forced_last(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    try:
        job_id = _review_job(bridge, tmp_path / "out")
        # Region item on page 2 (boxed) + a smart split on page 1 (no box). The split
        # must land on page 1, before the page 2 region — not appended at the end.
        report = {
            "pages": [
                {"index": 1, "output_path": "p1.png",
                 "translation_terminal_items": [_terminal_item("A", "RA", [0, 400, 10, 10])]},
                {"index": 2, "output_path": "p2.png",
                 "translation_terminal_items": [_terminal_item("B", "RB", [0, 100, 10, 10])]},
            ],
            "summary": {"quality_validation": {"smart_split_details": [
                {"page": 1, "requires_review": True, "reason": "no_gutter"},
            ]}},
        }
        monkeypatch.setattr(bridge, "_quality_report_data", lambda job: report)
        review = bridge.quality_review(job_id)
        pages = _pages_of(review["items"])
        types = [it["type"] for it in review["items"]]
        assert pages == [1, 1, 2]
        # On page 1 the boxed region comes first, then the boxless smart split.
        assert types == ["region", "smart_split", "region"]
    finally:
        _close(bridge)


def test_page_identity_prefers_index_over_sequence_index(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    try:
        job_id = _review_job(bridge, tmp_path / "out")
        report = {"pages": [{"index": 7, "sequence_index": 3, "output_path": "p7.png",
            "translation_terminal_items": [_terminal_item("A", "RA", [1, 1, 5, 5])]}]}
        monkeypatch.setattr(bridge, "_quality_report_data", lambda job: report)
        review = bridge.quality_review(job_id)
        item = review["items"][0]
        assert item["page_index"] == 7
        assert item["sequence_index"] == 3
        assert item["page_url"].endswith("/page/7")
    finally:
        _close(bridge)


def test_index_zero_is_not_lost_to_sequence_index(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    try:
        job_id = _review_job(bridge, tmp_path / "out")
        report = {"pages": [{"index": 0, "sequence_index": 9, "output_path": "p0.png",
            "translation_terminal_items": [_terminal_item("A", "RA", [1, 1, 5, 5])]}]}
        monkeypatch.setattr(bridge, "_quality_report_data", lambda job: report)
        review = bridge.quality_review(job_id)
        assert review["items"][0]["page_index"] == 0
    finally:
        _close(bridge)


def test_bounding_box_is_in_dto_and_validated(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    try:
        job_id = _review_job(bridge, tmp_path / "out")
        report = {"pages": [{"index": 1, "output_path": "p1.png",
            "translation_terminal_items": [
                _terminal_item("Good", "RG", [12, 34, 56, 78]),
                _terminal_item("BadNeg", "RN", [-1, 5, 10, 10]),
                _terminal_item("BadZero", "RZ", [0, 0, 0, 10]),
                _terminal_item("Missing", "RM", None),
            ]}]}
        monkeypatch.setattr(bridge, "_quality_report_data", lambda job: report)
        review = bridge.quality_review(job_id)
        by_region = {it["region_id"].split(":")[-1]: it for it in review["items"]}
        assert by_region["RG"]["bounding_box"] == [12, 34, 56, 78]
        assert by_region["RG"]["bounding_box_valid"] is True
        for bad in ("RN", "RZ", "RM"):
            assert by_region[bad]["bounding_box"] is None
            assert by_region[bad]["bounding_box_valid"] is False
    finally:
        _close(bridge)


def test_preview_resolution_states(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    try:
        out = tmp_path / "out"
        job_id = _review_job(bridge, out)
        ready_png = out / "p1.png"
        Image.new("RGB", (120, 200), "white").save(ready_png)
        outside = tmp_path / "escape.png"
        Image.new("RGB", (120, 200), "white").save(outside)
        report = {"pages": [
            {"index": 1, "output_path": str(ready_png)},
            {"index": 2, "output_path": str(out / "missing.png")},
            {"index": 3, "output_path": str(outside)},
        ]}
        monkeypatch.setattr(bridge, "_quality_report_data", lambda job: report)

        assert bridge.quality_review_page_resolution(job_id, 1)["state"] == "ready"
        # No page 99 in the report -> explicit not_found, never a silent other page.
        assert bridge.quality_review_page_resolution(job_id, 99)["state"] == "not_found"
        # Page exists in the report but its file is gone; job is terminal -> unavailable.
        assert bridge.quality_review_page_resolution(job_id, 2)["state"] == "unavailable"
        # Path escapes the output dir -> denied.
        assert bridge.quality_review_page_resolution(job_id, 3)["state"] == "denied"
    finally:
        _close(bridge)


def test_preview_missing_file_while_running_is_processing(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    try:
        out = tmp_path / "out"
        job_id = _review_job(bridge, out, status=JobStatus.RUNNING)
        report = {"pages": [{"index": 1, "output_path": str(out / "missing.png")}]}
        monkeypatch.setattr(bridge, "_quality_report_data", lambda job: report)
        # Running job, page file not written yet -> processing, not unavailable.
        assert bridge.quality_review_page_resolution(job_id, 1)["state"] == "processing"
    finally:
        _close(bridge)
