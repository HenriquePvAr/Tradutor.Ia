"""Offline coverage for Drive CLI dotenv loading and redacted output."""

from __future__ import annotations

import _test_bootstrap  # noqa: F401

import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import drive_auth
import google_drive_factory
import local_environment


DRIVE_ENV_KEYS = (
    "COMMUNITY_STORAGE_PROVIDER",
    "COMMUNITY_DRIVE_ROOT_FOLDER_ID",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_OAUTH_TOKEN_PATH",
    "GOOGLE_OAUTH_SCOPES",
)


@pytest.fixture(autouse=True)
def isolated_drive_environment(monkeypatch):
    for key in DRIVE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _configure(monkeypatch, tmp_path: Path, *, root: str = "", token_path: Path | None = None):
    token_path = token_path or (tmp_path / "tokens" / "drive-token.json")
    client_id = "offline-client-id.example"
    client_secret = "offline-client-secret"
    lines = [
        "COMMUNITY_STORAGE_PROVIDER=google_drive",
        f"GOOGLE_OAUTH_CLIENT_ID={client_id}",
        f"GOOGLE_OAUTH_CLIENT_SECRET={client_secret}",
        f"GOOGLE_OAUTH_TOKEN_PATH={token_path}",
        f"COMMUNITY_DRIVE_ROOT_FOLDER_ID={root}",
    ]
    env_path = tmp_path / ".env"
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(local_environment, "LOCAL_ENV_PATH", env_path)
    return env_path, token_path, client_id, client_secret


def test_status_loads_client_id_without_printing_it(tmp_path, monkeypatch, capsys):
    _, _, client_id, _ = _configure(monkeypatch, tmp_path)

    assert drive_auth.main(["status"]) == 0
    output = capsys.readouterr().out
    assert "client_id_configured: True" in output
    assert client_id not in output


def test_status_loads_client_secret_without_printing_it(tmp_path, monkeypatch, capsys):
    _, _, _, client_secret = _configure(monkeypatch, tmp_path)

    assert drive_auth.main(["status"]) == 0
    output = capsys.readouterr().out
    assert "client_secret_configured: True" in output
    assert client_secret not in output


def test_status_loads_token_path_without_printing_it(tmp_path, monkeypatch, capsys):
    _, token_path, _, _ = _configure(monkeypatch, tmp_path)

    assert drive_auth.main(["status"]) == 0
    output = capsys.readouterr().out
    assert "token_path_configured: True" in output
    assert str(token_path) not in output


def test_status_works_without_token_and_does_not_create_one(tmp_path, monkeypatch, capsys):
    _, token_path, _, _ = _configure(monkeypatch, tmp_path)

    assert drive_auth.main(["status"]) == 0
    output = capsys.readouterr().out
    assert "provider: google_drive" in output
    assert "token_present: False" in output
    assert "token_valid: False" in output
    assert "root_folder_configured: False" in output
    assert not token_path.exists()


def test_authorize_receives_loaded_configuration_offline(tmp_path, monkeypatch, capsys):
    _, token_path, client_id, client_secret = _configure(monkeypatch, tmp_path)
    received = {}

    def fake_flow(**kwargs):
        received.update(kwargs)
        return drive_auth.OAuthTokens(
            access_token="offline-access-token",
            refresh_token="offline-refresh-token",
            expiry=time.time() + 3600,
        )

    monkeypatch.setattr(drive_auth, "run_installed_app_flow", fake_flow)

    assert drive_auth.main(["authorize"]) == 0
    output = capsys.readouterr().out
    assert received["client_id"] == client_id
    assert received["client_secret"] == client_secret
    assert received["scopes"] == ["https://www.googleapis.com/auth/drive.file"]
    assert token_path.is_file()
    assert str(token_path) not in output
    assert client_id not in output
    assert client_secret not in output
    assert "offline-access-token" not in output
    assert "offline-refresh-token" not in output


def test_access_receives_loaded_configuration_offline(tmp_path, monkeypatch, capsys):
    _configure(monkeypatch, tmp_path, root="offline-root-folder")

    class Provider:
        def stat_file(self, file_id):
            assert file_id == "offline-root-folder"
            return SimpleNamespace(
                file_id=file_id,
                mime_type="application/vnd.google-apps.folder",
                trashed=False,
            )

        def health_check(self):
            return True

    monkeypatch.setattr(google_drive_factory, "build_google_drive_provider", lambda _config: Provider())

    assert drive_auth.main(["test-access"]) == 0
    assert "provider_healthy: True" in capsys.readouterr().out


