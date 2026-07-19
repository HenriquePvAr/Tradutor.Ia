"""Deterministic local environment loading without external side effects."""

from __future__ import annotations

import _test_bootstrap  # noqa: F401

import os
import subprocess
import sys
from pathlib import Path

import pytest

import community_publish_runner
import local_environment
import start_tradutor
import worker_service


ENV_KEYS = (
    "COMMUNITY_STORAGE_PROVIDER",
    "COMMUNITY_DRIVE_ROOT_FOLDER_ID",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_OAUTH_TOKEN_PATH",
    "GOOGLE_OAUTH_SCOPES",
    "LOCAL_ENV_TEST_VALUE",
)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _select(monkeypatch, path: Path) -> None:
    monkeypatch.setattr(local_environment, "LOCAL_ENV_PATH", path)


def test_env_present_and_process_environment_absent(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("LOCAL_ENV_TEST_VALUE=from-local-file\n", encoding="utf-8")
    _select(monkeypatch, env_path)

    assert local_environment.load_local_environment() is True
    assert os.environ["LOCAL_ENV_TEST_VALUE"] == "from-local-file"


def test_env_absent_is_allowed(tmp_path, monkeypatch):
    _select(monkeypatch, tmp_path / ".env")

    assert local_environment.load_local_environment() is False
    assert "LOCAL_ENV_TEST_VALUE" not in os.environ


def test_process_environment_overrides_local_file(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("COMMUNITY_STORAGE_PROVIDER=google_drive\n", encoding="utf-8")
    _select(monkeypatch, env_path)
    monkeypatch.setenv("COMMUNITY_STORAGE_PROVIDER", "filesystem")

    local_environment.load_local_environment()

    assert os.environ["COMMUNITY_STORAGE_PROVIDER"] == "filesystem"


def test_env_example_is_never_loaded(tmp_path, monkeypatch):
    (tmp_path / ".env.example").write_text(
        "LOCAL_ENV_TEST_VALUE=must-not-load\n", encoding="utf-8"
    )
    _select(monkeypatch, tmp_path / ".env")

    assert local_environment.load_local_environment() is False
    assert "LOCAL_ENV_TEST_VALUE" not in os.environ


def test_loading_is_independent_of_current_working_directory(tmp_path, monkeypatch):
    project = tmp_path / "project"
    elsewhere = tmp_path / "elsewhere"
    project.mkdir()
    elsewhere.mkdir()
    env_path = project / ".env"
    env_path.write_text("LOCAL_ENV_TEST_VALUE=deterministic\n", encoding="utf-8")
    _select(monkeypatch, env_path)
    monkeypatch.chdir(elsewhere)

    local_environment.load_local_environment()

    assert os.environ["LOCAL_ENV_TEST_VALUE"] == "deterministic"


def test_windows_path_is_preserved(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    windows_path = r"C:\Users\Example User\AppData\Local\TradutorIA\drive_token.json"
    env_path.write_text(f"GOOGLE_OAUTH_TOKEN_PATH={windows_path}\n", encoding="utf-8")
    _select(monkeypatch, env_path)

    local_environment.load_local_environment()

    assert os.environ["GOOGLE_OAUTH_TOKEN_PATH"] == windows_path


def test_quoted_value_is_supported(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text('LOCAL_ENV_TEST_VALUE="quoted value"\n', encoding="utf-8")
    _select(monkeypatch, env_path)

    local_environment.load_local_environment()

    assert os.environ["LOCAL_ENV_TEST_VALUE"] == "quoted value"


def test_unquoted_value_with_spaces_is_supported(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("LOCAL_ENV_TEST_VALUE=value with spaces\n", encoding="utf-8")
    _select(monkeypatch, env_path)

    local_environment.load_local_environment()

    assert os.environ["LOCAL_ENV_TEST_VALUE"] == "value with spaces"


def test_empty_root_folder_value_is_preserved(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("COMMUNITY_DRIVE_ROOT_FOLDER_ID=\n", encoding="utf-8")
    _select(monkeypatch, env_path)

    local_environment.load_local_environment()

    assert os.environ["COMMUNITY_DRIVE_ROOT_FOLDER_ID"] == ""


def test_google_drive_provider_is_loaded(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("COMMUNITY_STORAGE_PROVIDER=google_drive\n", encoding="utf-8")
    _select(monkeypatch, env_path)

    local_environment.load_local_environment()

    assert os.environ["COMMUNITY_STORAGE_PROVIDER"] == "google_drive"


def test_invalid_file_fails_closed_without_echoing_contents(tmp_path, monkeypatch, capsys):
    env_path = tmp_path / ".env"
    invalid_content = "THIS LINE IS INVALID AND PRIVATE !"
    env_path.write_text(invalid_content + "\n", encoding="utf-8")
    _select(monkeypatch, env_path)

    assert local_environment.load_local_environment_for_entrypoint() is False
    captured = capsys.readouterr()
    assert "configuration_error" in captured.err
    assert invalid_content not in captured.err
    assert str(env_path) not in captured.err


def test_drive_auth_import_does_not_open_browser(tmp_path):
    code = (
        "import webbrowser\n"
        "webbrowser.open = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('browser'))\n"
        "import drive_auth\n"
        "print('import-ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(local_environment.PROJECT_ROOT)}, check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "import-ok"


def test_drive_auth_import_does_not_open_network(tmp_path):
    code = (
        "import socket\n"
        "socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('network'))\n"
        "import drive_auth\n"
        "print('import-ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(local_environment.PROJECT_ROOT)}, check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "import-ok"


def test_launcher_fails_safely_when_local_environment_is_invalid(monkeypatch):
    monkeypatch.setattr(start_tradutor, "load_local_environment_for_entrypoint", lambda: False)

    assert start_tradutor.main(["status"]) == 2


def test_worker_fails_safely_when_local_environment_is_invalid(monkeypatch):
    monkeypatch.setattr(worker_service, "load_local_environment_for_entrypoint", lambda: False)

    assert worker_service.main(["--status"]) == 2


def test_publish_runner_fails_safely_when_local_environment_is_invalid(monkeypatch):
    monkeypatch.setattr(
        community_publish_runner, "load_local_environment_for_entrypoint", lambda: False
    )

    assert community_publish_runner.main([]) == 2


def test_app_ui_loads_environment_before_reading_port():
    source = (local_environment.PROJECT_ROOT / "app_ui.py").read_text(encoding="utf-8")

    assert source.index("load_local_environment_for_entrypoint()") < source.index("APP_PORT =")
