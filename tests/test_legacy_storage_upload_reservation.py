"""Fast, hermetic tests for ReservedLegacyArtifactStorage's orchestration logic.

Uses a fully scriptable in-memory fake reservation backend so these tests exercise the wrapper's
branching (busy/conflict/already_completed/recovery_required/stale_lease/integrity_mismatch)
without Docker or a real network. The SQL state machine itself (the four functions this fake
mimics) is proven separately, against a real local PostgreSQL, in
test_legacy_storage_upload_reservation_postgres.py.
"""

from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import uuid
from pathlib import Path
from typing import Any

import pytest

from legacy_publication_migration_executor import (
    ConflictingStorageObjectError,
    FakeLegacyArtifactStorage,
    FakeLegacyProvenanceBackend,
    ResponseLostError,
    TerminalBackendError,
    TransientBackendError,
)
from legacy_publication_migrator import load_migration_input, plan_migration
from legacy_storage_upload_reservation import (
    ReservationClaim,
    ReservationCompletion,
    ReservationFailureRecorded,
    ReservationStarted,
    ReservedLegacyArtifactStorage,
    compute_storage_scope_key,
)
from test_legacy_publication_migrator_dry_run import _draft_fixture


def _input_and_plan(root: Path, **overrides: Any):
    root.mkdir(parents=True, exist_ok=True)
    overrides.setdefault("legacy_publication_id", "TEST-PUB-RES-001")
    import json

    fixture_path = root / "res-fixture.json"
    fixture_path.write_text(json.dumps(_draft_fixture(root, **overrides)), encoding="utf-8")
    migration_input = load_migration_input(fixture_path, artifact_root=root)
    return migration_input, plan_migration(migration_input)


class ScriptedReservationBackend:
    """A reservation backend whose responses are pre-scripted per call, in order."""

    def __init__(self) -> None:
        self.claim_calls: list[dict[str, Any]] = []
        self.started_calls: list[dict[str, Any]] = []
        self.complete_calls: list[dict[str, Any]] = []
        self.failure_calls: list[dict[str, Any]] = []
        self._claim_script: list[ReservationClaim] = []
        self._started_script: list[ReservationStarted] = []
        self._complete_script: list[ReservationCompletion] = []
        self._failure_script: list[ReservationFailureRecorded] = []

    def script_claim(self, result: ReservationClaim) -> None:
        self._claim_script.append(result)

    def script_started(self, result: ReservationStarted) -> None:
        self._started_script.append(result)

    def script_complete(self, result: ReservationCompletion) -> None:
        self._complete_script.append(result)

    def script_failure(self, result: ReservationFailureRecorded) -> None:
        self._failure_script.append(result)

    def claim(self, **kwargs: Any) -> ReservationClaim:
        self.claim_calls.append(kwargs)
        return self._claim_script.pop(0)

    def mark_create_started(self, **kwargs: Any) -> ReservationStarted:
        self.started_calls.append(kwargs)
        return self._started_script.pop(0)

    def complete(self, **kwargs: Any) -> ReservationCompletion:
        self.complete_calls.append(kwargs)
        return self._complete_script.pop(0)

    def mark_failure(self, **kwargs: Any) -> ReservationFailureRecorded:
        self.failure_calls.append(kwargs)
        return self._failure_script.pop(0)


def _acquired_claim(**overrides: Any) -> ReservationClaim:
    base = dict(
        reservation_id=str(uuid.uuid4()),
        lease_holder_token=str(uuid.uuid4()),
        lease_generation=1,
        lease_expires_at=None,
        status="acquired",
        storage_file_id=None,
        reservation_state="reserved",
    )
    base.update(overrides)
    return ReservationClaim(**base)


def test_compute_storage_scope_key_is_deterministic_and_provider_scoped():
    a = compute_storage_scope_key("google_drive", "folder-123")
    b = compute_storage_scope_key("google_drive", "folder-123")
    c = compute_storage_scope_key("google_drive", "folder-456")
    d = compute_storage_scope_key("s3", "folder-123")
    assert a == b
    assert a != c
    assert a != d


