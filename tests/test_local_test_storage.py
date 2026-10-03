"""Security contract for the explicit local-test community storage provider."""

import _test_bootstrap  # noqa: F401

import os
from pathlib import Path
from unittest.mock import patch

import pytest

import community_api
from community_storage import LocalTestStorageProvider, StorageError, build_storage_provider


def _provider(tmp_path: Path, owner: str = "owner-a") -> LocalTestStorageProvider:
    return LocalTestStorageProvider(tmp_path / "storage", owner_id=owner)


def _upload(provider: LocalTestStorageProvider, payload: bytes = b"%PDF-1.4\n%%EOF"):
    folder = provider.ensure_folder("series", "root")
    session = provider.create_resumable_session(
        filename="chapter.pdf", mime_type="application/pdf",
        size=len(payload), parent_id=folder,
    )
    result = provider.upload_chunk(session, 0, payload)
    return folder, session, result


@pytest.mark.parametrize("configured", ["", "google_drive", "s3", "supabase"])
def test_local_test_rejects_missing_or_remote_storage_before_provider_build(
    tmp_path, monkeypatch, configured
):
    env = {
        "APP_ENV": "test",
        "AUTH_PROVIDER": "local_test",
        "ALLOW_LOCAL_TEST_IDENTITIES": "1",
        "STORAGE_PROVIDER": configured,
        "LOCAL_TEST_STORAGE_ROOT": str(tmp_path / "storage"),
        "TRADUTOR_UI_HOST": "127.0.0.1",
        "COMMUNITY_STORAGE_PROVIDER": "google_drive",
    }
    with patch.dict(os.environ, env, clear=True), patch(
        "google_drive_factory.build_google_drive_provider"
    ) as remote:
        with pytest.raises(StorageError, match="local_test_external_storage_blocked"):
            community_api.storage_provider_name()
        remote.assert_not_called()


@pytest.mark.parametrize(
    ("app_env", "host"),
    [("production", "127.0.0.1"), ("test", "0.0.0.0"), ("development", "example.test")],
)
def test_local_test_storage_rejects_production_or_public_host(tmp_path, app_env, host):
    env = {
        "APP_ENV": app_env,
        "AUTH_PROVIDER": "local_test",
        "ALLOW_LOCAL_TEST_IDENTITIES": "1",
        "STORAGE_PROVIDER": "local_test",
        "LOCAL_TEST_STORAGE_ROOT": str(tmp_path / "storage"),
        "TRADUTOR_UI_HOST": host,
    }
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(StorageError, match="local_test_external_storage_blocked"):
            community_api.storage_provider_name()


def test_explicit_local_test_upload_is_owner_partitioned_and_opaque(tmp_path):
    provider = build_storage_provider({
        "storage_provider": "local_test",
        "storage_root": str(tmp_path / "storage"),
        "owner_id": "owner-a",
    })
    _, session, result = _upload(provider)
    assert result.completed
    assert result.file_id.startswith("ltf.")
    assert "owner-a" not in result.file_id
    assert "\\" not in result.file_id and "/" not in result.file_id
    metadata = provider.stat_file(result.file_id)
    assert metadata.mime_type == "application/pdf"
    assert metadata.size == len(b"%PDF-1.4\n%%EOF")
    assert provider.root in provider._paths(result.file_id)[1].resolve().parents
    assert not provider._session_path(session.session_id).is_symlink()


def test_local_test_storage_owner_scope_is_fail_closed(tmp_path):
    owner_a = _provider(tmp_path, "owner-a")
    _, _, result = _upload(owner_a)
    owner_b = _provider(tmp_path, "owner-b")
    with pytest.raises(StorageError, match="owner_mismatch"):
        owner_b.stat_file(result.file_id)


@pytest.mark.parametrize("filename", ["../chapter.pdf", "C:\\chapter.pdf", "/tmp/chapter.pdf"])
def test_local_test_storage_rejects_traversal_and_absolute_names(tmp_path, filename):
    provider = _provider(tmp_path)
    with pytest.raises(StorageError, match="file_type_rejected"):
        provider.create_resumable_session(
            filename=filename, mime_type="application/pdf", size=1, parent_id="root"
        )


def test_local_test_storage_rejects_mime_and_oversize(tmp_path):
    provider = _provider(tmp_path)
    with pytest.raises(StorageError, match="file_type_rejected"):
        provider.create_resumable_session(
            filename="chapter.pdf", mime_type="text/html", size=1, parent_id="root"
        )
    with pytest.raises(StorageError, match="size_rejected"):
        provider.create_resumable_session(
            filename="chapter.pdf", mime_type="application/pdf",
            size=provider._MAX_BYTES + 1, parent_id="root",
        )


def test_local_test_storage_rejects_external_symlink(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    provider = LocalTestStorageProvider(root, owner_id="owner-a")
    owner_root = provider._owner_root(provider.owner_key)
    outside = tmp_path / "outside"
    outside.mkdir()
    owner_root.rmdir()
    try:
        owner_root.symlink_to(outside, target_is_directory=True)
    except OSError:
        original = Path.is_symlink
        monkeypatch.setattr(
            Path,
            "is_symlink",
            lambda path: path == owner_root or original(path),
        )
    with pytest.raises(StorageError, match="path_rejected"):
        provider.ensure_folder("series", "root")


def test_local_test_cleanup_is_idempotent_and_removes_partial_file(tmp_path):
    provider = _provider(tmp_path)
    folder = provider.ensure_folder("series", "root")
    session = provider.create_resumable_session(
        filename="chapter.pdf", mime_type="application/pdf", size=10, parent_id=folder
    )
    provider.upload_chunk(session, 0, b"123")
    _, _, partial = provider._paths(session.file_id)
    assert partial.exists()
    provider.abandon_resumable_session(session.session_id)
    provider.abandon_resumable_session(session.session_id)
    assert not partial.exists()
    provider.delete_file(session.file_id)
    provider.delete_file(session.file_id)
    assert not provider.exists(session.file_id)


def test_failed_oversize_chunk_does_not_write_temporary_bytes(tmp_path):
    provider = _provider(tmp_path)
    session = provider.create_resumable_session(
        filename="chapter.pdf", mime_type="application/pdf", size=2, parent_id="root"
    )
    _, _, partial = provider._paths(session.file_id)
    with pytest.raises(StorageError, match="size_rejected"):
        provider.upload_chunk(session, 0, b"too large")
    assert partial.read_bytes() == b""
    provider.abandon_resumable_session(session.session_id)
    assert not partial.exists()


def test_local_test_storage_errors_do_not_expose_root(tmp_path):
    provider = _provider(tmp_path)
    with pytest.raises(StorageError) as captured:
        provider.stat_file("../outside")
    assert str(tmp_path) not in str(captured.value)
