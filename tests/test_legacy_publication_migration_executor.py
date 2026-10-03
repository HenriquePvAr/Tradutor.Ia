from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import dataclasses
import hashlib
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from legacy_publication_migrator import (
    canonical_plan_json,
    load_migration_input,
    plan_migration,
)
from legacy_publication_migration_executor import (
    ConflictingStorageObjectError,
    FakeLegacyArtifactStorage,
    FakeLegacyProvenanceBackend,
    FakeLegacyTargetBackend,
    LegacyMigrationExecutor,
    ResponseLostError,
    TerminalBackendError,
    TransientBackendError,
)
from test_legacy_publication_migrator_dry_run import _draft_fixture, _published_fixture


def _input_and_plan(root: Path, fixture: dict[str, object]):
    fixture_path = root / "executor-fixture.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    migration_input = load_migration_input(fixture_path, artifact_root=root)
    return migration_input, plan_migration(migration_input)


def _published_input_and_plan(root: Path, **overrides: object):
    return _input_and_plan(root, _published_fixture(root, **overrides))


def _draft_input_and_plan(root: Path, **overrides: object):
    return _input_and_plan(root, _draft_fixture(root, **overrides))


def _executor(
    *,
    provenance: FakeLegacyProvenanceBackend | None = None,
    targets: FakeLegacyTargetBackend | None = None,
    storage: FakeLegacyArtifactStorage | None = None,
    max_attempts: int = 3,
):
    return LegacyMigrationExecutor(
        provenance_backend=provenance or FakeLegacyProvenanceBackend(),
        target_backend=targets or FakeLegacyTargetBackend(),
        artifact_storage=storage or FakeLegacyArtifactStorage(),
        max_attempts=max_attempts,
    )


def test_executor_requires_explicit_backends():
    with pytest.raises(TypeError):
        LegacyMigrationExecutor()  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "fixture_factory,overrides,expected_readiness",
    [
        (_published_fixture, {"remote_asset_verification": "partial"}, "blocked_remote_asset_verification"),
        (_published_fixture, {"owner_resolution_state": "unresolved", "owner_id": None}, "blocked_owner"),
        (_published_fixture, {"migration_mode": "surprise"}, "invalid_input"),
        (_published_fixture, {"target": {"work": {"state": "target_existing_conflict"}, "chapter": {"state": "target_absent"}}}, "blocked_target_collision"),
    ],
)
def test_blocked_plan_rejected_before_backend_calls(tmp_path: Path, fixture_factory, overrides: dict[str, object], expected_readiness: str):
    migration_input, plan = _input_and_plan(tmp_path, fixture_factory(tmp_path, **overrides))
    assert plan.readiness == expected_readiness
    provenance = FakeLegacyProvenanceBackend()
    targets = FakeLegacyTargetBackend()
    storage = FakeLegacyArtifactStorage()
    result = _executor(provenance=provenance, targets=targets, storage=storage).execute(migration_input, plan)
    assert result.execution_status == "blocked_before_execution"
    assert result.errors
    assert result.completed_operations == []
    assert result.backend_calls_summary["total"] == 0
    assert provenance.call_count == 0
    assert targets.call_count == 0
    assert storage.upload_count == 0


def test_plan_digest_is_recorded_and_validated(tmp_path: Path):
    migration_input, plan = _published_input_and_plan(tmp_path)
    result = _executor().execute(migration_input, plan)
    assert result.plan_digest == plan.plan_digest
    assert result.execution_status == "completed"


@pytest.mark.parametrize("field,value", [("readiness", "ready_for_execution"), ("plan_digest", "0" * 64)])
def test_plan_tampering_is_rejected(tmp_path: Path, field: str, value: str):
    migration_input, plan = _published_input_and_plan(tmp_path, remote_asset_verification="partial")
    tampered = dataclasses.replace(plan, **{field: value})
    result = _executor().execute(migration_input, tampered)
    assert result.execution_status == "blocked_before_execution"
    assert "plan_digest_mismatch" in result.errors or "plan_not_executable" in result.errors
    assert result.backend_calls_summary["total"] == 0


