"""Persistent, cross-process reservation/lease for the legacy storage upload race.

Closes the gap demonstrated by
``test_concurrent_narrow_lookup_race_can_duplicate_without_external_lock``
(``test_legacy_publication_drive_adapter.py``): two workers can each independently observe zero
matches from a storage provider's own idempotency lookup before either create is visible to the
other, because nothing previously held a lock across that network round trip. This module adds:

- ``LegacyArtifactUploadReservationBackend`` — the protocol the executor-facing storage wrapper
  depends on. It never mentions PostgreSQL.
- ``PostgresLegacyStorageUploadReservationBackend`` — the only implementation, calling the four
  SECURITY DEFINER functions added by
  ``supabase/migrations/20260807140000_legacy_storage_upload_reservations.sql``.
- ``ReservedLegacyArtifactStorage`` — a ``LegacyArtifactStorage`` implementation that wraps any
  other ``LegacyArtifactStorage`` (e.g. ``GoogleDriveLegacyArtifactStorage``, left completely
  untouched) and a reservation backend, and enforces: claim -> (busy/conflict short-circuit) ->
  mark_create_started (committed) -> delegate create to the wrapped storage (whose own narrow
  idempotency lookup always runs before its create, satisfying the mandatory recovery lookup for
  free) -> complete. This is a decorator, not a change to the wrapped storage or to
  ``LegacyMigrationExecutor`` -- both stay exactly as validated in commits
  40a3cc2/e1d4a93/f6c12da.

What this module does NOT claim: the lease cannot cancel an HTTP request already in flight to a
real storage provider. ``complete`` being rejected as ``stale_lease`` after a takeover turns that
residual risk into a detectable, ``ResponseLostError`` outcome instead of a silent double-create
being trusted -- it does not eliminate the underlying physical possibility. See
``minimum_safe_create_lease_ttl`` in ``legacy_publication_drive_adapter.py`` for the deadline
relationship this lease's TTLs are sized against.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row

from legacy_publication_drive_adapter import derive_idempotency_key
from legacy_publication_migration_executor import (
    ConflictingStorageObjectError,
    LegacyExecutorError,
    LegacyProvenanceBackend,
    ResponseLostError,
    TerminalBackendError,
    TransientBackendError,
    ValidationError,
)
from legacy_publication_migrator import DryRunResult, LegacyMigrationInput
from legacy_publication_postgres_adapter import (
    OPERATOR_ROLE,
    LocalPostgresConnectionSettings,
    map_postgres_error,
)


def compute_storage_scope_key(storage_provider: str, storage_scope_raw: str) -> str:
    """Provider-agnostic fingerprint of an upload destination (drive_adapter_integration.md).

    Kept out of the UNIQUE tuple's raw form so a future non-Drive provider's own scope-string
    shape (arbitrary length, arbitrary characters) never has to fit the reservation schema.
    """
    return hashlib.sha256(f"{storage_provider}:{storage_scope_raw}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReservationClaim:
    reservation_id: str
    lease_holder_token: str | None
    lease_generation: int
    lease_expires_at: Any
    status: str  # acquired | busy | recovery_required | already_completed | conflict
    storage_file_id: str | None
    reservation_state: str


@dataclass(frozen=True)
class ReservationStarted:
    status: str  # started | already_in_flight_same_lease | already_completed | stale_lease
    lease_expires_at: Any


@dataclass(frozen=True)
class ReservationCompletion:
    status: str  # completed | already_completed | stale_lease | integrity_mismatch
    storage_file_id: str | None


@dataclass(frozen=True)
class ReservationFailureRecorded:
    status: str  # failed_retryable | failed_terminal | stale_lease | conflict


class LegacyArtifactUploadReservationBackend(Protocol):
    def claim(
        self,
        *,
        migration_id: str,
        storage_provider: str,
        storage_scope_key: str,
        idempotency_key: str,
        expected_sha256: str,
        expected_size: int,
        storage_scope_raw: str | None = None,
    ) -> ReservationClaim: ...

    def mark_create_started(
        self, *, reservation_id: str, lease_holder_token: str, lease_generation: int
    ) -> ReservationStarted: ...

    def complete(
        self,
        *,
        reservation_id: str,
        lease_holder_token: str,
        lease_generation: int,
        storage_file_id: str,
        actual_sha256: str,
        actual_size: int,
    ) -> ReservationCompletion: ...

    def mark_failure(
        self,
        *,
        reservation_id: str,
        lease_holder_token: str,
        lease_generation: int,
        error_code: str,
        outcome: str,
    ) -> ReservationFailureRecorded: ...


class PostgresLegacyStorageUploadReservationBackend:
    """LegacyArtifactUploadReservationBackend backed by the local-only reservation SQL functions.

    Same discipline as ``PostgresLegacyProvenanceBackend``: local-host-only ``settings``, one
    ``SET ROLE migration_operator`` per call, an explicit allowlist of the exact four functions
    this class ever executes, no dynamic SQL, no string-built queries.
    """

    def __init__(self, settings: LocalPostgresConnectionSettings) -> None:
        self.settings = settings
        self.call_count = 0

    def _connect(self):
        try:
            return psycopg.connect(self.settings.dsn, autocommit=True, row_factory=dict_row)
        except psycopg.Error as exc:
            raise map_postgres_error(exc) from exc

    def _call(self, statement: str, params: tuple[Any, ...]) -> dict[str, Any]:
        self.call_count += 1
        try:
            with self._connect() as conn:
                conn.execute(f"set role {OPERATOR_ROLE}")
                row = conn.execute(statement, params).fetchone()
                return dict(row)
        except psycopg.Error as exc:
            raise map_postgres_error(exc) from exc

    def claim(
        self,
        *,
        migration_id: str,
        storage_provider: str,
        storage_scope_key: str,
        idempotency_key: str,
        expected_sha256: str,
        expected_size: int,
        storage_scope_raw: str | None = None,
    ) -> ReservationClaim:
        row = self._call(
            "select reservation_id::text, lease_holder_token::text, lease_generation, "
            "lease_expires_at, status, storage_file_id, reservation_state "
            "from private.claim_legacy_storage_upload(%s::uuid,%s::text,%s::text,%s::text,%s::text,%s::bigint,%s::text)",
            (migration_id, storage_provider, storage_scope_key, idempotency_key, expected_sha256, expected_size, storage_scope_raw),
        )
        return ReservationClaim(
            reservation_id=str(row["reservation_id"]),
            lease_holder_token=str(row["lease_holder_token"]) if row["lease_holder_token"] else None,
            lease_generation=int(row["lease_generation"]),
            lease_expires_at=row["lease_expires_at"],
            status=str(row["status"]),
            storage_file_id=row["storage_file_id"],
            reservation_state=str(row["reservation_state"]),
        )

    def mark_create_started(self, *, reservation_id: str, lease_holder_token: str, lease_generation: int) -> ReservationStarted:
        row = self._call(
            "select status, lease_expires_at from private.mark_storage_create_started(%s::uuid,%s::uuid,%s::bigint)",
            (reservation_id, lease_holder_token, lease_generation),
        )
        return ReservationStarted(status=str(row["status"]), lease_expires_at=row["lease_expires_at"])

    def complete(
        self,
        *,
        reservation_id: str,
        lease_holder_token: str,
        lease_generation: int,
        storage_file_id: str,
        actual_sha256: str,
        actual_size: int,
    ) -> ReservationCompletion:
        row = self._call(
            "select status, storage_file_id from private.complete_legacy_storage_upload(%s::uuid,%s::uuid,%s::bigint,%s::text,%s::text,%s::bigint)",
            (reservation_id, lease_holder_token, lease_generation, storage_file_id, actual_sha256, actual_size),
        )
        return ReservationCompletion(status=str(row["status"]), storage_file_id=row["storage_file_id"])

    def mark_failure(
        self,
        *,
        reservation_id: str,
        lease_holder_token: str,
        lease_generation: int,
        error_code: str,
        outcome: str,
    ) -> ReservationFailureRecorded:
        row = self._call(
            "select status from private.mark_legacy_storage_upload_failure(%s::uuid,%s::uuid,%s::bigint,%s::text,%s::text)",
            (reservation_id, lease_holder_token, lease_generation, error_code, outcome),
        )
        return ReservationFailureRecorded(status=str(row["status"]))


class ReservedLegacyArtifactStorage:
    """``LegacyArtifactStorage`` decorator that reserves a persistent lease before delegating.

    Wraps an existing ``LegacyArtifactStorage`` (never modifies it) plus a
    ``LegacyArtifactUploadReservationBackend`` and a narrow slice of the provenance backend
    (``claim_legacy_publication``, already idempotent and already called elsewhere in the
    executor's own operation sequence) used only to resolve ``migration_id`` -- the reservation
    table's required audit FK. This keeps ``LegacyMigrationExecutor`` and the wrapped storage
    (e.g. ``GoogleDriveLegacyArtifactStorage``) both untouched: this class satisfies the exact
    same two-argument ``upload(migration_input, plan)`` shape they already use.
    """

    def __init__(
        self,
        *,
        storage: Any,
        reservation_backend: LegacyArtifactUploadReservationBackend,
        provenance_backend: LegacyProvenanceBackend,
        storage_provider: str,
        storage_scope_raw: str,
    ) -> None:
        self._storage = storage
        self._reservation = reservation_backend
        self._provenance = provenance_backend
        self._storage_provider = storage_provider
        self._storage_scope_raw = storage_scope_raw
        self._storage_scope_key = compute_storage_scope_key(storage_provider, storage_scope_raw)
        # ponytail: per-instance cache so a same-process retry (LegacyMigrationExecutor's own
        # attempt loop calling upload() fresh each attempt) reuses the lease it already holds
        # instead of re-claiming and seeing its own still-live lease reported back as "busy".
        # Correctness never depends on this cache -- the DB fencing check (lease_generation +
        # lease_holder_token) is authoritative regardless of whether this dict is ever populated.
        # Upgrade to a persisted/shared cache only if a future caller needs cross-instance reuse.
        self._held: dict[str, ReservationClaim] = {}

    @property
    def upload_count(self) -> int:
        return getattr(self._storage, "upload_count", 0)

    def has_conflict(self, plan_digest: str) -> bool:
        return self._storage.has_conflict(plan_digest)

    def use_verified_remote_asset(self, migration_input: LegacyMigrationInput, plan: DryRunResult) -> dict[str, Any]:
        return self._storage.use_verified_remote_asset(migration_input, plan)

    def upload(self, migration_input: LegacyMigrationInput, plan: DryRunResult) -> dict[str, Any]:
        artifact = migration_input.publication.artifact
        idempotency_key = derive_idempotency_key(migration_input, plan)
        migration_row = self._provenance.claim_legacy_publication(migration_input, plan)
        migration_id = migration_row["migration_id"]

        claim = self._held.get(idempotency_key)
        if claim is None:
            claim = self._reservation.claim(
                migration_id=migration_id,
                storage_provider=self._storage_provider,
                storage_scope_key=self._storage_scope_key,
                idempotency_key=idempotency_key,
                expected_sha256=artifact.pdf_sha256,
                expected_size=artifact.pdf_size,
                storage_scope_raw=self._storage_scope_raw,
            )

        if claim.status == "already_completed":
            self._held.pop(idempotency_key, None)
            return {
                "storage_provider": self._storage_provider,
                "storage_file_id": claim.storage_file_id,
                "size": artifact.pdf_size,
                "sha256": artifact.pdf_sha256,
                "action": "reuse_reservation_completed",
            }
        if claim.status == "busy":
            # Never call the underlying storage's create while another worker's lease is live.
            raise TransientBackendError("storage_reservation_busy")
        if claim.status == "conflict":
            raise ConflictingStorageObjectError("storage_reservation_conflict")
        if claim.status not in ("acquired", "recovery_required"):
            raise TerminalBackendError(f"storage_reservation_unexpected_status:{claim.status}")

        self._held[idempotency_key] = claim
        started = self._reservation.mark_create_started(
            reservation_id=claim.reservation_id,
            lease_holder_token=claim.lease_holder_token,
            lease_generation=claim.lease_generation,
        )
        if started.status == "stale_lease":
            self._held.pop(idempotency_key, None)
            raise TransientBackendError("storage_reservation_stale_lease")
        if started.status == "already_completed":
            self._held.pop(idempotency_key, None)
            raise TransientBackendError("storage_reservation_already_completed_race")

        # create_in_flight is now committed. Any failure past this point -- including the
        # wrapped storage's own internal narrow-lookup-then-create -- is outcome-unknown from
        # this reservation's point of view (crash_windows.md windows C-G), never "known not
        # created". The wrapped storage's upload() always performs its narrow idempotency lookup
        # before attempting a create, which is exactly the mandatory recovery lookup a
        # recovery_required claim requires -- no separate call needed.
        try:
            asset = self._storage.upload(migration_input, plan)
        except LegacyExecutorError as exc:
            try:
                self._reservation.mark_failure(
                    reservation_id=claim.reservation_id,
                    lease_holder_token=claim.lease_holder_token,
                    lease_generation=claim.lease_generation,
                    error_code=type(exc).__name__.lower(),
                    outcome="unknown_after_create_attempt",
                )
            except LegacyExecutorError:
                pass
            raise

        completion = self._reservation.complete(
            reservation_id=claim.reservation_id,
            lease_holder_token=claim.lease_holder_token,
            lease_generation=claim.lease_generation,
            storage_file_id=asset["storage_file_id"],
            actual_sha256=asset["sha256"],
            actual_size=asset["size"],
        )
        self._held.pop(idempotency_key, None)
        if completion.status == "stale_lease":
            # Our lease expired mid-flight and another worker took over: the DB refused our
            # completion. The physical object this call just created may be the very one the new
            # holder reuses (or already reused) -- genuinely ambiguous from here, never a silent
            # success. See external_side_effect_race.md for the residual physical limitation this
            # represents (a caller-side lease cannot cancel a request already in flight).
            raise ResponseLostError("storage_reservation_completion_stale_lease")
        if completion.status == "integrity_mismatch":
            raise TerminalBackendError("storage_reservation_integrity_mismatch")
        if completion.status not in ("completed", "already_completed"):
            raise TerminalBackendError(f"storage_reservation_unexpected_completion:{completion.status}")
        return asset
