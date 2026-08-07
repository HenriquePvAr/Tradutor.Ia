from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import hashlib
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest


from legacy_publication_migrator import (
    LegacyArtifactInput,
    LegacyMigrationInput,
    LegacyPublicationInput,
    LegacySourceInput,
    LegacyTargetPlan,
    canonical_plan_json,
    load_migration_input,
    plan_migration,
)


PDF_A = b"%PDF-1.4\n% synthetic fixture\nTEST LEGACY PUBLICATION A\n%%EOF\n"
PDF_B = b"%PDF-1.4\n% synthetic fixture\nTEST LEGACY PUBLICATION B\n%%EOF\n"


def _write_pdf(root: Path, name: str, data: bytes) -> dict[str, object]:
    path = root / name
    path.write_bytes(data)
    return {
        "local_artifact_path": name,
        "pdf_sha256": hashlib.sha256(data).hexdigest(),
        "pdf_md5": hashlib.md5(data).hexdigest(),
        "pdf_size": len(data),
    }


def _source(source_instance_id: str = "10000000-0000-4000-8000-000000000001") -> dict[str, object]:
    return {
        "source_system": "tradutor_ia_sqlite_synthetic_v1",
        "source_instance_id": source_instance_id,
        "source_schema_version": 3,
        "initial_snapshot_sha256": "a" * 64,
        "initial_logical_fingerprint": "synthetic-logical-fingerprint-a",
        "manifest_id": "20000000-0000-4000-8000-000000000001",
        "manifest_version": 1,
        "approval_state": "approved",
        "metadata": {"fixture": "synthetic", "operator": "synthetic-operator"},
    }


def _published_fixture(root: Path, **publication_overrides: object) -> dict[str, object]:
    artifact = _write_pdf(root, "published-a.pdf", PDF_A)
    publication = {
        "legacy_publication_id": "TEST-PUB-PUBLISHED-001",
        "source_instance_id": "10000000-0000-4000-8000-000000000001",
        "source_system": "tradutor_ia_sqlite_synthetic_v1",
        "owner_id": "30000000-0000-4000-8000-000000000001",
        "owner_resolution_state": "resolved",
        "source_job_id": "synthetic-job-published-001",
        "source_run_id": "40000000-0000-4000-8000-000000000001",
        "migration_mode": "published_copy",
        "requested_target_status": "community",
        "source_record_digest": "digest-published-a",
        "remote_asset_verification": "strong",
        "remote_asset": {
            "storage_provider": "google_drive",
            "storage_file_id": "synthetic-drive-file-published-001",
            "verified": True,
        },
        "target": {
            "work": {"state": "target_absent"},
            "chapter": {"state": "target_absent"},
        },
        "metadata": {"title": "Same Synthetic Title"},
    }
    publication.update(artifact)
    publication.update(publication_overrides)
    return {"schema_version": 1, "source": _source(), "publications": [publication]}


def _draft_fixture(root: Path, **publication_overrides: object) -> dict[str, object]:
    artifact = _write_pdf(root, "recovery-b.pdf", PDF_B)
    publication = {
        "legacy_publication_id": "TEST-PUB-RECOVERY-001",
        "source_instance_id": "10000000-0000-4000-8000-000000000002",
        "source_system": "tradutor_ia_sqlite_synthetic_v1",
        "owner_id": "30000000-0000-4000-8000-000000000002",
        "owner_resolution_state": "resolved",
        "source_job_id": "synthetic-job-recovery-001",
        "source_run_id": "40000000-0000-4000-8000-000000000002",
        "migration_mode": "draft_recovery",
        "requested_target_status": "draft",
        "source_record_digest": "digest-recovery-b",
        "remote_asset_verification": "unverified",
        "remote_asset": None,
        "target": {
            "work": {"state": "target_absent"},
            "chapter": {"state": "target_absent"},
        },
        "metadata": {"title": "Same Synthetic Title"},
    }
    publication.update(artifact)
    publication.update(publication_overrides)
    source = _source("10000000-0000-4000-8000-000000000002")
    source["manifest_id"] = "20000000-0000-4000-8000-000000000002"
    return {"schema_version": 1, "source": source, "publications": [publication]}


