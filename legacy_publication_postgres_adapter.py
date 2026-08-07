"""Local PostgreSQL adapter for the legacy publication migration runtime.

This module is intentionally a PostgreSQL function adapter, not a Supabase/Drive
adapter.  It refuses non-local hosts and exposes only the operational 2A2
functions used by :class:`LegacyMigrationExecutor`.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from legacy_publication_migration_executor import (
    ConflictError,
    LegacyExecutorError,
    ResponseLostError,
    TerminalBackendError,
    TransientBackendError,
    ValidationError,
)
from legacy_publication_migrator import DryRunResult, LegacyMigrationInput


OPERATOR_ROLE = "migration_operator"
ALLOWED_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
FORBIDDEN_OPERATIONAL_FUNCTIONS = frozenset({"authorize_draft_recovery_publication"})
ALLOWED_RUNTIME_FUNCTIONS: dict[str, str] = {
    "register_legacy_source": "select private.register_legacy_source(%s::jsonb) as source_instance_id",
    "claim_legacy_publication": (
        "select migration_id::text, result from private.claim_legacy_publication("
        "%s::uuid,%s::text,%s::int,%s::text,%s::text,%s::text,%s::uuid,%s::text,"
        "%s::text,%s::text,%s::text,%s::text,%s::bigint,%s::text,%s::text,%s::text,%s::text,%s::jsonb)"
    ),
    "attach_migration_work": "select private.attach_migration_work(%s::uuid,%s::uuid)",
    "attach_migration_chapter": "select private.attach_migration_chapter(%s::uuid,%s::uuid)",
    "attach_migration_asset": "select private.attach_migration_asset(%s::uuid,%s::uuid,%s::text,%s::bigint,%s::text,%s::text)",
    "mark_migration_failure": "select private.mark_migration_failure(%s::uuid,%s::text,%s::boolean)",
    "complete_legacy_migration": "select private.complete_legacy_migration(%s::uuid,%s::text) as migration_state",
    "append_legacy_migration_event": "select private.append_legacy_migration_event(%s::uuid,%s::text,%s::jsonb,%s::text) as event_id",
}


class RefuseRemotePostgresHostError(ValueError):
    """Raised when a local-only adapter is configured with a non-local host."""


def _is_local_host(host: str) -> bool:
    normalized = (host or "").strip().lower()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    return normalized in ALLOWED_LOCAL_HOSTS


@dataclass(frozen=True)
class LocalPostgresConnectionSettings:
    host: str
    port: int
    dbname: str
    user: str
    password: str | None = None

    def __post_init__(self) -> None:
        if not _is_local_host(self.host):
            raise RefuseRemotePostgresHostError(f"refusing non-local PostgreSQL host: {self.host}")
        if int(self.port) <= 0:
            raise ValueError("port_must_be_positive")

    @property
    def dsn(self) -> str:
        parts = {
            "host": self.host,
            "port": str(self.port),
            "dbname": self.dbname,
            "user": self.user,
        }
        if self.password is not None:
            parts["password"] = self.password
        return " ".join(f"{key}={value}" for key, value in parts.items())


def map_postgres_error(exc: BaseException) -> LegacyExecutorError:
    state = exc.sqlstate if isinstance(exc, psycopg.Error) else exc.__dict__.get("sqlstate", "")
    state = state or ""
    message = str(exc)
    if not state and any(fragment in message.lower() for fragment in ("connection timeout", "connection refused", "could not connect")):
        return TransientBackendError("postgres_connection_error")
    if state in {"23505", "PT001"}:
        return ConflictError(_error_code(message, "postgres_conflict"))
    if state.startswith("08") or state in {"40001", "40P01", "53300", "57P01", "57P02", "57P03"}:
        return TransientBackendError(_error_code(message, "postgres_transient"))
    if state in {"23502", "23503", "23514", "22023", "22P02", "42501", "55000", "P0002"}:
        return ValidationError(_error_code(message, "postgres_validation"))
    return TerminalBackendError(_error_code(message, "postgres_unknown_error"))


def _error_code(message: str, fallback: str) -> str:
    lowered = message.lower()
    if "source_snapshot_changed" in lowered:
        return "source_snapshot_conflict"
    if "different source registration" in lowered or "different source_system" in lowered:
        return "source_snapshot_conflict"
    if "different target_work_id" in lowered:
        return "target_work_conflict"
    if "different target_chapter_id" in lowered:
        return "target_chapter_conflict"
    if "colliding live migration" in lowered or "already carries provenance" in lowered:
        return "target_collision"
    if "not approved" in lowered or "permission denied" in lowered:
        return "permission_denied"
    return fallback


class PostgresLegacyProvenanceBackend:
    """LegacyProvenanceBackend implementation backed by local 2A2 SQL functions."""

    def __init__(
        self,
        settings: LocalPostgresConnectionSettings,
        *,
        response_lost_after_commit: set[str] | None = None,
    ) -> None:
        self.settings = settings
        self.call_count = 0
        self._response_lost_after_commit = set(response_lost_after_commit or set())
        self._lost_once: set[str] = set()
        self._sources: dict[str, dict[str, Any]] = {}
        self._migrations: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []

    def _connect(self, *, row_factory: Any | None = None):
        kwargs: dict[str, Any] = {"autocommit": True}
        if row_factory is not None:
            kwargs["row_factory"] = row_factory
        try:
            return psycopg.connect(self.settings.dsn, **kwargs)
        except psycopg.Error as exc:
            raise map_postgres_error(exc) from exc

    def _operator_call(self, operation_type: str, params: tuple[Any, ...], *, fetch: str = "one") -> Any:
        if operation_type not in ALLOWED_RUNTIME_FUNCTIONS:
            raise ValidationError("unknown_runtime_operation")
        self.call_count += 1
        try:
            with self._connect(row_factory=dict_row) as conn:
                conn.execute(f"set role {OPERATOR_ROLE}")
                cursor = conn.execute(ALLOWED_RUNTIME_FUNCTIONS[operation_type], params)
                if operation_type in self._response_lost_after_commit and operation_type not in self._lost_once:
                    self._lost_once.add(operation_type)
                    raise ResponseLostError(f"response_lost:{operation_type}")
                if fetch == "none":
                    return None
                return cursor.fetchone()
        except ResponseLostError:
            raise
        except psycopg.Error as exc:
            raise map_postgres_error(exc) from exc

    def register_legacy_source(self, migration_input: LegacyMigrationInput, plan: DryRunResult) -> dict[str, Any]:
        source = migration_input.source
        publication = migration_input.publication
        manifest = {
            "manifest_id": source.manifest_id,
            "proposed_source_instance_id": source.source_instance_id,
            "source_system": source.source_system,
            "source_schema_version": source.source_schema_version,
            "snapshot_sha256": source.initial_snapshot_sha256,
            "logical_fingerprint": source.initial_logical_fingerprint,
            "source_reference": source.metadata.get("source_reference", "synthetic-local-postgres"),
            "publication_count": 1,
            "source_record_digests": [publication.source_record_digest],
            "approval_state": source.approval_state,
            "approved_by": source.metadata.get("approved_by", "local-postgres-adapter-test"),
        }
        row = self._operator_call("register_legacy_source", (Jsonb(manifest),))
        result = {
            "source_instance_id": str(row["source_instance_id"]),
            "source_system": source.source_system,
            "initial_snapshot_sha256": source.initial_snapshot_sha256,
            "manifest_id": source.manifest_id,
            "source_state": "registered",
        }
        self._sources[result["source_instance_id"]] = result
        return result

    def claim_legacy_publication(self, migration_input: LegacyMigrationInput, plan: DryRunResult) -> dict[str, Any]:
        publication = migration_input.publication
        artifact = publication.artifact
        remote_asset = publication.remote_asset or {}
        row = self._operator_call(
            "claim_legacy_publication",
            (
                publication.source_instance_id,
                publication.legacy_publication_id,
                migration_input.source.source_schema_version,
                publication.source_record_digest,
                publication.migration_mode,
                publication.requested_target_status,
                publication.owner_id,
                publication.owner_resolution_state,
                publication.metadata.get("legacy_file_id", publication.legacy_publication_id),
                publication.source_job_id,
                publication.source_run_id,
                artifact.pdf_sha256,
                artifact.pdf_size,
                remote_asset.get("storage_provider"),
                remote_asset.get("storage_file_id"),
                remote_asset.get("storage_folder_id"),
                migration_input.source.initial_snapshot_sha256,
                Jsonb({"plan_digest": plan.plan_digest, "idempotency_key": plan.idempotency_key}),
            ),
        )
        migration_id = str(row["migration_id"])
        result = str(row["result"])
        if result in {"conflicting_identity", "partial_target_conflicting"}:
            raise ConflictError(result)
        migration = self._remember_migration(
            migration_id,
            migration_input,
            plan,
            migration_state=_claim_result_state(result),
        )
        return {**migration, "result": result}

    def attach_migration_work(self, migration_id: str, work_id: str, plan: DryRunResult) -> dict[str, Any]:
        row = self._cached_migration(migration_id)
        if row.get("migration_state") in {"completed", "recovery_completed"}:
            row["target_work_id"] = row.get("target_work_id") or work_id
            return dict(row)
        self._operator_call("attach_migration_work", (migration_id, work_id), fetch="none")
        row["target_work_id"] = work_id
        if row.get("migration_state") not in {"chapter_created", "asset_linked", "completed", "recovery_completed"}:
            row["migration_state"] = "work_created"
        return dict(row)

    def attach_migration_chapter(self, migration_id: str, chapter_id: str, plan: DryRunResult) -> dict[str, Any]:
        row = self._cached_migration(migration_id)
        if row.get("migration_state") in {"completed", "recovery_completed"}:
            row["target_chapter_id"] = row.get("target_chapter_id") or chapter_id
            return dict(row)
        self._operator_call("attach_migration_chapter", (migration_id, chapter_id), fetch="none")
        row["target_chapter_id"] = chapter_id
        if row.get("migration_state") not in {"asset_linked", "completed", "recovery_completed"}:
            row["migration_state"] = "chapter_created"
        return dict(row)

    def attach_migration_asset(self, migration_id: str, chapter_id: str, asset: dict[str, Any], plan: DryRunResult) -> dict[str, Any]:
        row = self._cached_migration(migration_id)
        if row.get("migration_state") in {"completed", "recovery_completed"}:
            row["target_chapter_id"] = row.get("target_chapter_id") or chapter_id
            row["target_chapter_asset_id"] = row.get("target_chapter_asset_id") or chapter_id
            return dict(row)
        self._operator_call(
            "attach_migration_asset",
            (
                migration_id,
                chapter_id,
                asset["sha256"],
                asset["size"],
                asset["storage_provider"],
                asset["storage_file_id"],
            ),
            fetch="none",
        )
        row["target_chapter_id"] = chapter_id
        row["target_chapter_asset_id"] = chapter_id
        if row.get("migration_state") not in {"completed", "recovery_completed"}:
            row["migration_state"] = "asset_linked"
        return dict(row)

    def complete_legacy_migration(self, migration_id: str, completed_by: str, plan: DryRunResult) -> dict[str, Any]:
        row = self._operator_call("complete_legacy_migration", (migration_id, completed_by))
        result = str(row["migration_state"])
        migration_state = "completed" if result == "already_completed" else result
        migration = self._cached_migration(migration_id)
        migration["migration_state"] = migration_state
        migration["completed_by"] = completed_by
        return {**migration, "result": result}

    def mark_migration_failure(self, migration_id: str, error_code: str, retryable: bool) -> None:
        self._operator_call("mark_migration_failure", (migration_id, error_code, retryable), fetch="none")
        if migration_id in self._migrations:
            self._migrations[migration_id]["migration_state"] = "failed_retryable" if retryable else "failed_terminal"
            self._migrations[migration_id]["last_error_code"] = error_code

    def append_legacy_migration_event(
        self,
        migration_id: str,
        event_type: str,
        metadata: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> str:
        row = self._operator_call("append_legacy_migration_event", (migration_id, event_type, Jsonb(metadata or {}), error_code))
        event_id = str(row["event_id"])
        self._events.append(
            {
                "id": event_id,
                "migration_id": migration_id,
                "event_type": event_type,
                "metadata": dict(metadata or {}),
                "error_code": error_code,
            }
        )
        return event_id

    def _cached_migration(self, migration_id: str) -> dict[str, Any]:
        if migration_id not in self._migrations:
            self._migrations[migration_id] = {
                "migration_id": migration_id,
                "migration_mode": None,
                "migration_state": "claimed",
                "target_work_id": None,
                "target_chapter_id": None,
                "target_chapter_asset_id": None,
                "source_record_digest": None,
                "source_snapshot_digest": None,
                "completed_by": None,
                "plan_digest": None,
            }
        return self._migrations[migration_id]

    def _remember_migration(
        self,
        migration_id: str,
        migration_input: LegacyMigrationInput,
        plan: DryRunResult,
        *,
        migration_state: str,
    ) -> dict[str, Any]:
        publication = migration_input.publication
        row = self._cached_migration(migration_id)
        row.update(
            {
                "migration_id": migration_id,
                "publication_key": f"{publication.source_instance_id}:{publication.legacy_publication_id}",
                "plan_digest": plan.plan_digest,
                "migration_mode": publication.migration_mode,
                "migration_state": migration_state,
                "source_record_digest": publication.source_record_digest,
                "source_snapshot_digest": migration_input.source.initial_snapshot_sha256,
            }
        )
        return dict(row)

    def snapshot(self) -> dict[str, Any]:
        return {
            "sources": {key: dict(row) for key, row in self._sources.items()},
            "migrations": {key: dict(row) for key, row in self._migrations.items()},
            "events": [dict(row) for row in self._events],
            "operation_effect_counts": {},
        }

    def locality_proof(self) -> dict[str, Any]:
        try:
            with self._connect(row_factory=dict_row) as conn:
                row = dict(
                    conn.execute(
                        """
                        select current_database() as current_database,
                               inet_server_addr()::text as inet_server_addr,
                               inet_server_port() as inet_server_port,
                               version() as server_version,
                               pg_is_in_recovery() as pg_is_in_recovery,
                               current_user as current_user,
                               session_user as session_user
                        """
                    ).fetchone()
                )
        except psycopg.Error as exc:
            raise map_postgres_error(exc) from exc
        return {
            "host": self.settings.host,
            "port": self.settings.port,
            "database": self.settings.dbname,
            **row,
        }

    def runtime_contract(self) -> dict[str, Any]:
        try:
            with self._connect(row_factory=dict_row) as conn:
                roles = list(conn.execute("select rolname from pg_roles where rolname in ('migration_operator','migration_publication_admin') order by rolname"))
                functions = list(
                    conn.execute(
                        """
                        select p.proname, p.prosecdef, p.proconfig::text as proconfig
                        from pg_proc p
                        join pg_namespace n on n.oid = p.pronamespace
                        where n.nspname = 'private'
                          and p.proname in (
                            'register_legacy_source','claim_legacy_publication','attach_migration_work',
                            'attach_migration_chapter','attach_migration_asset','mark_migration_failure',
                            'complete_legacy_migration','append_legacy_migration_event',
                            'authorize_draft_recovery_publication'
                          )
                        order by p.proname
                        """
                    )
                )
                executable = list(
                    conn.execute(
                        """
                        select routine_name
                        from information_schema.routine_privileges
                        where specific_schema = 'private'
                          and grantee = 'migration_operator'
                          and privilege_type = 'EXECUTE'
                        order by routine_name
                        """
                    )
                )
                rls = list(
                    conn.execute(
                        """
                        select schemaname, tablename, rowsecurity
                        from pg_tables
                        where schemaname in ('private','public')
                          and tablename in ('legacy_migration_sources','legacy_publication_migrations',
                                            'legacy_publication_migration_events','chapter_assets',
                                            'works','chapters')
                        order by schemaname, tablename
                        """
                    )
                )
        except psycopg.Error as exc:
            raise map_postgres_error(exc) from exc
        return {
            "roles": [row["rolname"] for row in roles],
            "runtime_functions": [row["proname"] for row in functions],
            "operator_executable_functions": [row["routine_name"] for row in executable],
            "security_definer": {row["proname"]: row["prosecdef"] for row in functions},
            "search_path": {row["proname"]: row["proconfig"] for row in functions},
            "rls": [dict(row) for row in rls],
        }


def _claim_result_state(result: str) -> str:
    if result == "already_completed":
        return "completed"
    if result == "recovery_completed":
        return "recovery_completed"
    return "claimed"
