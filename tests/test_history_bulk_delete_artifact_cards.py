import json
from pathlib import Path

from yomu.runtime.job_completion import build_job_completion_summary
from yomu.ui.ui_bridge import UiBridge


def test_bulk_delete_is_partial_and_delegates_to_single_item_primitive():
    bridge = UiBridge.__new__(UiBridge)
    calls = []

    def delete_one(item, *, delete_files, confirm):
        calls.append((item, delete_files, confirm))
        if item == "bad":
            raise ValueError("local_artifact_published")
        return {
            "code": "local_history_item_hidden",
            "local_artifact_id": item,
            "deleted_files": item == "gone",
            "files_state": "removed" if item == "gone" else "unsafe_path_preserved",
            "safe_error_code": "" if item == "gone" else "PATH_OUTSIDE_OUTPUT_ROOT",
        }

    bridge.delete_local_artifact = delete_one
    result = bridge.bulk_delete_local_artifacts(
        ["gone", "bad", "kept", "gone"], delete_files=True, confirm="EXCLUIR")
    assert [call[0] for call in calls] == ["gone", "bad", "kept"]
    assert all(call[1:] == (True, "EXCLUIR") for call in calls)
    assert result["requested"] == 3
    assert result["completed"] == 2
    assert result["failed"] == 1
    assert [item["status"] for item in result["results"]] == [
        "deleted", "failed", "history_removed_files_preserved"
    ]


def test_bulk_delete_rejects_confirmation_before_mutating():
    bridge = UiBridge.__new__(UiBridge)
    bridge.delete_local_artifact = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError())
    try:
        bridge.bulk_delete_local_artifacts(["x"], confirm="EXCLUI")
    except ValueError as exc:
        assert str(exc) == "confirmation_invalid"
    else:
        raise AssertionError("invalid confirmation was accepted")


def test_artifact_summary_prefers_manifest_and_falls_back_to_real_files(tmp_path):
    output = tmp_path / "run"
    (output / "images" / "clean").mkdir(parents=True)
    (output / "images" / "clean" / "001.png").write_bytes(b"png")
    (output / "translation").mkdir()
    (output / "translation" / "translation.json").write_text("{}", encoding="utf-8")
    (output / "artifact_manifest.json").write_text(json.dumps({
        "contract_version": 2,
        "requested_artifacts": ["clean_image", "translation_manifest"],
        "artifact_status": {
            "clean_image": "produced",
            "translation_manifest": "produced",
            "final_pdf": "not_requested",
        },
        "logical_page_count": 1,
    }), encoding="utf-8")
    record = {
        "status": "finished",
        "job_id": "job",
        "output_folder": str(output),
        "output_profile": "automatic",
        "pages_processed": 1,
        "pdf_path": "",
    }
    summary = build_job_completion_summary(record, tmp_path)
    assert summary["artifacts"]["clean_image"]["available"] is True
    assert summary["artifacts"]["translation_manifest"]["available"] is True
    assert summary["artifacts"]["final_pdf"]["available"] is False


def test_frontend_contract_has_selection_and_artifact_gating():
    js = Path("static/tradutor_ui.js").read_text(encoding="utf-8")
    html = Path("ui/ui_shell.html").read_text(encoding="utf-8")
    assert "histSelectionToggle" in html
    assert "bulkDeleteModalOverlay" in html
    assert "bulk_delete_local_artifacts" in js or "/api/ui/history/bulk-delete" in js
    assert "artifactIsAvailable" in js
    assert "completion_summary" in js