@pytest.mark.parametrize("replacement", [b"%PDF-1.4\nchanged size\n%%EOF\n", b"%PDF-1.4\n% synthetic fixture\nTEST LEGACY PUBLICATION B\n%%EOF\n"])
def test_artifact_changed_after_plan_blocks_before_backend(tmp_path: Path, replacement: bytes):
    migration_input, plan = _published_input_and_plan(tmp_path)
    migration_input.publication.artifact.local_artifact_path.write_bytes(replacement)
    result = _executor().execute(migration_input, plan)
    assert result.execution_status == "blocked_before_execution"
    assert "artifact_changed_after_plan" in result.errors
    assert result.backend_calls_summary["total"] == 0


def test_artifact_path_escape_after_plan_is_rejected(tmp_path: Path):
    migration_input, plan = _published_input_and_plan(tmp_path)
    outside = tmp_path.parent / "outside-executor.pdf"
    outside.write_bytes(migration_input.publication.artifact.local_artifact_path.read_bytes())
    escaped_artifact = dataclasses.replace(migration_input.publication.artifact, local_artifact_path=outside)
    escaped_pub = dataclasses.replace(migration_input.publication, artifact=escaped_artifact)
    escaped_input = dataclasses.replace(migration_input, publication=escaped_pub)
    result = _executor().execute(escaped_input, plan)
    assert result.execution_status == "blocked_before_execution"
    assert "artifact_path_outside_root" in result.errors
    assert result.backend_calls_summary["total"] == 0


def test_published_copy_strong_success_uses_verified_asset_without_upload(tmp_path: Path):
    migration_input, plan = _published_input_and_plan(tmp_path)
    storage = FakeLegacyArtifactStorage()
    result = _executor(storage=storage).execute(migration_input, plan)
    assert result.execution_status == "completed"
    assert result.final_migration_state == "completed"
    assert result.final_target_state == "community"
    assert result.storage_action == "reuse_verified_remote_asset"
    assert storage.upload_count == 0
    assert "attach_migration_asset" in [op.operation_type for op in result.completed_operations]
    assert all(op.status == "completed" and op.response_received for op in result.completed_operations)


def test_published_copy_partial_cannot_execute_even_if_called_directly(tmp_path: Path):
    migration_input, plan = _published_input_and_plan(tmp_path, remote_asset_verification="partial")
    result = _executor().execute(migration_input, plan)
    assert result.execution_status == "blocked_before_execution"
    assert "plan_not_executable" in result.errors
    assert result.backend_calls_summary["total"] == 0


def test_draft_recovery_success_uploads_to_fake_storage_and_stays_draft(tmp_path: Path):
    migration_input, plan = _draft_input_and_plan(tmp_path)
    storage = FakeLegacyArtifactStorage()
    targets = FakeLegacyTargetBackend()
    result = _executor(storage=storage, targets=targets).execute(migration_input, plan)
    assert result.execution_status == "completed"
    assert result.final_migration_state == "recovery_completed"
    assert result.final_target_state == "draft"
    assert result.storage_action == "fake_upload"
    assert storage.upload_count == 1
    assert storage.objects
    assert all(chapter["status"] == "draft" for chapter in targets.chapters.values())
    forbidden = ("publish", "promote", "community", "authorize_draft_recovery_publication")
    serialized = json.dumps(result.to_json_dict(), sort_keys=True)
    assert not any(word in serialized for word in forbidden)


def test_executor_has_no_authorization_backend_surface(tmp_path: Path):
    migration_input, plan = _draft_input_and_plan(tmp_path)
    executor = _executor()
    assert not hasattr(executor, "authorize_draft_recovery_publication")
    result = executor.execute(migration_input, plan)
    assert "authorize" not in " ".join(op.operation_type for op in result.completed_operations)


