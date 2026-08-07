"""Pure dry-run planner for future legacy publication migration.

The idempotency key is intentionally derived only from the canonical
(source_system, source_instance_id, legacy_publication_id) identity. If a future
executor writes a remote operation and loses the response, re-planning the same
input produces the same operation identities instead of minting a new migration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
VALID_MODES = {"published_copy", "draft_recovery"}
VALID_REMOTE_ASSET_VERIFICATIONS = {"unverified", "partial", "strong"}
VALID_TARGET_STATES = {
    "target_absent",
    "target_existing_compatible",
    "target_existing_conflict",
    "missing",
}
WRITE_FUNCTIONS = {
    "register_legacy_source": "private.register_legacy_source(jsonb)",
    "claim_legacy_publication": "private.claim_legacy_publication(...)",
    "attach_migration_work": "private.attach_migration_work(uuid, uuid)",
    "attach_migration_chapter": "private.attach_migration_chapter(uuid, uuid)",
    "attach_migration_asset": "private.attach_migration_asset(uuid, uuid, text, bigint, text, text)",
    "complete_legacy_migration": "private.complete_legacy_migration(uuid, text)",
}
PRIVATE_INVENTORY_FILENAMES = {
    "publication_inventory.private.json",
    "owner_resolution.private.json",
    "job_correlation.private.json",
    "output_correlation.private.json",
    "logical_fingerprints.private.json",
}
SECRET_KEYS = {"token", "password", "authorization", "cookie", "connection_string", "service_role"}


@dataclass(frozen=True)
class LegacySourceInput:
    source_system: str
    source_instance_id: str
    source_schema_version: int
    initial_snapshot_sha256: str
    initial_logical_fingerprint: str
    manifest_id: str
    manifest_version: int
    approval_state: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class LegacyArtifactInput:
    local_artifact_path: Path
    pdf_sha256: str
    pdf_md5: str | None
    pdf_size: int


@dataclass(frozen=True)
class LegacyTargetPlan:
    work: dict[str, Any]
    chapter: dict[str, Any]


@dataclass(frozen=True)
class LegacyPublicationInput:
    legacy_publication_id: str
    source_instance_id: str
    source_system: str
    owner_id: str | None
    owner_resolution_state: str
    source_job_id: str | None
    source_run_id: str | None
    migration_mode: str
    requested_target_status: str
    source_record_digest: str
    artifact: LegacyArtifactInput
    remote_asset_verification: str
    remote_asset: dict[str, Any] | None
    target: LegacyTargetPlan
    metadata: dict[str, Any]


@dataclass(frozen=True)
class LegacyMigrationInput:
    schema_version: int
    source: LegacySourceInput
    publication: LegacyPublicationInput


@dataclass(frozen=True)
class DryRunResult:
    schema_version: int
    migration_mode: str
    source_identity: dict[str, Any]
    publication_identity: dict[str, Any]
    preconditions: dict[str, Any]
    artifact_verification: dict[str, Any]
    planned_operations: list[dict[str, Any]]
    blocked_operations: dict[str, Any]
    warnings: list[str]
    errors: list[str]
    readiness: str
    would_write: bool
    idempotency_key: str
    plan_digest: str = ""
    publication_authorization_required: bool = False

    def to_json_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "migration_mode": self.migration_mode,
            "source_identity": self.source_identity,
            "publication_identity": self.publication_identity,
            "preconditions": self.preconditions,
            "artifact_verification": self.artifact_verification,
            "planned_operations": self.planned_operations,
            "blocked_operations": self.blocked_operations,
            "warnings": self.warnings,
            "errors": self.errors,
            "readiness": self.readiness,
            "would_write": self.would_write,
            "idempotency_key": self.idempotency_key,
            "plan_digest": self.plan_digest,
            "publication_authorization_required": self.publication_authorization_required,
        }
        return payload


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _mask_uuid(value: str | None) -> str | None:
    if value is None:
        return None
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value):
        return f"{value[:9]}...{value[-4:]}"
    if len(value) > 16:
        return f"{value[:8]}...{value[-4:]}"
    return value


def _mask_hash(value: str | None) -> str | None:
    if value is None:
        return None
    if re.fullmatch(r"[0-9a-f]{32,64}", value):
        return f"{value[:8]}...{value[-4:]}"
    return _mask_uuid(value)


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in sorted(value.items()):
            lowered = str(key).lower()
            if any(secret in lowered for secret in SECRET_KEYS):
                sanitized[key] = "<redacted>"
            elif lowered.endswith("_id") or "storage" in lowered:
                sanitized[key] = _mask_uuid(str(item)) if item is not None else None
            else:
                sanitized[key] = _sanitize(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def _canonical_identity(source_system: str, source_instance_id: str, legacy_publication_id: str) -> str:
    return f"legacy-migration:{source_system}:{source_instance_id}:{legacy_publication_id}"


def canonical_plan_json(plan: dict[str, Any], *, include_digest: bool = True) -> str:
    payload = dict(plan)
    if not include_digest:
        payload["plan_digest"] = ""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _resolve_artifact_path(raw_path: str, artifact_root: Path) -> Path:
    root = artifact_root.resolve()
    candidate = (root / raw_path).resolve()
    if root != candidate and root not in candidate.parents:
        raise ValueError("artifact_path_outside_root")
    return candidate


def _require_hex(value: str, length: int, error: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-f]{{{length}}}", value):
        errors.append(error)


def _is_canonical_uuid(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def load_migration_input(fixture: str | Path, *, artifact_root: str | Path | None = None) -> LegacyMigrationInput:
    fixture_path = Path(fixture)
    if fixture_path.name in PRIVATE_INVENTORY_FILENAMES:
        raise ValueError("private_inventory_not_allowed")
    root = Path(artifact_root) if artifact_root is not None else fixture_path.parent
    raw = json.loads(fixture_path.read_text(encoding="utf-8-sig"))
    publications = raw.get("publications") or []
    if len(publications) != 1:
        raise ValueError("exactly_one_publication_required")
    source_raw = raw["source"]
    pub_raw = publications[0]
    source = LegacySourceInput(
        source_system=str(source_raw.get("source_system") or ""),
        source_instance_id=str(source_raw.get("source_instance_id") or ""),
        source_schema_version=int(source_raw.get("source_schema_version") or 0),
        initial_snapshot_sha256=str(source_raw.get("initial_snapshot_sha256") or ""),
        initial_logical_fingerprint=str(source_raw.get("initial_logical_fingerprint") or ""),
        manifest_id=str(source_raw.get("manifest_id") or ""),
        manifest_version=int(source_raw.get("manifest_version") or 0),
        approval_state=str(source_raw.get("approval_state") or ""),
        metadata=dict(source_raw.get("metadata") or {}),
    )
    artifact = LegacyArtifactInput(
        local_artifact_path=_resolve_artifact_path(str(pub_raw.get("local_artifact_path") or ""), root),
        pdf_sha256=str(pub_raw.get("pdf_sha256") or ""),
        pdf_md5=pub_raw.get("pdf_md5"),
        pdf_size=int(pub_raw.get("pdf_size") or 0),
    )
    target_raw = pub_raw.get("target") or {}
    publication = LegacyPublicationInput(
        legacy_publication_id=str(pub_raw.get("legacy_publication_id") or ""),
        source_instance_id=str(pub_raw.get("source_instance_id") or ""),
        source_system=str(pub_raw.get("source_system") or ""),
        owner_id=pub_raw.get("owner_id"),
        owner_resolution_state=str(pub_raw.get("owner_resolution_state") or "unresolved"),
        source_job_id=pub_raw.get("source_job_id"),
        source_run_id=pub_raw.get("source_run_id"),
        migration_mode=str(pub_raw.get("migration_mode") or ""),
        requested_target_status=str(pub_raw.get("requested_target_status") or ""),
        source_record_digest=str(pub_raw.get("source_record_digest") or ""),
        artifact=artifact,
        remote_asset_verification=str(pub_raw.get("remote_asset_verification") or "unverified"),
        remote_asset=pub_raw.get("remote_asset"),
        target=LegacyTargetPlan(work=dict(target_raw.get("work") or {}), chapter=dict(target_raw.get("chapter") or {})),
        metadata=dict(pub_raw.get("metadata") or {}),
    )
    return LegacyMigrationInput(schema_version=int(raw.get("schema_version") or 0), source=source, publication=publication)


def _verify_artifact(artifact: LegacyArtifactInput) -> tuple[dict[str, Any], list[str]]:
    result: dict[str, Any] = {
        "path": artifact.local_artifact_path.name,
        "exists": False,
        "is_file": False,
        "signature": "unavailable",
        "size": "unavailable",
        "sha256": "unavailable",
        "md5": "unavailable",
        "declared_size": artifact.pdf_size,
        "declared_sha256": _mask_hash(artifact.pdf_sha256),
        "asset_action": None,
    }
    errors: list[str] = []
    if not artifact.local_artifact_path.exists():
        errors.append("artifact_missing")
        return result, errors
    result["exists"] = True
    if not artifact.local_artifact_path.is_file():
        errors.append("artifact_not_file")
        return result, errors
    result["is_file"] = True
    data = artifact.local_artifact_path.read_bytes()
    result["signature"] = "match" if data.startswith(b"%PDF-") else "mismatch"
    if result["signature"] != "match":
        errors.append("artifact_not_pdf")
    size = len(data)
    sha = hashlib.sha256(data).hexdigest()
    md5 = hashlib.md5(data).hexdigest()
    result["computed_size"] = size
    result["computed_sha256"] = _mask_hash(sha)
    result["computed_md5"] = _mask_hash(md5)
    result["size"] = "match" if size == artifact.pdf_size else "mismatch"
    result["sha256"] = "match" if sha == artifact.pdf_sha256 else "mismatch"
    if artifact.pdf_md5:
        result["declared_md5"] = _mask_hash(artifact.pdf_md5)
        result["md5"] = "match" if md5 == artifact.pdf_md5 else "mismatch"
    if result["size"] == "mismatch":
        errors.append("artifact_size_mismatch")
    if result["sha256"] == "mismatch":
        errors.append("artifact_sha256_mismatch")
    return result, errors


def _target_errors(target: LegacyTargetPlan) -> list[str]:
    errors: list[str] = []
    for name, spec in (("work", target.work), ("chapter", target.chapter)):
        state = str(spec.get("state") or "")
        if state not in VALID_TARGET_STATES:
            errors.append("invalid_target_state")
            continue
        if state == "missing":
            errors.append(f"{name}_target_missing")
        if state == "target_existing_conflict":
            errors.append("target_collision")
        if state == "target_existing_compatible":
            if not spec.get("target_id") or spec.get("provenance") != "strong":
                errors.append("target_identity_unresolved")
    return errors


def _operation(sequence: int, operation_type: str, identity: str, before: str, after: str) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "operation_type": operation_type,
        "required_role": "migration_operator",
        "required_function": WRITE_FUNCTIONS[operation_type],
        "preconditions": ["approved_synthetic_input", "dry_run_only"],
        "idempotency_identity": f"{identity}:{operation_type}",
        "expected_state_before": before,
        "expected_state_after": after,
        "remote_write": True,
        "executed": False,
    }


def _planned_operations(mode: str, canonical_identity: str, include_asset: bool) -> list[dict[str, Any]]:
    base = [
        ("register_legacy_source", "source_unregistered_or_registered", "source_registered"),
        ("claim_legacy_publication", "source_registered", "claimed"),
        ("attach_migration_work", "claimed", "work_created"),
        ("attach_migration_chapter", "work_created", "chapter_created"),
    ]
    if include_asset:
        base.append(("attach_migration_asset", "chapter_created", "asset_linked"))
        base.append(("complete_legacy_migration", "asset_linked", "completed"))
    elif mode == "draft_recovery":
        base.append(("complete_legacy_migration", "chapter_created", "recovery_completed"))
    return [_operation(idx + 1, op, canonical_identity, before, after) for idx, (op, before, after) in enumerate(base)]


def _readiness(errors: list[str], publication: LegacyPublicationInput) -> str:
    if any(error.startswith("invalid_") for error in errors) or "source_identity_mismatch" in errors:
        return "invalid_input"
    if "owner_unresolved" in errors:
        return "blocked_owner"
    if "artifact_sha256_mismatch" in errors:
        return "blocked_hash_mismatch"
    if "artifact_missing" in errors or "remote_asset_missing" in errors:
        return "blocked_artifact"
    if "remote_asset_verification_insufficient" in errors:
        return "blocked_remote_asset_verification"
    if "target_collision" in errors or "target_identity_unresolved" in errors:
        return "blocked_target_collision"
    if any(error.endswith("_target_missing") for error in errors):
        return "blocked_contract"
    if errors:
        return "blocked_contract"
    return "recovery_draft_ready" if publication.migration_mode == "draft_recovery" else "ready_for_execution"


def plan_migration(migration_input: LegacyMigrationInput) -> DryRunResult:
    source = migration_input.source
    publication = migration_input.publication
    errors: list[str] = []
    warnings: list[str] = []
    if migration_input.schema_version != SCHEMA_VERSION:
        errors.append("invalid_schema_version")
    if source.approval_state != "approved":
        errors.append("source_manifest_not_approved")
    _require_hex(source.initial_snapshot_sha256, 64, "invalid_source_hash_evidence", errors)
    if not source.source_system or not source.source_instance_id:
        errors.append("invalid_source_identity")
    if not _is_canonical_uuid(source.source_instance_id):
        errors.append("invalid_source_instance_id")
    if not _is_canonical_uuid(source.manifest_id):
        errors.append("invalid_manifest_id")
    if publication.source_instance_id != source.source_instance_id or publication.source_system != source.source_system:
        errors.append("source_identity_mismatch")
    if not _is_canonical_uuid(publication.source_instance_id):
        errors.append("invalid_publication_source_instance_id")
    if not publication.legacy_publication_id or not publication.source_record_digest:
        errors.append("invalid_publication_identity")
    if publication.migration_mode not in VALID_MODES:
        errors.append("invalid_migration_mode")
    remote_asset_verification_valid = publication.remote_asset_verification in VALID_REMOTE_ASSET_VERIFICATIONS
    if not remote_asset_verification_valid:
        errors.append("invalid_remote_asset_verification")
    if publication.owner_resolution_state != "resolved" or not publication.owner_id:
        errors.append("owner_unresolved")
    elif not _is_canonical_uuid(publication.owner_id):
        errors.append("invalid_owner_id")
    artifact_verification, artifact_errors = _verify_artifact(publication.artifact)
    errors.extend(artifact_errors)
    errors.extend(_target_errors(publication.target))
    for target_name, target_spec in (("work", publication.target.work), ("chapter", publication.target.chapter)):
        target_id = target_spec.get("target_id")
        if target_id is not None and not _is_canonical_uuid(str(target_id)):
            errors.append(f"invalid_target_{target_name}_id")
    if publication.migration_mode == "published_copy":
        if publication.remote_asset is None:
            errors.append("remote_asset_missing")
        elif remote_asset_verification_valid and publication.remote_asset_verification != "strong":
            errors.append("remote_asset_verification_insufficient")
    elif publication.migration_mode == "draft_recovery":
        artifact_verification["asset_action"] = "local_upload_required"
        if publication.requested_target_status == "community":
            errors.append("draft_recovery_community_forbidden")
    canonical_identity = _canonical_identity(
        source.source_system, source.source_instance_id, publication.legacy_publication_id
    )
    identity_digest = _sha256_text(canonical_identity)
    idempotency_key = f"legacy-publication-migration:{identity_digest}"
    readiness = _readiness(errors, publication)
    include_ops = readiness in {"ready_for_execution", "recovery_draft_ready"}
    include_asset = publication.migration_mode == "published_copy" and readiness == "ready_for_execution"
    planned_operations = _planned_operations(publication.migration_mode, idempotency_key, include_asset) if include_ops else []
    source_identity = {
        "source_system": source.source_system,
        "source_instance_id": _mask_uuid(source.source_instance_id),
        "initial_snapshot_sha256": _mask_hash(source.initial_snapshot_sha256),
        "manifest_id": _mask_uuid(source.manifest_id),
        "metadata": _sanitize(source.metadata),
    }
    publication_identity = {
        "source_system": publication.source_system,
        "source_instance_id": _mask_uuid(publication.source_instance_id),
        "legacy_publication_id": publication.legacy_publication_id,
        "owner_id": _mask_uuid(publication.owner_id),
        "source_job_id": _mask_uuid(publication.source_job_id),
        "source_run_id": _mask_uuid(publication.source_run_id),
        "source_record_digest": publication.source_record_digest,
    }
    remote_asset = _sanitize(publication.remote_asset) if publication.remote_asset else None
    preconditions = {
        "owner_resolved": publication.owner_resolution_state == "resolved" and bool(publication.owner_id),
        "pdf_validated": not artifact_errors,
        "target_plan_coherent": not _target_errors(publication.target),
        "remote_asset_verification": publication.remote_asset_verification,
        "remote_asset": remote_asset,
        "provenance_contract_valid": "source_identity_mismatch" not in errors,
        "dry_run_only": True,
    }
    blocked_operations = {
        "execution": "dry_run_only",
        "remote_functions": "not_called",
        "network": "not_allowed",
        "administrative_authorization": "separate_human_step",
    }
    result = DryRunResult(
        schema_version=SCHEMA_VERSION,
        migration_mode=publication.migration_mode,
        source_identity=source_identity,
        publication_identity=publication_identity,
        preconditions=preconditions,
        artifact_verification=artifact_verification,
        planned_operations=planned_operations,
        blocked_operations=blocked_operations,
        warnings=warnings,
        errors=sorted(set(errors)),
        readiness=readiness,
        would_write=False,
        idempotency_key=idempotency_key,
        publication_authorization_required=False,
    )
    digest = hashlib.sha256(canonical_plan_json(result.to_json_dict(), include_digest=False).encode("utf-8")).hexdigest()
    return DryRunResult(**{**result.__dict__, "plan_digest": digest})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synthetic legacy publication migration planner")
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--artifact-root", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.dry_run:
        print("execution_not_enabled", file=sys.stderr)
        return 2
    try:
        migration_input = load_migration_input(args.fixture, artifact_root=args.artifact_root)
        result = plan_migration(migration_input)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result.to_json_dict(), sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
