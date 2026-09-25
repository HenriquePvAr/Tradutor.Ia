import pytest
from unittest.mock import patch

from local_folder_job import build_local_job_command
from run_webtoon import build_parser
from ui_bridge import UiBridge


def test_output_format_defaults_to_pdf_and_rejects_unknown():
    args = build_parser().parse_args(["https://example.test/chapter"])
    assert args.output_format == "pdf"
    assert build_parser().parse_args(["https://example.test/chapter", "--output-format", "png"]).output_format == "png"
    assert build_parser().parse_args(["https://example.test/chapter", "--output-format", "psd"]).output_format == "psd"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["https://example.test/chapter", "--output-format", "jpg"])


def test_local_images_source_contract_is_explicit_and_exclusive():
    assert UiBridge._requested_source_type({"source_type": "local_images", "image_paths": ["a.png"]}) == "local_images"
    with pytest.raises(Exception):
        UiBridge._requested_source_type({"source_type": "local_images", "image_paths": ["a.png"], "local_folder": "x"})
    with pytest.raises(Exception):
        UiBridge._requested_source_type({"source_type": "local_images", "image_paths": []})


def test_local_runner_carries_output_format_without_changing_pipeline_entry():
    command = build_local_job_command(
        snapshot_ref="abc", output="chapter", mode="quality", logical_pages=True,
        use_cache=True, force=False, use_context=True, output_format="png",
        python_executable="python",
    )
    assert command[1].endswith("run_local_folder.py")
    assert "--output-format" in command
    assert command[command.index("--output-format") + 1] == "png"


def test_psd_output_format_is_forwarded_without_changing_entrypoint():
    command = build_local_job_command(
        snapshot_ref="abc", output="chapter", mode="fast", logical_pages=True,
        use_cache=True, force=False, use_context=True, output_format="psd",
        python_executable="python",
    )
    assert command[command.index("--output-format") + 1] == "psd"


def test_frozen_local_runner_uses_internal_child_dispatcher_and_preserves_contract():
    command = build_local_job_command(
        snapshot_ref="snap-123", output="chapter", mode="quality", logical_pages=True,
        use_cache=False, force=True, use_context=False, open_output=True,
        output_format="pdf", python_executable="YomuSekai.exe", frozen=True,
    )
    assert command[:3] == ["YomuSekai.exe", "--internal-child", "local-folder"]
    assert "run_local_folder.py" not in command
    assert command[command.index("--snapshot-ref") + 1] == "snap-123"
    assert command[command.index("--output") + 1] == "chapter"
    assert "--logical-pages" in command
    assert "--force" in command
    assert "--no-context" in command
    assert "--open-output" in command


def test_frozen_local_runner_dispatches_to_existing_local_entrypoint():
    import start_tradutor

    with patch("run_local_folder.main", return_value=0) as runner:
        assert start_tradutor.main([
            "--internal-child", "local-folder", "--snapshot-ref", "snap-123",
        ]) == 0
    runner.assert_called_once_with(["--snapshot-ref", "snap-123"])
