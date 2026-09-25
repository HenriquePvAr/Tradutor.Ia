import json
from pathlib import Path

from job_store import JobStatus
from local_folder_job import build_local_job_command
from ui_bridge import UiBridge


def test_frozen_local_command_can_bind_output_to_authoritative_job_dir(tmp_path):
    output_dir = tmp_path / "user-data" / "output" / "webtoon_chapter" / "run-1"
    command = build_local_job_command(
        snapshot_ref="snapshot-1",
        output="webtoon_chapter/run-1",
        output_path=output_dir,
        mode="quality",
        logical_pages=True,
        use_cache=False,
        force=True,
        use_context=True,
        python_executable=str(tmp_path / "YomuSekai.exe"),
        frozen=True,
    )

    assert command[1:3] == ["--internal-child", "local-folder"]
    assert command[command.index("--output") + 1] == str(output_dir.resolve())


def test_legacy_frozen_local_artifacts_populate_review_history_metrics(tmp_path):
    exe = tmp_path / "candidate23" / "YomuSekai.exe"
    output_dir = exe.parent / "_internal" / "output" / "webtoon_chapter" / "run-1"
    output_dir.mkdir(parents=True)
    (output_dir / "run_manifest.json").write_text(json.dumps({
        "run_id": "run-1",
        "final_status": "review_required",
        "quality_passed": False,
        "pdf_path": "chapter.pdf",
    }), encoding="utf-8")
    (output_dir / "chapter.pdf").write_bytes(b"pdf")
    (output_dir / "timing_report.json").write_text(json.dumps({
        "processed_images": 12,
        "groups_translated": 4,
        "pages_with_error": 0,
        "quality_validation": {
            "passed": False,
            "manual_review_required_groups": 1,
        },
    }), encoding="utf-8")

    job = {
        "id": "job-1",
        "run_id": "run-1",
        "status": JobStatus.FAILED,
        "source_type": "local_folder",
        "output_dir": str(tmp_path / "user-data" / "output" / "webtoon_chapter" / "run-1"),
        "command": [str(exe), "--internal-child", "local-folder", "--output", "webtoon_chapter/run-1"],
        "configuration": {"source_type": "local_folder", "mode": "quality"},
    }

    bridge = object.__new__(UiBridge)
    assert UiBridge._effective_artifact_output_dir(job) == output_dir.resolve()
    assert bridge._current_job_result_metrics(job) == {
        "pages_processed": 12,
        "groups_translated": 4,
        "manual_review_count": 1,
        "errors": 0,
        "quality_gate": False,
    }
