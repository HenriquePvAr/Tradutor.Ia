"""Composition-root safeguards for Community publication metadata persistence."""

import _test_bootstrap  # noqa: F401

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from community_publication_metadata import (
    NullPublicationMetadataRepository,
    PublicationMetadataError,
    PublicationMetadataRepositoryConfig,
    SecretHeaderDict,
    SupabasePublicationMetadataRepository,
    build_publication_metadata_repository,
    redacted_config,
)

SYNTHETIC_SECRET = "sb_secret_TEST_ONLY_REDACTION_CANARY_00000000000000000000"
SYNTHETIC_URL = "https://example.invalid"


def test_env_composition_wires_supabase_secret_without_legacy_fallback(monkeypatch):
    from community_api import publication_metadata_config_from_env

    monkeypatch.setenv("COMMUNITY_PUBLICATION_METADATA_PROVIDER", "supabase")
    monkeypatch.setenv("SUPABASE_URL", SYNTHETIC_URL)
    monkeypatch.setenv("SUPABASE_SECRET_KEY", SYNTHETIC_SECRET)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "legacy-compromised-not-accepted")

    config = publication_metadata_config_from_env()
    repo = build_publication_metadata_repository({**config, "transport": object()})
    assert isinstance(repo, SupabasePublicationMetadataRepository)
    assert not isinstance(repo, NullPublicationMetadataRepository)
    assert "secret_key" not in config
    assert "backend_secret_key" not in config
    assert config["secret_env_var"] == "SUPABASE_SECRET_KEY"
    assert "SUPABASE_SERVICE_ROLE_KEY" not in config


def test_required_env_composition_fails_closed_without_secret(monkeypatch):
    from community_api import publication_metadata_config_from_env

    monkeypatch.setenv("COMMUNITY_PUBLICATION_METADATA_PROVIDER", "supabase")
    monkeypatch.setenv("SUPABASE_URL", SYNTHETIC_URL)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "legacy-compromised-not-accepted")

    with pytest.raises(PublicationMetadataError, match="not_configured"):
        publication_metadata_config_from_env()


def test_explicit_offline_mode_is_the_only_null_repository(monkeypatch):
    from community_api import publication_metadata_config_from_env

    monkeypatch.setenv("COMMUNITY_PUBLICATION_METADATA_PROVIDER", "none")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("COMMUNITY_STORAGE_PROVIDER", "filesystem")
    monkeypatch.delenv("COMMUNITY_PUBLICATION_METADATA_REQUIRED", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    config = publication_metadata_config_from_env()
    assert isinstance(build_publication_metadata_repository(config), NullPublicationMetadataRepository)


def test_config_repr_str_and_diagnostics_redact_synthetic_secret():
    config = PublicationMetadataRepositoryConfig({
        "provider": "supabase",
        "url": SYNTHETIC_URL,
        "secret_key": SYNTHETIC_SECRET,
        "Authorization": f"Bearer {SYNTHETIC_SECRET}",
        "apikey": SYNTHETIC_SECRET,
    })

    assert SYNTHETIC_SECRET not in repr(config)
    assert SYNTHETIC_SECRET not in str(config)
    assert SYNTHETIC_SECRET not in repr(config.safe_debug_dict())
    assert SYNTHETIC_SECRET not in repr(redacted_config(config))
    assert "<redacted>" in repr(config)


def test_header_repr_redacts_synthetic_secret_while_value_remains_usable():
    headers = SecretHeaderDict({"apikey": SYNTHETIC_SECRET, "Accept": "application/json"})

    assert headers["apikey"] == SYNTHETIC_SECRET
    assert SYNTHETIC_SECRET not in repr(headers)
    assert "<redacted>" in repr(headers)


def test_repository_exception_and_repr_do_not_leak_synthetic_secret():
    class FailingTransport:
        def request(self, *args, **kwargs):
            raise RuntimeError(f"transport contained {SYNTHETIC_SECRET}")

    repo = SupabasePublicationMetadataRepository(
        build_publication_metadata_repository({
            "provider": "supabase",
            "url": SYNTHETIC_URL,
            "secret_key": SYNTHETIC_SECRET,
            "transport": object(),
        })._config,
        secret_key=SYNTHETIC_SECRET,
        transport=FailingTransport(),
    )
    with pytest.raises(PublicationMetadataError) as caught:
        repo.reserve({
            "publication_id": "00000000-0000-0000-0000-000000000001",
            "owner_user_id": "00000000-0000-0000-0000-000000000002",
            "job_id": "job",
            "run_id": "run",
            "artifact_sha256": "a" * 64,
            "artifact_size_bytes": 1,
            "mime_type": "application/pdf",
            "storage_provider": "google_drive",
        })

    assert SYNTHETIC_SECRET not in str(caught.value)
    assert SYNTHETIC_SECRET not in repr(caught.value)


def test_config_logging_redacts_synthetic_secret(caplog):
    config = PublicationMetadataRepositoryConfig({"secret_key": SYNTHETIC_SECRET})

    with caplog.at_level(logging.INFO):
        logging.getLogger("test").info("config=%r", config)

    assert SYNTHETIC_SECRET not in caplog.text
    assert "<redacted>" in caplog.text


def test_pytest_assertion_diff_does_not_reveal_synthetic_secret(tmp_path):
    script = tmp_path / "assertion_canary.py"
    script.write_text(
        "\n".join([
            "import _test_bootstrap",
            "from community_publication_metadata import PublicationMetadataRepositoryConfig",
            f"secret = {SYNTHETIC_SECRET!r}",
            "config = PublicationMetadataRepositoryConfig({'secret_key': secret})",
            "def test_canary_failure():",
            "    assert config == {'secret_key': 'expected-redacted'}",
        ]),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parent),
        "TRADUTOR_IA_HERMETIC_TEST_ENV": "1",
        "SUPABASE_URL": SYNTHETIC_URL,
        "SUPABASE_SECRET_KEY": SYNTHETIC_SECRET,
        "APP_ENV": "test",
    }

    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(script), "-q"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    captured = result.stdout + result.stderr
    assert result.returncode != 0
    assert SYNTHETIC_SECRET not in captured
    assert "<redacted>" in captured