def test_busy_never_calls_underlying_storage_create(tmp_path: Path):
    migration_input, plan = _input_and_plan(tmp_path)
    provenance = FakeLegacyProvenanceBackend()
    storage = FakeLegacyArtifactStorage()
    reservation = ScriptedReservationBackend()
    reservation.script_claim(_acquired_claim(status="busy", lease_holder_token=None))
    wrapper = ReservedLegacyArtifactStorage(
        storage=storage, reservation_backend=reservation, provenance_backend=provenance,
        storage_provider="google_drive", storage_scope_raw="folder-abc",
    )
    with pytest.raises(TransientBackendError):
        wrapper.upload(migration_input, plan)
    assert storage.upload_count == 0
    assert not reservation.started_calls


def test_conflict_raises_without_calling_storage(tmp_path: Path):
    migration_input, plan = _input_and_plan(tmp_path)
    provenance = FakeLegacyProvenanceBackend()
    storage = FakeLegacyArtifactStorage()
    reservation = ScriptedReservationBackend()
    reservation.script_claim(_acquired_claim(status="conflict", lease_holder_token=None))
    wrapper = ReservedLegacyArtifactStorage(
        storage=storage, reservation_backend=reservation, provenance_backend=provenance,
        storage_provider="google_drive", storage_scope_raw="folder-abc",
    )
    with pytest.raises(ConflictingStorageObjectError):
        wrapper.upload(migration_input, plan)
    assert storage.upload_count == 0


def test_already_completed_short_circuits_without_calling_storage(tmp_path: Path):
    migration_input, plan = _input_and_plan(tmp_path)
    provenance = FakeLegacyProvenanceBackend()
    storage = FakeLegacyArtifactStorage()
    reservation = ScriptedReservationBackend()
    reservation.script_claim(_acquired_claim(status="already_completed", lease_holder_token=None, storage_file_id="FILE-1"))
    wrapper = ReservedLegacyArtifactStorage(
        storage=storage, reservation_backend=reservation, provenance_backend=provenance,
        storage_provider="google_drive", storage_scope_raw="folder-abc",
    )
    result = wrapper.upload(migration_input, plan)
    assert result["storage_file_id"] == "FILE-1"
    assert result["action"] == "reuse_reservation_completed"
    assert storage.upload_count == 0


def test_acquired_flow_marks_started_then_uploads_then_completes(tmp_path: Path):
    migration_input, plan = _input_and_plan(tmp_path)
    provenance = FakeLegacyProvenanceBackend()
    storage = FakeLegacyArtifactStorage()
    reservation = ScriptedReservationBackend()
    claim = _acquired_claim()
    reservation.script_claim(claim)
    reservation.script_started(ReservationStarted(status="started", lease_expires_at=None))
    reservation.script_complete(ReservationCompletion(status="completed", storage_file_id="will-be-overwritten"))
    wrapper = ReservedLegacyArtifactStorage(
        storage=storage, reservation_backend=reservation, provenance_backend=provenance,
        storage_provider="google_drive", storage_scope_raw="folder-abc",
    )
    result = wrapper.upload(migration_input, plan)
    assert storage.upload_count == 1
    assert reservation.started_calls[0]["reservation_id"] == claim.reservation_id
    assert reservation.complete_calls[0]["storage_file_id"] == result["storage_file_id"]
    assert not reservation.failure_calls


def test_recovery_required_still_goes_through_wrapped_storages_own_lookup(tmp_path: Path):
    """recovery_required proceeds like acquired: the wrapped storage's upload() always looks up
    before creating, so no separate 'force lookup' call is needed (drive_adapter_integration.md).
    """
    migration_input, plan = _input_and_plan(tmp_path)
    provenance = FakeLegacyProvenanceBackend()
    storage = FakeLegacyArtifactStorage()
    reservation = ScriptedReservationBackend()
    claim = _acquired_claim(status="recovery_required", reservation_state="create_in_flight")
    reservation.script_claim(claim)
    reservation.script_started(ReservationStarted(status="started", lease_expires_at=None))
    reservation.script_complete(ReservationCompletion(status="completed", storage_file_id="X"))
    wrapper = ReservedLegacyArtifactStorage(
        storage=storage, reservation_backend=reservation, provenance_backend=provenance,
        storage_provider="google_drive", storage_scope_raw="folder-abc",
    )
    wrapper.upload(migration_input, plan)
    assert storage.upload_count == 1


