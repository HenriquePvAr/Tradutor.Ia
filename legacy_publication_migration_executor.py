"""In-memory executor for synthetic legacy publication migration plans.

This module intentionally contains no real Supabase, Drive, SQL, HTTP, or
environment-backed adapter.  It executes only planner-approved inputs against
explicitly injected fake/protocol backends so that retry, idempotency, and
resume semantics can be tested before any real service is connected.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from legacy_publication_migrator import (
    DryRunResult,
    LegacyMigrationInput,
    canonical_plan_json,
    plan_migration,
)


EXECUTOR_SCHEMA_VERSION = 1
EXECUTABLE_READINESS = {
    "published_copy": "ready_for_execution",
    "draft_recovery": "recovery_draft_ready",
}
ALLOWED_PLAN_OPERATIONS = {
    "register_legacy_source",
    "claim_legacy_publication",
    "attach_migration_work",
    "attach_migration_chapter",
    "attach_migration_asset",
    "complete_legacy_migration",
}
REAL_EVENT_TYPES = {
    "source_registered",
    "migration_claimed",
    "work_attached",
    "chapter_attached",
    "asset_attached",
    "migration_retryable_failure",
    "migration_terminal_failure",
    "migration_completed",
}


class LegacyExecutorError(Exception):
    """Base class for fake executor domain failures."""


class ResponseLostError(LegacyExecutorError):
    """The backend mutation happened, but the caller did not receive the response."""


class TransientBackendError(LegacyExecutorError):
    """Retryable backend failure before a durable effect is known to exist."""


class TerminalBackendError(LegacyExecutorError):
    """Non-retryable backend failure."""


class ConflictError(TerminalBackendError):
    """Canonical identity conflict."""


class ValidationError(TerminalBackendError):
    """Contract validation failure."""


class ConflictingStorageObjectError(TerminalBackendError):
    """A deterministic fake storage key already points at different content."""


@dataclass(frozen=True)
class OperationExecutionResult:
    sequence: int
    operation_type: str
    idempotency_key: str
    attempt: int
    status: str
    backend_effect: str
    response_received: bool
    retryable: bool

    def to_json_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class LegacyExecutionResult:
    schema_version: int
    plan_digest: str
    migration_mode: str
    execution_status: str
    completed_operations: list[OperationExecutionResult]
    pending_operations: list[str]
    failed_operation: str | None
    retryable: bool
    resume_supported: bool
    resume_state: dict[str, Any]
    final_migration_state: str | None
    final_target_state: str | None
    storage_action: str | None
    warnings: list[str]
    errors: list[str]
    backend_calls_summary: dict[str, Any]
    audit_journal: list[dict[str, Any]]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_digest": self.plan_digest,
            "migration_mode": self.migration_mode,
            "execution_status": self.execution_status,
            "completed_operations": [op.to_json_dict() for op in self.completed_operations],
            "pending_operations": self.pending_operations,
            "failed_operation": self.failed_operation,
            "retryable": self.retryable,
            "resume_supported": self.resume_supported,
            "resume_state": self.resume_state,
            "final_migration_state": self.final_migration_state,
            "final_target_state": self.final_target_state,
            "storage_action": self.storage_action,
            "warnings": self.warnings,
            "errors": self.errors,
            "backend_calls_summary": self.backend_calls_summary,
            "audit_journal": self.audit_journal,
        }


class LegacyProvenanceBackend(Protocol):
    call_count: int

    def register_legacy_source(self, migration_input: LegacyMigrationInput, plan: DryRunResult) -> dict[str, Any]: ...

    def claim_legacy_publication(self, migration_input: LegacyMigrationInput, plan: DryRunResult) -> dict[str, Any]: ...

    def attach_migration_work(self, migration_id: str, work_id: str, plan: DryRunResult) -> dict[str, Any]: ...

    def attach_migration_chapter(self, migration_id: str, chapter_id: str, plan: DryRunResult) -> dict[str, Any]: ...

    def attach_migration_asset(self, migration_id: str, chapter_id: str, asset: dict[str, Any], plan: DryRunResult) -> dict[str, Any]: ...

    def complete_legacy_migration(self, migration_id: str, completed_by: str, plan: DryRunResult) -> dict[str, Any]: ...

    def mark_migration_failure(self, migration_id: str, error_code: str, retryable: bool) -> None: ...

    def append_legacy_migration_event(self, migration_id: str, event_type: str, metadata: dict[str, Any] | None = None, error_code: str | None = None) -> str: ...

    def snapshot(self) -> dict[str, Any]: ...


class LegacyTargetBackend(Protocol):
    call_count: int

    def resolve_or_create_work(self, migration_input: LegacyMigrationInput, plan: DryRunResult) -> dict[str, Any]: ...

    def resolve_or_create_chapter(self, migration_input: LegacyMigrationInput, work_id: str, plan: DryRunResult) -> dict[str, Any]: ...


class LegacyArtifactStorage(Protocol):
    upload_count: int

    def use_verified_remote_asset(self, migration_input: LegacyMigrationInput, plan: DryRunResult) -> dict[str, Any]: ...

    def upload(self, migration_input: LegacyMigrationInput, plan: DryRunResult) -> dict[str, Any]: ...

    def has_conflict(self, plan_digest: str) -> bool: ...


@dataclass
class _Fault:
    exception: Exception
    before_mutation: bool
    remaining: int


class _FaultMixin:
    def __init__(self) -> None:
        self._faults: dict[str, list[_Fault]] = {}

    def inject_response_lost(self, operation_type: str, *, after_calls: int = 1) -> None:
        self._faults.setdefault(operation_type, []).append(
            _Fault(ResponseLostError(f"response_lost:{operation_type}"), before_mutation=False, remaining=after_calls)
        )

    def inject_failure(self, operation_type: str, exception: Exception, *, before_mutation: bool, times: int = 1) -> None:
        self._faults.setdefault(operation_type, []).append(_Fault(exception, before_mutation=before_mutation, remaining=times))

    def clear_failures(self) -> None:
        self._faults.clear()

    def _pop_fault(self, operation_type: str, *, before_mutation: bool) -> Exception | None:
        for fault in list(self._faults.get(operation_type, [])):
            if fault.before_mutation != before_mutation or fault.remaining <= 0:
                continue
            fault.remaining -= 1
            if fault.remaining == 0:
                self._faults[operation_type].remove(fault)
            return fault.exception
        return None


def _stable_uuid(namespace: str, value: str) -> str:
    return str(uuid.uuid5(uuid.uuid5(uuid.NAMESPACE_URL, f"tradutor-ia:{namespace}"), value))


def _source_key(migration_input: LegacyMigrationInput) -> str:
    source = migration_input.source
    return f"{source.source_system}:{source.source_instance_id}"


def _publication_key(migration_input: LegacyMigrationInput) -> str:
    publication = migration_input.publication
    return f"{publication.source_system}:{publication.source_instance_id}:{publication.legacy_publication_id}"


class FakeLegacyProvenanceBackend(_FaultMixin):
    def __init__(self) -> None:
        super().__init__()
        self.sources: dict[str, dict[str, Any]] = {}
        self.migrations: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.operation_effect_counts: dict[str, int] = {}
        self.call_count = 0

    def _record_effect(self, operation_type: str) -> None:
        self.operation_effect_counts[operation_type] = self.operation_effect_counts.get(operation_type, 0) + 1

    def _call(self, operation_type: str) -> None:
        self.call_count += 1
        fault = self._pop_fault(operation_type, before_mutation=True)
        if fault:
            raise fault

    def _after_mutation(self, operation_type: str) -> None:
        fault = self._pop_fault(operation_type, before_mutation=False)
        if fault:
            raise fault

    def register_legacy_source(self, migration_input: LegacyMigrationInput, plan: DryRunResult) -> dict[str, Any]:
        operation = "register_legacy_source"
        self._call(operation)
        source = migration_input.source
        key = _source_key(migration_input)
        existing = self.sources.get(key)
        if existing:
            if existing["initial_snapshot_sha256"] != source.initial_snapshot_sha256:
                raise ConflictError("source_snapshot_conflict")
            self._after_mutation(operation)
            return existing
        row = {
            "source_instance_id": source.source_instance_id,
            "source_system": source.source_system,
            "initial_snapshot_sha256": source.initial_snapshot_sha256,
            "manifest_id": source.manifest_id,
            "source_state": "registered",
        }
        self.sources[key] = row
        self._record_effect(operation)
        self.append_legacy_migration_event("source", "source_registered", {"source_instance_id": source.source_instance_id})
        self._after_mutation(operation)
        return row

    def claim_legacy_publication(self, migration_input: LegacyMigrationInput, plan: DryRunResult) -> dict[str, Any]:
        operation = "claim_legacy_publication"
        self._call(operation)
        key = _publication_key(migration_input)
        existing = self.migrations.get(key)
        publication = migration_input.publication
        if existing:
            if existing["plan_digest"] != plan.plan_digest:
                raise ConflictError("conflicting_plan_digest")
            self._after_mutation(operation)
            return existing
        migration_id = _stable_uuid("migration", key)
        row = {
            "migration_id": migration_id,
            "publication_key": key,
            "plan_digest": plan.plan_digest,
            "migration_mode": publication.migration_mode,
            "migration_state": "claimed",
            "target_work_id": None,
            "target_chapter_id": None,
            "target_chapter_asset_id": None,
            "source_record_digest": publication.source_record_digest,
            "source_snapshot_digest": migration_input.source.initial_snapshot_sha256,
            "completed_by": None,
        }
        self.migrations[key] = row
        self._record_effect(operation)
        self.append_legacy_migration_event(migration_id, "migration_claimed", {"plan_digest": plan.plan_digest})
        self._after_mutation(operation)
        return row

    def _migration_by_id(self, migration_id: str) -> dict[str, Any]:
        for row in self.migrations.values():
            if row["migration_id"] == migration_id:
                return row
        raise ValidationError("migration_not_found")

    def attach_migration_work(self, migration_id: str, work_id: str, plan: DryRunResult) -> dict[str, Any]:
        operation = "attach_migration_work"
        self._call(operation)
        row = self._migration_by_id(migration_id)
        if row["target_work_id"] == work_id and row["migration_state"] in {"work_created", "chapter_created", "asset_linked", "completed", "recovery_completed"}:
            self._after_mutation(operation)
            return row
        if row["migration_state"] not in {"claimed", "failed_retryable"}:
            raise ValidationError("invalid_work_attach_state")
        row["target_work_id"] = work_id
        row["migration_state"] = "work_created"
        self._record_effect(operation)
        self.append_legacy_migration_event(migration_id, "work_attached", {"work_id": work_id})
        self._after_mutation(operation)
        return row

    def attach_migration_chapter(self, migration_id: str, chapter_id: str, plan: DryRunResult) -> dict[str, Any]:
        operation = "attach_migration_chapter"
        self._call(operation)
        row = self._migration_by_id(migration_id)
        if row["target_chapter_id"] == chapter_id and row["migration_state"] in {"chapter_created", "asset_linked", "completed", "recovery_completed"}:
            self._after_mutation(operation)
            return row
        if row["migration_state"] not in {"work_created", "failed_retryable"}:
            raise ValidationError("invalid_chapter_attach_state")
        row["target_chapter_id"] = chapter_id
        row["migration_state"] = "chapter_created"
        self._record_effect(operation)
        self.append_legacy_migration_event(migration_id, "chapter_attached", {"chapter_id": chapter_id})
        self._after_mutation(operation)
        return row

    def attach_migration_asset(self, migration_id: str, chapter_id: str, asset: dict[str, Any], plan: DryRunResult) -> dict[str, Any]:
        operation = "attach_migration_asset"
        self._call(operation)
        row = self._migration_by_id(migration_id)
        asset_id = asset["storage_file_id"]
        if row["target_chapter_asset_id"] == asset_id and row["migration_state"] in {"asset_linked", "completed", "recovery_completed"}:
            self._after_mutation(operation)
            return row
        if row["target_chapter_id"] != chapter_id:
            raise ValidationError("asset_chapter_mismatch")
        if row["migration_state"] not in {"chapter_created", "asset_pending", "failed_retryable"}:
            raise ValidationError("invalid_asset_attach_state")
        row["target_chapter_asset_id"] = asset_id
        row["migration_state"] = "asset_linked"
        self._record_effect(operation)
        self.append_legacy_migration_event(migration_id, "asset_attached", {"storage_file_id": asset_id})
        self._after_mutation(operation)
        return row

    def complete_legacy_migration(self, migration_id: str, completed_by: str, plan: DryRunResult) -> dict[str, Any]:
        operation = "complete_legacy_migration"
        self._call(operation)
        row = self._migration_by_id(migration_id)
        expected_terminal = "recovery_completed" if row["migration_mode"] == "draft_recovery" else "completed"
        if row["migration_state"] == expected_terminal:
            self._after_mutation(operation)
            return row
        if row["migration_mode"] == "published_copy" and row["migration_state"] != "asset_linked":
            raise ValidationError("published_copy_requires_asset_linked")
        if row["migration_mode"] == "draft_recovery" and row["migration_state"] not in {"chapter_created", "asset_linked"}:
            raise ValidationError("draft_recovery_requires_chapter_or_asset")
        row["migration_state"] = expected_terminal
        row["completed_by"] = completed_by
        self._record_effect(operation)
        self.append_legacy_migration_event(migration_id, "migration_completed", {"completed_by": completed_by})
        self._after_mutation(operation)
        return row

    def mark_migration_failure(self, migration_id: str, error_code: str, retryable: bool) -> None:
        row = self._migration_by_id(migration_id)
        row["migration_state"] = "failed_retryable" if retryable else "failed_terminal"
        row["last_error_code"] = error_code
        self.append_legacy_migration_event(
            migration_id,
            "migration_retryable_failure" if retryable else "migration_terminal_failure",
            {},
            error_code,
        )

    def append_legacy_migration_event(self, migration_id: str, event_type: str, metadata: dict[str, Any] | None = None, error_code: str | None = None) -> str:
        if event_type not in REAL_EVENT_TYPES:
            raise ValidationError(f"invalid_event_type:{event_type}")
        event_id = _stable_uuid("event", f"{migration_id}:{event_type}:{len(self.events)}")
        self.events.append(
            {
                "id": event_id,
                "migration_id": migration_id,
                "event_type": event_type,
                "metadata": dict(metadata or {}),
                "error_code": error_code,
            }
        )
        return event_id

    def snapshot(self) -> dict[str, Any]:
        return {
            "sources": json.loads(json.dumps(self.sources, sort_keys=True)),
            "migrations": json.loads(json.dumps(self.migrations, sort_keys=True)),
            "events": json.loads(json.dumps(self.events, sort_keys=True)),
            "operation_effect_counts": dict(self.operation_effect_counts),
        }


class FakeLegacyTargetBackend:
    def __init__(self) -> None:
        self.works: dict[str, dict[str, Any]] = {}
        self.chapters: dict[str, dict[str, Any]] = {}
        self.call_count = 0

    def resolve_or_create_work(self, migration_input: LegacyMigrationInput, plan: DryRunResult) -> dict[str, Any]:
        self.call_count += 1
        spec = migration_input.publication.target.work
        if spec.get("state") == "target_existing_conflict":
            raise ConflictError("target_collision")
        target_id = str(spec.get("target_id") or _stable_uuid("work", f"{plan.plan_digest}:work"))
        existing = self.works.get(target_id)
        if existing:
            return existing
        status = "draft" if migration_input.publication.migration_mode == "draft_recovery" else migration_input.publication.requested_target_status
        row = {"work_id": target_id, "status": status, "provenance": "strong", "identity": f"{plan.plan_digest}:work"}
        self.works[target_id] = row
        return row

    def resolve_or_create_chapter(self, migration_input: LegacyMigrationInput, work_id: str, plan: DryRunResult) -> dict[str, Any]:
        self.call_count += 1
        spec = migration_input.publication.target.chapter
        if spec.get("state") == "target_existing_conflict":
            raise ConflictError("target_collision")
        target_id = str(spec.get("target_id") or _stable_uuid("chapter", f"{plan.plan_digest}:chapter"))
        existing = self.chapters.get(target_id)
        if existing:
            return existing
        status = "draft" if migration_input.publication.migration_mode == "draft_recovery" else migration_input.publication.requested_target_status
        row = {"chapter_id": target_id, "work_id": work_id, "status": status, "provenance": "strong", "identity": f"{plan.plan_digest}:chapter"}
        self.chapters[target_id] = row
        return row


class FakeLegacyArtifactStorage:
    def __init__(self, *, mismatch: str | None = None) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.upload_count = 0
        self.bytes: dict[str, bytes] = {}
        self.mismatch = mismatch
        self._conflicting_plan_digests: set[str] = set()

    def has_conflict(self, plan_digest: str) -> bool:
        return plan_digest in self._conflicting_plan_digests

    def inject_conflicting_existing_object(self, plan_digest: str) -> None:
        self._conflicting_plan_digests.add(plan_digest)

    def use_verified_remote_asset(self, migration_input: LegacyMigrationInput, plan: DryRunResult) -> dict[str, Any]:
        remote_asset = migration_input.publication.remote_asset or {}
        return {
            "storage_provider": remote_asset.get("storage_provider", "verified_remote"),
            "storage_file_id": str(remote_asset.get("storage_file_id") or _stable_uuid("verified-asset", plan.plan_digest)),
            "size": migration_input.publication.artifact.pdf_size,
            "sha256": migration_input.publication.artifact.pdf_sha256,
            "action": "reuse_verified_remote_asset",
        }

    def upload(self, migration_input: LegacyMigrationInput, plan: DryRunResult) -> dict[str, Any]:
        if self.has_conflict(plan.plan_digest):
            raise ConflictingStorageObjectError("storage_object_conflict")
        artifact = migration_input.publication.artifact
        data = artifact.local_artifact_path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        size = len(data)
        storage_file_id = _stable_uuid("fake-storage", f"{plan.plan_digest}:{sha}:{size}")
        existing = self.objects.get(storage_file_id)
        if existing:
            if existing["sha256"] != sha or existing["size"] != size:
                raise ConflictingStorageObjectError("storage_object_conflict")
            return {**existing, "action": "already_exists_same_content"}
        self.upload_count += 1
        reported_sha = "0" * 64 if self.mismatch == "sha256" else sha
        reported_size = size + 1 if self.mismatch == "size" else size
        row = {
            "storage_provider": "fake_memory",
            "storage_file_id": storage_file_id,
            "size": reported_size,
            "sha256": reported_sha,
            "actual_size": size,
            "actual_sha256": sha,
            "action": "fake_upload",
        }
        self.objects[storage_file_id] = row
        self.bytes[storage_file_id] = data
        return row


class LegacyMigrationExecutor:
    def __init__(
        self,
        *,
        provenance_backend: LegacyProvenanceBackend,
        target_backend: LegacyTargetBackend,
        artifact_storage: LegacyArtifactStorage,
        max_attempts: int = 3,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts_must_be_positive")
        self.provenance_backend = provenance_backend
        self.target_backend = target_backend
        self.artifact_storage = artifact_storage
        self.max_attempts = max_attempts

    def execute(self, migration_input: LegacyMigrationInput, plan: DryRunResult) -> LegacyExecutionResult:
        return self._execute(migration_input, plan, resume_state=None)

    def resume(self, migration_input: LegacyMigrationInput, plan: DryRunResult, resume_state: dict[str, Any]) -> LegacyExecutionResult:
        return self._execute(migration_input, plan, resume_state=resume_state)

    def _blocked(self, plan: DryRunResult, migration_input: LegacyMigrationInput, errors: list[str], journal: list[dict[str, Any]]) -> LegacyExecutionResult:
        return self._result(
            plan=plan,
            migration_input=migration_input,
            execution_status="blocked_before_execution",
            completed=[],
            pending=[],
            failed_operation=None,
            retryable=False,
            resume_supported=False,
            resume_state={},
            final_migration_state=None,
            final_target_state=None,
            storage_action=None,
            warnings=[],
            errors=errors,
            journal=journal,
        )

    def _validate_plan(self, migration_input: LegacyMigrationInput, plan: DryRunResult) -> list[str]:
        errors: list[str] = []
        computed_self = hashlib.sha256(canonical_plan_json(plan.to_json_dict(), include_digest=False).encode("utf-8")).hexdigest()
        if computed_self != plan.plan_digest:
            errors.append("plan_digest_mismatch")
        expected_readiness = EXECUTABLE_READINESS.get(plan.migration_mode)
        if expected_readiness is None or plan.readiness != expected_readiness:
            errors.append("plan_not_executable")
        for op in plan.planned_operations:
            if op.get("operation_type") not in ALLOWED_PLAN_OPERATIONS:
                errors.append("unknown_planned_operation")
        if plan.migration_mode == "published_copy" and "attach_migration_asset" not in [op.get("operation_type") for op in plan.planned_operations]:
            errors.append("plan_missing_asset_operation")
        return sorted(set(errors))

    def _validate_plan_against_input(self, migration_input: LegacyMigrationInput, plan: DryRunResult) -> list[str]:
        expected = plan_migration(migration_input)
        if expected.plan_digest != plan.plan_digest:
            return ["plan_digest_mismatch"]
        return []

    def _revalidate_artifact(self, migration_input: LegacyMigrationInput, plan: DryRunResult) -> list[str]:
        artifact = migration_input.publication.artifact
        planned_name = plan.artifact_verification.get("path")
        path = artifact.local_artifact_path
        if planned_name and path.name != planned_name:
            return ["artifact_path_outside_root"]
        try:
            resolved_parent = path.parent.resolve()
            resolved_path = path.resolve()
        except OSError:
            return ["artifact_changed_after_plan"]
        if resolved_parent != resolved_path.parent:
            return ["artifact_path_outside_root"]
        if not path.exists() or not path.is_file():
            return ["artifact_changed_after_plan"]
        data = path.read_bytes()
        if not data.startswith(b"%PDF-"):
            return ["artifact_changed_after_plan"]
        sha = hashlib.sha256(data).hexdigest()
        if len(data) != artifact.pdf_size or sha != artifact.pdf_sha256:
            return ["artifact_changed_after_plan"]
        return []

    def _execute(self, migration_input: LegacyMigrationInput, plan: DryRunResult, resume_state: dict[str, Any] | None) -> LegacyExecutionResult:
        journal = [{"event": "execution_started", "plan_digest": plan.plan_digest, "resume": bool(resume_state)}]
        validation_errors = self._validate_plan(migration_input, plan)
        if validation_errors:
            return self._blocked(plan, migration_input, validation_errors, journal)
        artifact_errors = self._revalidate_artifact(migration_input, plan)
        if artifact_errors:
            return self._blocked(plan, migration_input, artifact_errors, journal)
        input_plan_errors = self._validate_plan_against_input(migration_input, plan)
        if input_plan_errors:
            return self._blocked(plan, migration_input, input_plan_errors, journal)
        if self.artifact_storage.has_conflict(plan.plan_digest):
            return self._result(
                plan=plan,
                migration_input=migration_input,
                execution_status="failed_terminal",
                completed=[],
                pending=self._operation_sequence(migration_input, plan),
                failed_operation="storage_precheck",
                retryable=False,
                resume_supported=False,
                resume_state={},
                final_migration_state=None,
                final_target_state=None,
                storage_action=None,
                warnings=[],
                errors=["storage_object_conflict"],
                journal=journal,
            )

        completed: list[OperationExecutionResult] = []
        context: dict[str, Any] = {"storage_action": None, "work_id": None, "chapter_id": None, "migration_id": None}
        sequence = self._operation_sequence(migration_input, plan)
        for idx, operation_type in enumerate(sequence, start=1):
            outcome = self._run_operation(idx, operation_type, migration_input, plan, context)
            if isinstance(outcome, OperationExecutionResult):
                completed.append(outcome)
                journal.append({"event": f"{operation_type}_completed", "operation_type": operation_type, "attempt": outcome.attempt})
                continue
            status, error_code, retryable = outcome
            migration_id = context.get("migration_id")
            if migration_id and status == "failed_terminal":
                try:
                    self.provenance_backend.mark_migration_failure(migration_id, error_code, retryable=False)
                except LegacyExecutorError:
                    pass
            pending = sequence[idx - 1 :]
            return self._result(
                plan=plan,
                migration_input=migration_input,
                execution_status=status,
                completed=completed,
                pending=pending,
                failed_operation=operation_type,
                retryable=retryable,
                resume_supported=retryable,
                resume_state={"plan_digest": plan.plan_digest, "next_operation": operation_type, "completed": [op.operation_type for op in completed]},
                final_migration_state=self._final_migration_state(context),
                final_target_state=self._final_target_state(migration_input, context),
                storage_action=context.get("storage_action"),
                warnings=[],
                errors=[error_code],
                journal=journal,
            )
        return self._result(
            plan=plan,
            migration_input=migration_input,
            execution_status="completed",
            completed=completed,
            pending=[],
            failed_operation=None,
            retryable=False,
            resume_supported=True,
            resume_state={"plan_digest": plan.plan_digest, "completed": [op.operation_type for op in completed]},
            final_migration_state=self._final_migration_state(context),
            final_target_state=self._final_target_state(migration_input, context),
            storage_action=context.get("storage_action"),
            warnings=[],
            errors=[],
            journal=journal,
        )

    def _operation_sequence(self, migration_input: LegacyMigrationInput, plan: DryRunResult) -> list[str]:
        if migration_input.publication.migration_mode == "draft_recovery":
            return [
                "register_legacy_source",
                "claim_legacy_publication",
                "attach_migration_work",
                "attach_migration_chapter",
                "attach_migration_asset",
                "complete_legacy_migration",
            ]
        return [str(op["operation_type"]) for op in plan.planned_operations]

    def _run_operation(
        self,
        sequence: int,
        operation_type: str,
        migration_input: LegacyMigrationInput,
        plan: DryRunResult,
        context: dict[str, Any],
    ) -> OperationExecutionResult | tuple[str, str, bool]:
        for attempt in range(1, self.max_attempts + 1):
            try:
                backend_effect = self._dispatch_operation(operation_type, migration_input, plan, context)
                return OperationExecutionResult(
                    sequence=sequence,
                    operation_type=operation_type,
                    idempotency_key=f"{plan.idempotency_key}:{operation_type}",
                    attempt=attempt,
                    status="completed",
                    backend_effect=backend_effect,
                    response_received=True,
                    retryable=False,
                )
            except ResponseLostError:
                if attempt >= self.max_attempts:
                    return ("incomplete_resume_required", "response_lost", True)
                continue
            except TransientBackendError as exc:
                if attempt >= self.max_attempts:
                    return ("incomplete_resume_required", str(exc) or "transient_backend_error", True)
                continue
            except ConflictingStorageObjectError as exc:
                return ("failed_terminal", str(exc) or "storage_object_conflict", False)
            except TerminalBackendError as exc:
                return ("failed_terminal", str(exc) or "terminal_backend_error", False)
        return ("incomplete_resume_required", "max_attempts_exhausted", True)

    def _dispatch_operation(
        self,
        operation_type: str,
        migration_input: LegacyMigrationInput,
        plan: DryRunResult,
        context: dict[str, Any],
    ) -> str:
        if operation_type == "register_legacy_source":
            self.provenance_backend.register_legacy_source(migration_input, plan)
            return "source_registered_or_reused"
        if operation_type == "claim_legacy_publication":
            row = self.provenance_backend.claim_legacy_publication(migration_input, plan)
            context["migration_id"] = row["migration_id"]
            return "publication_claimed_or_reused"
        if operation_type == "attach_migration_work":
            work = self.target_backend.resolve_or_create_work(migration_input, plan)
            context["work_id"] = work["work_id"]
            row = self.provenance_backend.claim_legacy_publication(migration_input, plan)
            context["migration_id"] = row["migration_id"]
            self.provenance_backend.attach_migration_work(row["migration_id"], work["work_id"], plan)
            return "work_attached_or_reused"
        if operation_type == "attach_migration_chapter":
            if not context.get("work_id"):
                work = self.target_backend.resolve_or_create_work(migration_input, plan)
                context["work_id"] = work["work_id"]
            chapter = self.target_backend.resolve_or_create_chapter(migration_input, context["work_id"], plan)
            context["chapter_id"] = chapter["chapter_id"]
            row = self.provenance_backend.claim_legacy_publication(migration_input, plan)
            context["migration_id"] = row["migration_id"]
            self.provenance_backend.attach_migration_chapter(row["migration_id"], chapter["chapter_id"], plan)
            return "chapter_attached_or_reused"
        if operation_type == "attach_migration_asset":
            if not context.get("chapter_id"):
                if not context.get("work_id"):
                    work = self.target_backend.resolve_or_create_work(migration_input, plan)
                    context["work_id"] = work["work_id"]
                chapter = self.target_backend.resolve_or_create_chapter(migration_input, context["work_id"], plan)
                context["chapter_id"] = chapter["chapter_id"]
            if migration_input.publication.migration_mode == "published_copy":
                asset = self.artifact_storage.use_verified_remote_asset(migration_input, plan)
                context["storage_action"] = "reuse_verified_remote_asset"
            else:
                asset = self.artifact_storage.upload(migration_input, plan)
                context["storage_action"] = "fake_upload" if asset.get("action") == "fake_upload" else str(asset.get("action"))
                expected = migration_input.publication.artifact
                if asset["sha256"] != expected.pdf_sha256 or asset["size"] != expected.pdf_size:
                    raise TerminalBackendError("storage_integrity_mismatch")
            row = self.provenance_backend.claim_legacy_publication(migration_input, plan)
            context["migration_id"] = row["migration_id"]
            self.provenance_backend.attach_migration_asset(row["migration_id"], context["chapter_id"], asset, plan)
            return "asset_attached_or_reused"
        if operation_type == "complete_legacy_migration":
            row = self.provenance_backend.claim_legacy_publication(migration_input, plan)
            context["migration_id"] = row["migration_id"]
            completed = self.provenance_backend.complete_legacy_migration(row["migration_id"], "fake-executor", plan)
            return completed["migration_state"]
        raise TerminalBackendError("unknown_planned_operation")

    def _final_migration_state(self, context: dict[str, Any]) -> str | None:
        migration_id = context.get("migration_id")
        if not migration_id:
            return None
        for migration in self.provenance_backend.snapshot()["migrations"].values():
            if migration["migration_id"] == migration_id:
                return migration["migration_state"]
        return None

    def _final_target_state(self, migration_input: LegacyMigrationInput, context: dict[str, Any]) -> str | None:
        chapter_id = context.get("chapter_id")
        if not chapter_id:
            return None
        if hasattr(self.target_backend, "chapters"):
            chapter = getattr(self.target_backend, "chapters").get(chapter_id)
            if chapter:
                return chapter.get("status")
        return "draft" if migration_input.publication.migration_mode == "draft_recovery" else migration_input.publication.requested_target_status

    def _result(
        self,
        *,
        plan: DryRunResult,
        migration_input: LegacyMigrationInput,
        execution_status: str,
        completed: list[OperationExecutionResult],
        pending: list[str],
        failed_operation: str | None,
        retryable: bool,
        resume_supported: bool,
        resume_state: dict[str, Any],
        final_migration_state: str | None,
        final_target_state: str | None,
        storage_action: str | None,
        warnings: list[str],
        errors: list[str],
        journal: list[dict[str, Any]],
    ) -> LegacyExecutionResult:
        backend_calls = {
            "provenance": getattr(self.provenance_backend, "call_count", 0),
            "targets": getattr(self.target_backend, "call_count", 0),
            "storage_uploads": getattr(self.artifact_storage, "upload_count", 0),
        }
        backend_calls["total"] = backend_calls["provenance"] + backend_calls["targets"] + backend_calls["storage_uploads"]
        return LegacyExecutionResult(
            schema_version=EXECUTOR_SCHEMA_VERSION,
            plan_digest=plan.plan_digest,
            migration_mode=migration_input.publication.migration_mode,
            execution_status=execution_status,
            completed_operations=completed,
            pending_operations=pending,
            failed_operation=failed_operation,
            retryable=retryable,
            resume_supported=resume_supported,
            resume_state=resume_state,
            final_migration_state=final_migration_state,
            final_target_state=final_target_state,
            storage_action=storage_action,
            warnings=warnings,
            errors=sorted(set(errors)),
            backend_calls_summary=backend_calls,
            audit_journal=journal,
        )