@pytest.mark.parametrize(
    "operation_type",
    [
        "register_legacy_source",
        "claim_legacy_publication",
        "attach_migration_work",
        "attach_migration_chapter",
        "attach_migration_asset",
        "complete_legacy_migration",
    ],
)
def test_backend_operations_are_idempotent_on_rerun(tmp_path: Path, operation_type: str):
    migration_input, plan = _published_input_and_plan(tmp_path)
    provenance = FakeLegacyProvenanceBackend()
    targets = FakeLegacyTargetBackend()
    storage = FakeLegacyArtifactStorage()
    executor = _executor(provenance=provenance, targets=targets, storage=storage)
    first = executor.execute(migration_input, plan)
    second = executor.execute(migration_input, plan)
    assert first.execution_status == "completed"
    assert second.execution_status == "completed"
    snapshot = provenance.snapshot()
    assert len(snapshot["sources"]) == 1
    assert len(snapshot["migrations"]) == 1
    assert snapshot["operation_effect_counts"][operation_type] == 1
    assert all(op.attempt >= 1 for op in second.completed_operations)


def test_fake_upload_is_idempotent_for_same_plan_and_content(tmp_path: Path):
    migration_input, plan = _draft_input_and_plan(tmp_path)
    storage = FakeLegacyArtifactStorage()
    executor = _executor(storage=storage)
    first = executor.execute(migration_input, plan)
    second = executor.execute(migration_input, plan)
    assert first.execution_status == "completed"
    assert second.execution_status == "completed"
    assert storage.upload_count == 1
    assert len(storage.objects) == 1


@pytest.mark.parametrize("operation_type", ["register_legacy_source", "claim_legacy_publication", "attach_migration_asset", "complete_legacy_migration"])
def test_response_lost_recovery_is_idempotent(tmp_path: Path, operation_type: str):
    migration_input, plan = _published_input_and_plan(tmp_path)
    provenance = FakeLegacyProvenanceBackend()
    provenance.inject_response_lost(operation_type, after_calls=1)
    result = _executor(provenance=provenance).execute(migration_input, plan)
    assert result.execution_status == "completed"
    assert any(op.operation_type == operation_type and op.attempt == 2 for op in result.completed_operations)
    assert provenance.snapshot()["operation_effect_counts"][operation_type] == 1


def test_transient_failure_retries_with_bounded_attempts(tmp_path: Path):
    migration_input, plan = _published_input_and_plan(tmp_path)
    provenance = FakeLegacyProvenanceBackend()
    provenance.inject_failure("attach_migration_chapter", TransientBackendError("temporary"), before_mutation=True, times=1)
    result = _executor(provenance=provenance, max_attempts=3).execute(migration_input, plan)
    assert result.execution_status == "completed"
    chapter_ops = [op for op in result.completed_operations if op.operation_type == "attach_migration_chapter"]
    assert chapter_ops and chapter_ops[-1].attempt == 2


def test_terminal_failure_does_not_retry(tmp_path: Path):
    migration_input, plan = _published_input_and_plan(tmp_path)
    provenance = FakeLegacyProvenanceBackend()
    provenance.inject_failure("attach_migration_work", TerminalBackendError("terminal"), before_mutation=True, times=1)
    result = _executor(provenance=provenance, max_attempts=3).execute(migration_input, plan)
    assert result.execution_status == "failed_terminal"
    assert result.failed_operation == "attach_migration_work"
    assert result.retryable is False
    assert [op.operation_type for op in result.completed_operations].count("attach_migration_work") == 0


def test_resume_after_mid_flow_failure_does_not_duplicate_prior_steps(tmp_path: Path):
    migration_input, plan = _published_input_and_plan(tmp_path)
    provenance = FakeLegacyProvenanceBackend()
    provenance.inject_failure("attach_migration_chapter", TransientBackendError("temporary"), before_mutation=True, times=99)
    first = _executor(provenance=provenance, max_attempts=1).execute(migration_input, plan)
    assert first.execution_status == "incomplete_resume_required"
    provenance.clear_failures()
    second = _executor(provenance=provenance).resume(migration_input, plan, first.resume_state)
    assert second.execution_status == "completed"
    counts = provenance.snapshot()["operation_effect_counts"]
    assert counts["register_legacy_source"] == 1
    assert counts["claim_legacy_publication"] == 1
    assert counts["attach_migration_work"] == 1