def test_create_root_folder_receives_loaded_configuration_offline(tmp_path, monkeypatch, capsys):
    _configure(monkeypatch, tmp_path)

    class Provider:
        def ensure_folder(self, name, parent_id):
            assert name == "TradutorIA-Comunidade"
            assert parent_id == "root"
            return "offline-folder-id"

    monkeypatch.setattr(google_drive_factory, "build_google_drive_provider", lambda _config: Provider())

    assert drive_auth.main(["create-root-folder"]) == 0
    assert "offline-folder-id" in capsys.readouterr().out


def test_revoke_receives_loaded_token_path_offline(tmp_path, monkeypatch, capsys):
    _, token_path, _, _ = _configure(monkeypatch, tmp_path)
    drive_auth.save_tokens(token_path, drive_auth.OAuthTokens())

    assert drive_auth.main(["revoke", "--yes"]) == 0
    output = capsys.readouterr().out
    assert "local token removed" in output
    assert str(token_path) not in output
    assert not token_path.exists()


def test_invalid_configuration_stops_cli_without_values(tmp_path, monkeypatch, capsys):
    env_path = tmp_path / ".env"
    invalid_content = "PRIVATE INVALID CONTENT !"
    env_path.write_text(invalid_content + "\n", encoding="utf-8")
    monkeypatch.setattr(local_environment, "LOCAL_ENV_PATH", env_path)

    assert drive_auth.main(["status"]) == 2
    captured = capsys.readouterr()
    assert "configuration_error" in captured.err
    assert invalid_content not in captured.err
    assert str(env_path) not in captured.err


def test_real_status_subprocess_uses_only_temporary_env_from_other_cwd(tmp_path):
    project = tmp_path / "project"
    elsewhere = tmp_path / "elsewhere"
    project.mkdir()
    elsewhere.mkdir()
    shutil.copy2(local_environment.PROJECT_ROOT / "drive_auth.py", project / "drive_auth.py")
    shutil.copy2(local_environment.PROJECT_ROOT / "local_environment.py", project / "local_environment.py")
    (project / "community_storage.py").write_text(
        "class StorageError(Exception):\n"
        "    def __init__(self, message='', *, status=500, transient=False):\n"
        "        super().__init__(message)\n"
        "        self.status = status\n"
        "        self.transient = transient\n",
        encoding="utf-8",
    )
    fake_client = "subprocess-client-id.example"
    fake_secret = "subprocess-client-secret"
    fake_token_path = r"C:\__tradutor_offline_test__\drive_token.json"
    (project / ".env").write_text(
        "COMMUNITY_STORAGE_PROVIDER=google_drive\n"
        f"GOOGLE_OAUTH_CLIENT_ID={fake_client}\n"
        f"GOOGLE_OAUTH_CLIENT_SECRET={fake_secret}\n"
        f"GOOGLE_OAUTH_TOKEN_PATH={fake_token_path}\n"
        "COMMUNITY_DRIVE_ROOT_FOLDER_ID=\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(project / "drive_auth.py"), "status"],
        cwd=elsewhere,
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout + result.stderr

    assert result.returncode == 0
    assert "provider: google_drive" in output
    assert "client_id_configured: True" in output
    assert "client_secret_configured: True" in output
    assert "token_path_configured: True" in output
    assert "token_present: False" in output
    assert "root_folder_configured: False" in output
    assert fake_client not in output
    assert fake_secret not in output
    assert fake_token_path not in output


def test_temporary_cli_tests_never_modify_real_env(tmp_path, monkeypatch, capsys):
    real_env = local_environment.PROJECT_ROOT / ".env"
    before = hashlib.sha256(real_env.read_bytes()).digest() if real_env.is_file() else None
    _configure(monkeypatch, tmp_path)

    assert drive_auth.main(["status"]) == 0
    capsys.readouterr()

    after = hashlib.sha256(real_env.read_bytes()).digest() if real_env.is_file() else None
    assert after == before