def _plan_from_dict(root: Path, data: dict[str, object]):
    fixture = root / "fixture.json"
    fixture.write_text(json.dumps(data), encoding="utf-8")
    return plan_migration(load_migration_input(fixture, artifact_root=root))


def test_source_identity_and_hash_evidence_are_distinct(tmp_path: Path):
    plan = _plan_from_dict(tmp_path, _published_fixture(tmp_path))
    assert plan.source_identity["source_instance_id"] == "10000000-...0001"
    assert plan.source_identity["initial_snapshot_sha256"] == "aaaaaaaa...aaaa"
    assert "initial_snapshot_sha256" not in plan.idempotency_key


def test_publication_identity_uses_source_and_legacy_id_not_title_or_hash(tmp_path: Path):
    first = _plan_from_dict(tmp_path, _published_fixture(tmp_path))
    changed = _published_fixture(tmp_path, metadata={"title": "Renamed Synthetic Title"})
    second = _plan_from_dict(tmp_path, changed)
    assert first.publication_identity == second.publication_identity
    assert first.idempotency_key == second.idempotency_key


def test_pdf_integrity_is_recomputed_from_fixture(tmp_path: Path):
    fixture = _published_fixture(tmp_path)
    plan = _plan_from_dict(tmp_path, fixture)
    assert plan.artifact_verification["sha256"] == "match"
    assert plan.artifact_verification["md5"] == "match"
    assert plan.artifact_verification["size"] == "match"
    assert plan.artifact_verification["signature"] == "match"


def test_hash_mismatch_blocks_plan(tmp_path: Path):
    fixture = _published_fixture(tmp_path, pdf_sha256="0" * 64)
    plan = _plan_from_dict(tmp_path, fixture)
    assert plan.readiness == "blocked_hash_mismatch"
    assert "artifact_sha256_mismatch" in plan.errors


def test_published_copy_strong_verification_is_ready(tmp_path: Path):
    plan = _plan_from_dict(tmp_path, _published_fixture(tmp_path))
    assert plan.readiness == "ready_for_execution"
    assert [op["operation_type"] for op in plan.planned_operations] == [
        "register_legacy_source",
        "claim_legacy_publication",
        "attach_migration_work",
        "attach_migration_chapter",
        "attach_migration_asset",
        "complete_legacy_migration",
    ]
    assert all(op["executed"] is False for op in plan.planned_operations)
    assert all(op["remote_write"] is True for op in plan.planned_operations)
    assert plan.would_write is False


def test_source_instance_id_must_be_valid_uuid(tmp_path: Path):
    fixture = _published_fixture(tmp_path)
    fixture["source"]["source_instance_id"] = "not-a-uuid"
    fixture["publications"][0]["source_instance_id"] = "not-a-uuid"
    plan = _plan_from_dict(tmp_path, fixture)
    assert plan.readiness == "invalid_input"
    assert "invalid_source_instance_id" in plan.errors
    assert plan.would_write is False
    assert all(op["executed"] is False for op in plan.planned_operations)


@pytest.mark.parametrize(
    "bad_source_instance_id",
    ["", "123", "00000000", "zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz"],
)
def test_malformed_source_instance_ids_are_invalid(tmp_path: Path, bad_source_instance_id: str):
    fixture = _published_fixture(tmp_path)
    fixture["source"]["source_instance_id"] = bad_source_instance_id
    fixture["publications"][0]["source_instance_id"] = bad_source_instance_id
    plan = _plan_from_dict(tmp_path, fixture)
    assert plan.readiness == "invalid_input"
    assert "invalid_source_instance_id" in plan.errors
    assert plan.would_write is False
    assert all(op["executed"] is False for op in plan.planned_operations)