def test_process_restart_style_resume_with_same_fake_backends(tmp_path: Path):
    migration_input, plan = _published_input_and_plan(tmp_path)
    provenance = FakeLegacyProvenanceBackend()
    targets = FakeLegacyTargetBackend()
    storage = FakeLegacyArtifactStorage()
    provenance.inject_failure("attach_migration_asset", TransientBackendError("temporary"), before_mutation=True, times=99)
    first = _executor(provenance=provenance, targets=targets, storage=storage, max_attempts=1).execute(migration_input, plan)
    provenance.clear_failures()
    restarted_executor = _executor(provenance=provenance, targets=targets, storage=storage)
    second = restarted_executor.resume(migration_input, plan, first.resume_state)
    assert second.execution_status == "completed"
    assert provenance.snapshot()["operation_effect_counts"]["claim_legacy_publication"] == 1


def test_already_completed_rerun_returns_completed_without_duplicate_effects(tmp_path: Path):
    migration_input, plan = _published_input_and_plan(tmp_path)
    provenance = FakeLegacyProvenanceBackend()
    executor = _executor(provenance=provenance)
    assert executor.execute(migration_input, plan).execution_status == "completed"
    assert executor.execute(migration_input, plan).execution_status == "completed"
    assert provenance.snapshot()["operation_effect_counts"]["complete_legacy_migration"] == 1


def test_target_conflict_stops_without_creating_alternative_target(tmp_path: Path):
    migration_input, plan = _published_input_and_plan(
        tmp_path,
        target={"work": {"state": "target_existing_compatible", "target_id": "50000000-0000-4000-8000-000000000001", "provenance": "strong"}, "chapter": {"state": "target_existing_conflict", "target_id": "60000000-0000-4000-8000-000000000001", "provenance": "weak"}},
    )
    assert plan.readiness == "blocked_target_collision"
    result = _executor().execute(migration_input, plan)
    assert result.execution_status == "blocked_before_execution"
    assert result.backend_calls_summary["total"] == 0


@pytest.mark.parametrize("kind", ["sha256", "size"])
def test_storage_metadata_mismatch_blocks_attach_and_complete(tmp_path: Path, kind: str):
    migration_input, plan = _draft_input_and_plan(tmp_path)
    storage = FakeLegacyArtifactStorage(mismatch=kind)
    provenance = FakeLegacyProvenanceBackend()
    result = _executor(provenance=provenance, storage=storage).execute(migration_input, plan)
    assert result.execution_status == "failed_terminal"
    assert "storage_integrity_mismatch" in result.errors
    assert "attach_migration_asset" not in provenance.snapshot()["operation_effect_counts"]
    assert "complete_legacy_migration" not in provenance.snapshot()["operation_effect_counts"]


def test_existing_identical_storage_object_reused_and_conflicting_object_blocked(tmp_path: Path):
    migration_input, plan = _draft_input_and_plan(tmp_path)
    storage = FakeLegacyArtifactStorage()
    first = _executor(storage=storage).execute(migration_input, plan)
    assert first.execution_status == "completed"
    assert _executor(storage=storage).execute(migration_input, plan).execution_status == "completed"
    assert storage.upload_count == 1
    storage.inject_conflicting_existing_object(plan.plan_digest)
    conflict = _executor(storage=storage).execute(migration_input, plan)
    assert conflict.execution_status == "failed_terminal"
    assert "storage_object_conflict" in conflict.errors


def test_same_publication_conflicting_plan_is_blocked(tmp_path: Path):
    migration_input, plan = _published_input_and_plan(tmp_path)
    provenance = FakeLegacyProvenanceBackend()
    executor = _executor(provenance=provenance)
    assert executor.execute(migration_input, plan).execution_status == "completed"
    changed_fixture = _published_fixture(tmp_path, source_record_digest="digest-materially-different")
    changed_input, changed_plan = _input_and_plan(tmp_path, changed_fixture)
    result = executor.execute(changed_input, changed_plan)
    assert result.execution_status == "failed_terminal"
    assert "conflicting_plan_digest" in result.errors


