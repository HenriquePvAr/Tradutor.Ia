import json
from pathlib import Path

from ui_bridge import UiBridge
from ui_history import UIHistoryStore


def _download_fixture(tmp_path: Path, *, include_report=True):
    folder = tmp_path / "chapter" / "run"
    input_dir = folder / "input"
    input_dir.mkdir(parents=True)
    files = []
    for index in range(1, 4):
        path = input_dir / f"{index:03d}.png"
        path.write_bytes(b"png")
        files.append(path)
    (folder / "job_manifest.json").write_text(json.dumps({
        "job_id": "a" * 32,
        "run_id": "run-1",
        "status": "finished",
        "result_type": "directory",
        "result_path": str(folder),
    }), encoding="utf-8")
    if include_report:
        (folder / "downloaded_images.json").write_text(json.dumps({
            "total_downloaded": 3,
            "downloaded": [{"path": str(path)} for path in files],
            "download_gate": {"passed": True},
        }), encoding="utf-8")
    return folder


def test_download_only_metrics_use_download_report_without_translation_groups(tmp_path):
    folder = _download_fixture(tmp_path)
    metrics = UiBridge._download_only_result_metrics(folder)
    assert metrics["pages_processed"] == 3
    assert metrics["groups_translated"] is None
    assert metrics["quality_gate"] == "NOT_APPLICABLE"
    assert metrics["download_gate"] == "PASS"
    assert metrics["download_report_valid"] is True


def test_download_only_output_verification_accepts_directory_manifest(tmp_path):
    folder = _download_fixture(tmp_path)
    verification, manifest = UIHistoryStore._output_verification(folder, download_only=True)
    assert verification == "manifest_verified"
    assert manifest["result_type"] == "directory"


def test_download_only_missing_report_fails_safe_without_zero_success(tmp_path):
    folder = _download_fixture(tmp_path, include_report=False)
    metrics = UiBridge._download_only_result_metrics(folder)
    verification, _ = UIHistoryStore._output_verification(folder, download_only=True)
    assert metrics["pages_processed"] is None
    assert metrics["download_report_valid"] is False
    assert verification == "legacy_unverified"


def test_history_safe_record_preserves_download_only_contract(tmp_path):
    store = UIHistoryStore(tmp_path / "history.json")
    record = store.upsert({
        "id": "download-job",
        "download_only": True,
        "pages_processed": 3,
        "groups_translated": None,
        "quality_gate": "NOT_APPLICABLE",
        "source_verified": True,
        "download_report_valid": True,
        "output_verification": "manifest_verified",
        "status": "finished",
    })
    assert record["download_only"] is True
    assert record["source_verified"] is True
    assert record["pages_processed"] == 3
    assert record["groups_translated"] is None
    assert record["quality_gate"] == "NOT_APPLICABLE"


def test_history_ui_has_download_only_specific_rendering_and_keeps_normal_path():
    source = Path("static/tradutor_ui.js").read_text(encoding="utf-8")
    block = source[source.index("function renderHistoryCard"):source.index("function renderHistory()")]
    assert "isDownloadOnly" in block
    assert "páginas baixadas" in block
    assert "resultado de download indisponível" in block
    assert "groups_translated" in block
    assert "gate pendente" in block
