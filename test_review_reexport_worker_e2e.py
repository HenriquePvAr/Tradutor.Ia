"""Process-boundary lifecycle coverage for the persisted Review re-export action."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from job_store import JobStatus, JobStore
from ui_bridge import UiBridge


def _fixture(tmp_path: Path, *, height: int = 180):
    db = tmp_path / "jobs.sqlite3"
    output = tmp_path / "output" / "source"
    output.mkdir(parents=True)
    image_path = output / "page_0001.png"
    assert cv2.imwrite(str(image_path), np.full((height, 280, 3), 245, dtype=np.uint8))
    store = JobStore(db)
    parent_id = "a" * 32
    store.create_job(
        job_id=parent_id, source_url="", output_dir=str(output), command=[],
        run_id="b" * 32,
        configuration={"community_owner_id": "owner-test", "job_type": "translation",
                       "source_type": "local_folder", "chapter_name": "R4 Fixture", "output_format": "png"},
        series_title="R4 Fixture", operation_kind="chapter",
    )
    store._conn.execute("UPDATE jobs SET status=?,finished_at=? WHERE id=?",
                        (JobStatus.FINISHED, time.time(), parent_id))
    items = [
        {"region_id": "auto-1", "text": "1 AM HERE", "translation": "OLÁ", "classification": "speech",
         "bounding_box": [12, 18, 110, 36], "draw_box": [12, 18, 110, 36],
         "translation_box": [12, 18, 122, 54], "sent_to_translation": True,
         "translation_valid": True, "translation_final_state": "translated", "preserved_original": False},
        {"region_id": "auto-sfx", "text": "THUMP", "translation": "", "classification": "sfx",
         "bounding_box": [20, 78, 70, 25], "draw_box": [20, 78, 70, 25],
         "translation_box": [20, 78, 90, 103], "sent_to_translation": False,
         "translation_valid": True, "translation_final_state": "preserved_original", "preserved_original": True},
    ]
    report = {"pages": [{"index": 1, "image_path": str(image_path), "output_path": "",
                         "debug_data": {"items": items}}]}
    review = {"job_id": parent_id, "items": [
        {"type": "region", "page_index": 1, "region_id": "auto-1", "is_manual_region": False,
         "source_text_effective": "I AM HERE", "target_text_effective": "EU ESTOU AQUI",
         "translate_mode_effective": "translate", "translate_effective": "translate",
         "region_type_effective": "dialogue", "bounding_box_effective": [30, 22, 120, 40]},
        {"type": "region", "page_index": 1, "region_id": "auto-sfx", "is_manual_region": False,
         "source_text_effective": "THUMP", "target_text_effective": "", "translate_mode_effective": "ignore",
         "translate_effective": "ignore", "region_type_effective": "sfx", "bounding_box_effective": [20, 78, 70, 25]},
        {"type": "region", "page_index": 1, "region_id": "manual:missed-balloon", "is_manual_region": True,
         "source_text_effective": "I'M HERE", "target_text_effective": "ESTOU AQUI",
         "translate_mode_effective": "translate", "translate_effective": "translate",
         "region_type_effective": "dialogue", "bounding_box_effective": [145, 25, 115, 42]},
        {"type": "region", "page_index": 1, "region_id": "manual:narration", "is_manual_region": True,
         "source_text_effective": "THREE DAYS LATER", "target_text_effective": "TRÊS DIAS DEPOIS",
         "translate_mode_effective": "translate", "translate_effective": "translate",
         "region_type_effective": "narration", "bounding_box_effective": [22, 120, 220, 34]},
    ]}
    bridge = object.__new__(UiBridge)
    bridge.store = store
    bridge.history_revision = 0
    bridge.output_root = (tmp_path / "output").resolve()
    bridge._is_translation_job = lambda _job: True
    bridge.quality_review = lambda _job_id: review
    bridge._quality_report_data = lambda _job: report
    bridge.ensure_worker = lambda: {"online": False, "started": False}
    return db, output, store, bridge, parent_id


def _run_worker(db: Path, runtime: Path, *, fault: str = "") -> int:
    code = (
        "from pathlib import Path; from worker_service import Worker; "
        f"w=Worker(Path({str(db)!r}),poll_seconds=0.01); "
        "w.run(once=True); w.close()"
    )
    env = os.environ.copy()
    env.update({"TRADUTOR_RUNTIME_ROOT": str(runtime), "APP_ENV": "test"})
    env.pop("YOMU_TEST_REVIEW_REEXPORT_FAIL_ONCE", None)
    if fault:
        env["YOMU_TEST_REVIEW_REEXPORT_FAIL_ONCE"] = fault
    proc = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parent,
                          env=env, timeout=90, check=False, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-1500:]
    return proc.returncode


def _wait_job(store: JobStore, job_id: str, expected: set[str], timeout: float = 30):
    until = time.monotonic() + timeout
    latest = None
    while time.monotonic() < until:
        latest = store.get_job(job_id)
        if latest and (latest.get("status") in expected or latest.get("status") in JobStatus.TERMINAL):
            return latest
        time.sleep(0.05)
    raise AssertionError(f"job did not settle: {latest}")


def test_bridge_queues_worker_renders_png_and_recovers_in_new_process(tmp_path: Path, monkeypatch):
    db, output, store, bridge, parent_id = _fixture(tmp_path)
    runtime = tmp_path / "worker-runtime"
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("YOMU_TEST_REVIEW_REEXPORT_FAIL_ONCE", "export")
    try:
        queued = bridge.quality_review_reexport_for_owner("owner-test", parent_id, output_format="png")
        assert queued["status"] == "queued"
        assert queued["additional_yk_required"] == 0
        assert (Path(queued["output_path"]) if queued.get("output_path") else output / "review_generations" / queued["generation_id"] / "review_snapshot.json").exists()
        _run_worker(db, runtime, fault="export")
        first = _wait_job(store, queued["job_id"], {JobStatus.INTERRUPTED})
        first_pid = int(first["runner_pid"])
        assert first.get("recoverable") == 1
        assert not __import__("process_tree").matches(first_pid, create_time=float(first["runner_create_time"]))
        generation_root = output / "review_generations" / queued["generation_id"]
        assert list((generation_root / "staging").glob("attempt-*")), "fault injection should leave only this generation's orphan stage"

        bridge2 = object.__new__(UiBridge)
        bridge2.store = store
        bridge2.history_revision = 0
        bridge2.ensure_worker = lambda: {"online": False, "started": False}
        retry = bridge2.quality_review_reexport_retry_for_owner("owner-test", queued["job_id"])
        assert retry["status"] == "queued"
        assert retry["generation_id"] == queued["generation_id"]
        assert retry["review_snapshot_id"] == queued["review_snapshot_id"]
        _run_worker(db, runtime)
        finished = _wait_job(store, retry["job_id"], {JobStatus.FINISHED})
        second_pid = int(finished["runner_pid"])
        assert first_pid != second_pid
        final = Path(finished["output_dir"])
        files = sorted(final.glob("page_*.png"))
        assert len(files) == 1
        with __import__("PIL.Image", fromlist=["Image"]).open(files[0]) as image:
            image.verify()
        manifest = json.loads((final / "reconstruction_manifest.json").read_text(encoding="utf-8"))
        assert manifest["review_generation_id"] == queued["generation_id"]
        assert manifest["review_snapshot_id"] == queued["review_snapshot_id"]
        saved = json.loads((final / "review_snapshot.json").read_text(encoding="utf-8"))
        assert [r["effective_target_text"] for r in saved["regions"] if r["active"]] == [
            "EU ESTOU AQUI", "", "ESTOU AQUI", "TRÊS DIAS DEPOIS"]
        parent_public = bridge2._job_record(store.get_job(parent_id))
        assert parent_public["review_generations"][0]["generation_id"] == queued["generation_id"]
        staging = final.parent / "staging"
        assert not list(staging.glob("attempt-*"))
    finally:
        store.close()


@pytest.mark.parametrize("fmt", ["pdf", "psd"])
def test_worker_validates_non_png_review_formats(tmp_path: Path, fmt: str):
    db, _output, store, bridge, parent_id = _fixture(tmp_path)
    try:
        queued = bridge.quality_review_reexport_for_owner("owner-test", parent_id, output_format=fmt)
        _run_worker(db, tmp_path / "worker-runtime")
        final_job = _wait_job(store, queued["job_id"], {JobStatus.FINISHED})
        assert final_job["status"] == JobStatus.FINISHED, final_job.get("error_message")
        final_dir = Path(final_job["output_dir"])
        manifest = json.loads((final_dir / "reconstruction_manifest.json").read_text(encoding="utf-8"))
        artifact = final_dir / manifest["artifact"]
        assert artifact.exists()
        if fmt == "pdf":
            assert artifact.is_file() and artifact.stat().st_size > 0
            from pdf_reader import parse_document
            assert len(parse_document(artifact).pages) == 1
        else:
            from psd_tools import PSDImage
            psd_files = sorted(artifact.glob("*.psd")) if artifact.is_dir() else [artifact]
            assert len(psd_files) == 1
            assert PSDImage.open(str(psd_files[0])).width > 0
    finally:
        store.close()


def test_worker_generates_tall_20k_png(tmp_path: Path):
    db, _output, store, bridge, parent_id = _fixture(tmp_path, height=20_000)
    try:
        queued = bridge.quality_review_reexport_for_owner("owner-test", parent_id, output_format="png")
        _run_worker(db, tmp_path / "worker-runtime")
        final_job = _wait_job(store, queued["job_id"], {JobStatus.FINISHED})
        assert final_job["status"] == JobStatus.FINISHED, final_job.get("error_message")
        output = Path(final_job["output_dir"])
        page = output / "page_0001.png"
        with __import__("PIL.Image", fromlist=["Image"]).open(page) as image:
            assert image.size == (280, 20_000)
            image.verify()
    finally:
        store.close()