def test_source_snapshot_conflict_behavior(tmp_path: Path):
    migration_input, plan = _published_input_and_plan(tmp_path)
    provenance = FakeLegacyProvenanceBackend()
    executor = _executor(provenance=provenance)
    assert executor.execute(migration_input, plan).execution_status == "completed"
    changed_fixture = _published_fixture(tmp_path)
    changed_fixture["source"]["initial_snapshot_sha256"] = "b" * 64
    changed_input, changed_plan = _input_and_plan(tmp_path, changed_fixture)
    result = executor.execute(changed_input, changed_plan)
    assert result.execution_status == "failed_terminal"
    assert "source_snapshot_conflict" in result.errors


def test_interleaved_duplicate_execution_has_one_canonical_identity(tmp_path: Path):
    migration_input, plan = _published_input_and_plan(tmp_path)
    provenance = FakeLegacyProvenanceBackend()
    targets = FakeLegacyTargetBackend()
    storage = FakeLegacyArtifactStorage()
    first = _executor(provenance=provenance, targets=targets, storage=storage).execute(migration_input, plan)
    second = _executor(provenance=provenance, targets=targets, storage=storage).execute(migration_input, plan)
    assert first.execution_status == "completed"
    assert second.execution_status == "completed"
    snapshot = provenance.snapshot()
    assert len(snapshot["sources"]) == 1
    assert len(snapshot["migrations"]) == 1


def test_event_and_local_audit_journal_use_real_event_types(tmp_path: Path):
    migration_input, plan = _published_input_and_plan(tmp_path)
    provenance = FakeLegacyProvenanceBackend()
    result = _executor(provenance=provenance).execute(migration_input, plan)
    real_event_types = {
        "source_registered",
        "migration_claimed",
        "work_attached",
        "chapter_attached",
        "asset_attached",
        "migration_completed",
    }
    backend_events = [event["event_type"] for event in provenance.snapshot()["events"]]
    assert set(backend_events) <= real_event_types
    assert real_event_types <= set(backend_events)
    assert result.audit_journal[0]["event"] == "execution_started"


def test_executor_makes_no_network_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def fail_network(*args, **kwargs):
        raise AssertionError("network attempted")

    monkeypatch.setattr(socket.socket, "connect", fail_network)
    monkeypatch.setattr(socket, "create_connection", fail_network)
    migration_input, plan = _published_input_and_plan(tmp_path)
    assert _executor().execute(migration_input, plan).execution_status == "completed"


def test_executor_does_not_depend_on_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for key in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_OAUTH_CLIENT_ID"):
        monkeypatch.setenv(key, "must-not-be-read")
    migration_input, plan = _draft_input_and_plan(tmp_path)
    assert _executor().execute(migration_input, plan).execution_status == "completed"


def test_executor_does_not_write_real_filesystem_outside_fixture(tmp_path: Path):
    migration_input, plan = _draft_input_and_plan(tmp_path)
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    result = _executor().execute(migration_input, plan)
    after = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    assert result.execution_status == "completed"
    assert before == after


def test_cli_remains_dry_run_only(tmp_path: Path):
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(_published_fixture(tmp_path)), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "legacy_publication_migrator", "--fixture", str(fixture), "--artifact-root", str(tmp_path)],
        cwd=Path(__file__).parent,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "execution_not_enabled" in result.stderr
    assert "--execute" not in result.stderr


def test_operation_allowlist_rejects_unknown_planned_operation(tmp_path: Path):
    migration_input, plan = _published_input_and_plan(tmp_path)
    bad_plan = dataclasses.replace(
        plan,
        planned_operations=[*plan.planned_operations, {"operation_type": "drop_everything", "sequence": 99, "executed": False}],
    )
    bad_plan = dataclasses.replace(
        bad_plan,
        plan_digest=hashlib.sha256(canonical_plan_json(bad_plan.to_json_dict(), include_digest=False).encode()).hexdigest(),
    )
    result = _executor().execute(migration_input, bad_plan)
    assert result.execution_status == "blocked_before_execution"
    assert "unknown_planned_operation" in result.errors
    assert result.backend_calls_summary["total"] == 0