def test_valid_source_instance_id_uuid_is_accepted(tmp_path: Path):
    plan = _plan_from_dict(tmp_path, _published_fixture(tmp_path))
    assert plan.readiness == "ready_for_execution"
    assert "invalid_source_instance_id" not in plan.errors


@pytest.mark.parametrize(
    ("remote_asset_verification", "expected_readiness", "expected_error"),
    [
        ("strong", "ready_for_execution", None),
        ("partial", "blocked_remote_asset_verification", "remote_asset_verification_insufficient"),
        ("unverified", "blocked_remote_asset_verification", "remote_asset_verification_insufficient"),
    ],
)
def test_published_copy_canonical_remote_asset_verification_values(
    tmp_path: Path,
    remote_asset_verification: str,
    expected_readiness: str,
    expected_error: str | None,
):
    fixture = _published_fixture(tmp_path, remote_asset_verification=remote_asset_verification)
    plan = _plan_from_dict(tmp_path, fixture)
    assert plan.readiness == expected_readiness
    assert "invalid_remote_asset_verification" not in plan.errors
    if expected_error:
        assert expected_error in plan.errors
        assert not any(op["operation_type"] == "complete_legacy_migration" for op in plan.planned_operations)


def test_published_copy_invalid_remote_asset_verification_is_invalid(tmp_path: Path):
    fixture = _published_fixture(tmp_path, remote_asset_verification="nonsense")
    plan = _plan_from_dict(tmp_path, fixture)
    assert plan.readiness == "invalid_input"
    assert "invalid_remote_asset_verification" in plan.errors
    assert "remote_asset_verification_insufficient" not in plan.errors
    assert plan.planned_operations == []


@pytest.mark.parametrize("bad_value", ["STRONG", " strong ", "Partial"])
def test_remote_asset_verification_enum_is_strict(tmp_path: Path, bad_value: str):
    fixture = _published_fixture(tmp_path, remote_asset_verification=bad_value)
    plan = _plan_from_dict(tmp_path, fixture)
    assert plan.readiness == "invalid_input"
    assert "invalid_remote_asset_verification" in plan.errors
    assert plan.planned_operations == []


def test_published_copy_missing_asset_is_blocked(tmp_path: Path):
    fixture = _published_fixture(tmp_path, remote_asset=None, remote_asset_verification="unverified")
    plan = _plan_from_dict(tmp_path, fixture)
    assert plan.readiness == "blocked_artifact"
    assert "remote_asset_missing" in plan.errors


def test_draft_recovery_allows_missing_remote_asset(tmp_path: Path):
    plan = _plan_from_dict(tmp_path, _draft_fixture(tmp_path))
    assert plan.readiness == "recovery_draft_ready"
    assert plan.artifact_verification["asset_action"] == "local_upload_required"
    assert "attach_migration_asset" not in [op["operation_type"] for op in plan.planned_operations]


@pytest.mark.parametrize("remote_asset_verification", ["unverified", "partial", "strong"])
def test_draft_recovery_accepts_canonical_remote_asset_verification_values(
    tmp_path: Path, remote_asset_verification: str
):
    fixture = _draft_fixture(tmp_path, remote_asset_verification=remote_asset_verification)
    plan = _plan_from_dict(tmp_path, fixture)
    assert plan.readiness == "recovery_draft_ready"
    assert "invalid_remote_asset_verification" not in plan.errors
    assert plan.artifact_verification["asset_action"] == "local_upload_required"


def test_draft_recovery_invalid_remote_asset_verification_is_invalid(tmp_path: Path):
    fixture = _draft_fixture(tmp_path, remote_asset_verification="nonsense")
    plan = _plan_from_dict(tmp_path, fixture)
    assert plan.readiness == "invalid_input"
    assert "invalid_remote_asset_verification" in plan.errors
    assert plan.planned_operations == []