def test_mark_create_started_stale_lease_raises_transient_without_calling_storage(tmp_path: Path):
    migration_input, plan = _input_and_plan(tmp_path)
    provenance = FakeLegacyProvenanceBackend()
    storage = FakeLegacyArtifactStorage()
    reservation = ScriptedReservationBackend()
    reservation.script_claim(_acquired_claim())
    reservation.script_started(ReservationStarted(status="stale_lease", lease_expires_at=None))
    wrapper = ReservedLegacyArtifactStorage(
        storage=storage, reservation_backend=reservation, provenance_backend=provenance,
        storage_provider="google_drive", storage_scope_raw="folder-abc",
    )
    with pytest.raises(TransientBackendError):
        wrapper.upload(migration_input, plan)
    assert storage.upload_count == 0


def test_storage_failure_after_mark_create_started_records_unknown_after_create_attempt(tmp_path: Path):
    migration_input, plan = _input_and_plan(tmp_path)
    provenance = FakeLegacyProvenanceBackend()
    storage = FakeLegacyArtifactStorage()
    storage.inject_conflicting_existing_object(plan.plan_digest)
    reservation = ScriptedReservationBackend()
    claim = _acquired_claim()
    reservation.script_claim(claim)
    reservation.script_started(ReservationStarted(status="started", lease_expires_at=None))
    reservation.script_failure(ReservationFailureRecorded(status="failed_retryable"))
    wrapper = ReservedLegacyArtifactStorage(
        storage=storage, reservation_backend=reservation, provenance_backend=provenance,
        storage_provider="google_drive", storage_scope_raw="folder-abc",
    )
    with pytest.raises(ConflictingStorageObjectError):
        wrapper.upload(migration_input, plan)
    assert reservation.failure_calls[0]["outcome"] == "unknown_after_create_attempt"
    assert reservation.failure_calls[0]["reservation_id"] == claim.reservation_id


def test_completion_stale_lease_raises_response_lost_never_silent_success(tmp_path: Path):
    migration_input, plan = _input_and_plan(tmp_path)
    provenance = FakeLegacyProvenanceBackend()
    storage = FakeLegacyArtifactStorage()
    reservation = ScriptedReservationBackend()
    reservation.script_claim(_acquired_claim())
    reservation.script_started(ReservationStarted(status="started", lease_expires_at=None))
    reservation.script_complete(ReservationCompletion(status="stale_lease", storage_file_id=None))
    wrapper = ReservedLegacyArtifactStorage(
        storage=storage, reservation_backend=reservation, provenance_backend=provenance,
        storage_provider="google_drive", storage_scope_raw="folder-abc",
    )
    with pytest.raises(ResponseLostError):
        wrapper.upload(migration_input, plan)


def test_completion_integrity_mismatch_raises_terminal(tmp_path: Path):
    migration_input, plan = _input_and_plan(tmp_path)
    provenance = FakeLegacyProvenanceBackend()
    storage = FakeLegacyArtifactStorage()
    reservation = ScriptedReservationBackend()
    reservation.script_claim(_acquired_claim())
    reservation.script_started(ReservationStarted(status="started", lease_expires_at=None))
    reservation.script_complete(ReservationCompletion(status="integrity_mismatch", storage_file_id=None))
    wrapper = ReservedLegacyArtifactStorage(
        storage=storage, reservation_backend=reservation, provenance_backend=provenance,
        storage_provider="google_drive", storage_scope_raw="folder-abc",
    )
    with pytest.raises(TerminalBackendError):
        wrapper.upload(migration_input, plan)


def test_same_process_retry_reuses_held_lease_instead_of_reclaiming(tmp_path: Path):
    """A same-instance second upload() call for the same identity must not call claim() again
    while a lease is already held in memory (would otherwise see its own lease as 'busy')."""
    migration_input, plan = _input_and_plan(tmp_path)
    provenance = FakeLegacyProvenanceBackend()
    storage = FakeLegacyArtifactStorage()
    reservation = ScriptedReservationBackend()
    claim = _acquired_claim()
    reservation.script_claim(claim)
    reservation.script_started(ReservationStarted(status="stale_lease", lease_expires_at=None))
    wrapper = ReservedLegacyArtifactStorage(
        storage=storage, reservation_backend=reservation, provenance_backend=provenance,
        storage_provider="google_drive", storage_scope_raw="folder-abc",
    )
    with pytest.raises(TransientBackendError):
        wrapper.upload(migration_input, plan)
    # claim() was popped once and rolled back to _held after mark_create_started failed with
    # stale_lease -- so the cache is cleared and a genuinely fresh claim happens next time.
    assert len(reservation.claim_calls) == 1
