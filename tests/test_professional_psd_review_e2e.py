"""Review re-export PSD in both typesetting modes, via the real worker subprocess.

Reuses the R4 worker fixture. Proves ON/OFF layer contracts reflect the frozen review
snapshot (overrides, IGNORE, manual regions), that the mode is persisted in the generation
config + lineage, that ON and OFF are distinct generations, and that a mode survives
retry/recovery — all with no provider/OCR/YK.
"""
from __future__ import annotations

import json
from pathlib import Path

from psd_tools import PSDImage

from job_store import JobStatus
from test_review_reexport_worker_e2e import _fixture, _run_worker, _wait_job


def _psd_layer_names(final_dir: Path):
    manifest = json.loads((final_dir / "reconstruction_manifest.json").read_text(encoding="utf-8"))
    artifact = final_dir / manifest["artifact"]
    psd_files = sorted(artifact.glob("*.psd")) if artifact.is_dir() else [artifact]
    return [l.name for l in PSDImage.open(str(psd_files[0]))], artifact


def test_review_psd_on_has_region_layers(tmp_path):
    db, _output, store, bridge, parent_id = _fixture(tmp_path)
    try:
        queued = bridge.quality_review_reexport_for_owner("owner-test", parent_id,
                                                          output_format="psd", typesetting_mode="on")
        assert queued["additional_yk_required"] == 0
        _run_worker(db, tmp_path / "rt")
        fin = _wait_job(store, queued["job_id"], {JobStatus.FINISHED})
        assert fin["status"] == JobStatus.FINISHED, fin.get("error_message")
        names, artifact = _psd_layer_names(Path(fin["output_dir"]))
        assert names[0] == "Original" and names[1] == "Cleaned"
        assert "Translated Preview" in names
        # auto-1 (translate) + 2 manual translate regions -> 3 region layers; auto-sfx ignored.
        assert sum(n.startswith("Text/Region") for n in names) == 3
        manifest = json.loads((artifact / "psd_manifest.json").read_text(encoding="utf-8"))
        assert manifest["typesetting_mode"] == "on"
    finally:
        store.close()


def test_review_psd_off_is_clean_only_and_distinct_generation(tmp_path):
    db, _output, store, bridge, parent_id = _fixture(tmp_path)
    try:
        on = bridge.quality_review_reexport_for_owner("owner-test", parent_id,
                                                      output_format="psd", typesetting_mode="on")
        off = bridge.quality_review_reexport_for_owner("owner-test", parent_id,
                                                       output_format="psd", typesetting_mode="off")
        # ON and OFF are independently cached generations.
        assert on["generation_id"] != off["generation_id"]
        # mode persisted in the generation job config
        off_job = store.get_job(off["job_id"])
        assert off_job["configuration"]["typesetting_mode"] == "off"
        _run_worker(db, tmp_path / "rt")   # runs whichever is queued first
        _run_worker(db, tmp_path / "rt")   # and the second
        fin = _wait_job(store, off["job_id"], {JobStatus.FINISHED})
        assert fin["status"] == JobStatus.FINISHED, fin.get("error_message")
        names, artifact = _psd_layer_names(Path(fin["output_dir"]))
        assert names == ["Original", "Cleaned"]
        manifest = json.loads((artifact / "psd_manifest.json").read_text(encoding="utf-8"))
        assert manifest["typesetting_mode"] == "off"
        # Region metadata preserved even though no translated layers exist (effective review data).
        regions = manifest["pages"][0]["regions"]
        assert any(r["region_id"] == "manual:narration" and r["target_text_effective"] == "TRÊS DIAS DEPOIS" for r in regions)
        assert any(r["region_id"] == "auto-sfx" and r["would_render_in_typesetting_on"] is False for r in regions)
        # lineage records the mode
        lineage = json.loads((Path(fin["output_dir"]) / "reconstruction_manifest.json").read_text(encoding="utf-8"))
        assert lineage["typesetting_mode"] == "off"
        assert lineage["provider_calls_added"] == 0 and lineage["ocr_calls_added"] == 0 and lineage["additional_yk_required"] == 0
    finally:
        store.close()


def test_review_psd_off_recovery_preserves_mode(tmp_path):
    db, _output, store, bridge, parent_id = _fixture(tmp_path)
    try:
        queued = bridge.quality_review_reexport_for_owner("owner-test", parent_id,
                                                          output_format="psd", typesetting_mode="off")
        _run_worker(db, tmp_path / "rt", fault="export")  # crash once after validate
        first = _wait_job(store, queued["job_id"], {JobStatus.INTERRUPTED})
        assert first.get("recoverable") == 1
        from ui_bridge import UiBridge
        bridge2 = object.__new__(UiBridge)
        bridge2.store = store
        bridge2.history_revision = 0
        bridge2.ensure_worker = lambda: {"online": False, "started": False}
        retry = bridge2.quality_review_reexport_retry_for_owner("owner-test", queued["job_id"])
        assert retry["generation_id"] == queued["generation_id"]
        assert store.get_job(retry["job_id"])["configuration"]["typesetting_mode"] == "off"
        _run_worker(db, tmp_path / "rt")
        fin = _wait_job(store, retry["job_id"], {JobStatus.FINISHED})
        assert fin["status"] == JobStatus.FINISHED, fin.get("error_message")
        names, _artifact = _psd_layer_names(Path(fin["output_dir"]))
        assert names == ["Original", "Cleaned"]
    finally:
        store.close()