def test_draft_recovery_never_plans_community_or_publish(tmp_path: Path):
    plan = _plan_from_dict(tmp_path, _draft_fixture(tmp_path))
    serialized = json.dumps(plan.to_json_dict(), sort_keys=True)
    forbidden = ("publish", "promote", "community", "authorize_and_publish")
    assert not any(word in serialized for word in forbidden)
    assert plan.publication_authorization_required is False


def test_authorization_is_not_automatic(tmp_path: Path):
    plan = _plan_from_dict(tmp_path, _draft_fixture(tmp_path))
    assert all("authorize" not in op["operation_type"] for op in plan.planned_operations)
    assert plan.blocked_operations["administrative_authorization"] == "separate_human_step"


def test_target_existing_requires_strong_id_or_provenance(tmp_path: Path):
    fixture = _published_fixture(
        tmp_path,
        target={
            "work": {"state": "target_existing_compatible", "title": "Same Synthetic Title"},
            "chapter": {"state": "target_existing_compatible", "title": "Same Synthetic Title"},
        },
    )
    plan = _plan_from_dict(tmp_path, fixture)
    assert plan.readiness == "blocked_target_collision"
    assert "target_identity_unresolved" in plan.errors


def test_target_conflict_blocks(tmp_path: Path):
    fixture = _published_fixture(
        tmp_path,
        target={
            "work": {"state": "target_existing_conflict", "target_id": "50000000-0000-4000-8000-000000000001", "provenance": "strong"},
            "chapter": {"state": "target_absent"},
        },
    )
    plan = _plan_from_dict(tmp_path, fixture)
    assert plan.readiness == "blocked_target_collision"
    assert "target_collision" in plan.errors


def test_missing_work_or_chapter_blocks(tmp_path: Path):
    missing_work = _published_fixture(tmp_path, target={"work": {"state": "missing"}, "chapter": {"state": "target_absent"}})
    missing_chapter = _published_fixture(tmp_path, target={"work": {"state": "target_absent"}, "chapter": {"state": "missing"}})
    assert _plan_from_dict(tmp_path, missing_work).readiness == "blocked_contract"
    assert _plan_from_dict(tmp_path, missing_chapter).readiness == "blocked_contract"


def test_owner_unresolved_blocks_published_copy(tmp_path: Path):
    fixture = _published_fixture(tmp_path, owner_resolution_state="unresolved", owner_id=None)
    plan = _plan_from_dict(tmp_path, fixture)
    assert plan.readiness == "blocked_owner"
    assert "owner_unresolved" in plan.errors


def test_invalid_mode_and_state_are_categorical(tmp_path: Path):
    invalid_mode = _published_fixture(tmp_path, migration_mode="surprise")
    assert _plan_from_dict(tmp_path, invalid_mode).readiness == "invalid_input"
    invalid_state = _published_fixture(tmp_path, target={"work": {"state": "weird"}, "chapter": {"state": "target_absent"}})
    assert _plan_from_dict(tmp_path, invalid_state).readiness == "invalid_input"


def test_idempotency_changes_only_with_canonical_identity(tmp_path: Path):
    first = _plan_from_dict(tmp_path, _published_fixture(tmp_path))
    same = _plan_from_dict(tmp_path, _published_fixture(tmp_path, metadata={"title": "changed"}))
    other_source = _published_fixture(tmp_path)
    other_source["source"]["source_instance_id"] = "10000000-0000-4000-8000-000000000099"
    other_source["publications"][0]["source_instance_id"] = "10000000-0000-4000-8000-000000000099"
    other_pub = _published_fixture(tmp_path, legacy_publication_id="TEST-PUB-PUBLISHED-099")
    assert first.idempotency_key == same.idempotency_key
    assert first.idempotency_key != _plan_from_dict(tmp_path, other_source).idempotency_key
    assert first.idempotency_key != _plan_from_dict(tmp_path, other_pub).idempotency_key


def test_deterministic_output_and_plan_digest(tmp_path: Path):
    fixture = _published_fixture(tmp_path)
    first = _plan_from_dict(tmp_path, fixture)
    second = _plan_from_dict(tmp_path, fixture)
    assert first.to_json_dict() == second.to_json_dict()
    assert first.plan_digest == hashlib.sha256(canonical_plan_json(first.to_json_dict(), include_digest=False).encode()).hexdigest()
    changed_hash = _published_fixture(tmp_path)
    changed_hash["publications"][0]["pdf_sha256"] = "f" * 64
    assert first.plan_digest != _plan_from_dict(tmp_path, changed_hash).plan_digest


def test_json_serialization_and_secret_sanitization(tmp_path: Path):
    fixture = _published_fixture(
        tmp_path,
        owner_id="30000000-0000-4000-8000-0000000000aa",
        remote_asset={"storage_provider": "google_drive", "storage_file_id": "synthetic-secret-storage-identifier-abcdef", "verified": True},
        metadata={"token": "secret-token-value", "password": "secret-password-value", "safe": "ok"},
    )
    plan = _plan_from_dict(tmp_path, fixture)
    encoded = json.dumps(plan.to_json_dict(), sort_keys=True)
    assert "30000000-0000-4000-8000-0000000000aa" not in encoded
    assert "synthetic-secret-storage-identifier-abcdef" not in encoded
    assert "secret-token-value" not in encoded
    assert "secret-password-value" not in encoded
    assert "30000000-...00aa" in encoded


def test_no_network_and_dry_run_no_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def fail_connect(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", fail_connect)
    fixture = _published_fixture(tmp_path)
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    plan = plan_migration(load_migration_input(fixture_path, artifact_root=tmp_path))
    after = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    assert plan.would_write is False
    assert before == after


def test_loader_refuses_path_traversal_and_private_runtime_inventory(tmp_path: Path):
    outside = tmp_path.parent / "outside.pdf"
    outside.write_bytes(PDF_A)
    fixture = _published_fixture(tmp_path, local_artifact_path=f"..{Path('/').as_posix()}outside.pdf")
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    with pytest.raises(ValueError, match="artifact_path_outside_root"):
        load_migration_input(path, artifact_root=tmp_path)
    bad_private = tmp_path / "publication_inventory.private.json"
    bad_private.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="private_inventory_not_allowed"):
        load_migration_input(bad_private, artifact_root=tmp_path)


def test_dataclass_model_names_are_available():
    assert LegacyMigrationInput
    assert LegacySourceInput
    assert LegacyPublicationInput
    assert LegacyArtifactInput
    assert LegacyTargetPlan


def test_cli_without_dry_run_is_rejected(tmp_path: Path):
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(_published_fixture(tmp_path)), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "legacy_publication_migrator", "--fixture", str(fixture), "--artifact-root", str(tmp_path)],
        cwd=Path(__file__).parent,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "execution_not_enabled" in result.stderr


def test_cli_dry_run_success(tmp_path: Path):
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(_published_fixture(tmp_path)), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "legacy_publication_migrator", "--fixture", str(fixture), "--artifact-root", str(tmp_path), "--dry-run"],
        cwd=Path(__file__).parent,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["readiness"] == "ready_for_execution"
    assert payload["would_write"] is False


def test_cli_dry_run_invalid_input_returns_json_without_traceback(tmp_path: Path):
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(_draft_fixture(tmp_path, remote_asset_verification="nonsense")), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "legacy_publication_migrator", "--fixture", str(fixture), "--artifact-root", str(tmp_path), "--dry-run"],
        cwd=Path(__file__).parent,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr
    payload = json.loads(result.stdout)
    assert payload["readiness"] == "invalid_input"
    assert "invalid_remote_asset_verification" in payload["errors"]
    assert payload["would_write"] is False
