"""Hermetic Google Drive storage adapter for the legacy migration executor.

Implements the ``LegacyArtifactStorage`` protocol declared in
``legacy_publication_migration_executor.py`` against an injected
``DriveFilesClient``. This module never authenticates, never reads the
environment, never builds an OAuth flow, and never talks to a real network:
the client and the destination folder are always supplied by the caller.

Google Drive has no native SHA-256 field. For files this adapter creates, the
canonical SHA-256 is persisted in ``appProperties`` (private, small,
key/value metadata) alongside a static schema marker and the opaque
idempotency key. Drive's own ``md5Checksum`` is used only as an additional,
non-canonical integrity signal. A Drive object that lacks our SHA-256
``appProperty`` (e.g. a legacy file this adapter did not create) is never
promoted to "strong" here.

Response-lost recovery uses a narrow lookup restricted to the exact
destination folder and the exact idempotency ``appProperty`` value — never a
search by filename, title, or owner. Zero matches allow a create; exactly one
compatible match is reused; any incompatible or ambiguous match fails closed
(no overwrite, no delete, no silent pick).

Retry ownership stays with ``LegacyMigrationExecutor`` (see
``_run_operation``): this adapter performs exactly one attempt per call and
maps client-reported error categories onto the executor's exception
vocabulary instead of looping internally.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Protocol

from legacy_publication_migration_executor import (
    ConflictingStorageObjectError,
    LegacyExecutorError,
    ResponseLostError,
    TerminalBackendError,
    TransientBackendError,
    ValidationError,
)
from legacy_publication_migrator import DryRunResult, LegacyMigrationInput


APP_PROP_SCHEMA = "tradutor_legacy_schema"
APP_PROP_SCHEMA_VERSION = "1"
APP_PROP_SHA256 = "tradutor_legacy_sha256"
APP_PROP_IDEMPOTENCY = "tradutor_legacy_idempotency"
DRIVE_METADATA_FIELDS = ("id", "name", "mimeType", "size", "parents", "trashed", "md5Checksum", "appProperties")
PDF_MIME_TYPE = "application/pdf"


class DriveClientError(Exception):
    """Raised by a ``DriveFilesClient`` implementation for any failed call.

    ``category`` drives the mapping onto the executor's exception vocabulary:
    transient | response_lost | authentication | permission | not_found |
    validation | unknown.
    """

    def __init__(self, message: str, *, category: str = "unknown") -> None:
        super().__init__(message)
        self.category = category


class DriveFilesClient(Protocol):
    """Minimal, injectable Drive Files surface this adapter needs.

    Deliberately narrower than a full Drive SDK: no folder creation, no
    search-by-name, no update/delete. A real implementation would translate
    ``find_by_idempotency``'s pre-built ``query`` into a ``files.list`` call
    and ``create_pdf`` into a single-shot multipart upload with
    ``appProperties`` attached; wiring that real implementation (auth, HTTP
    transport) is out of scope for this hermetic phase.
    """

    def get_metadata(self, file_id: str, *, fields: tuple[str, ...]) -> dict[str, Any] | None: ...

    def find_by_idempotency(self, *, query: str, fields: tuple[str, ...], page_size: int) -> list[dict[str, Any]]: ...

    def create_pdf(self, *, folder_id: str, filename: str, data: bytes, app_properties: dict[str, str]) -> dict[str, Any]: ...


def escape_drive_query_literal(value: str) -> str:
    """Escape a single-quoted Drive ``q`` string literal.

    Drive's query grammar only requires escaping backslash and single quote
    inside a quoted literal; anything else (including newlines and the
    keywords ``and``/``or``) stays inert once it is inside the quotes.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


def build_idempotency_query(folder_id: str, idempotency_key: str) -> str:
    """Build a query restricted to one appProperty, one folder, not-trashed.

    Never references filename or title, so this can never widen into a
    broad/ambiguous search.
    """
    return (
        f"appProperties has {{ key='{escape_drive_query_literal(APP_PROP_IDEMPOTENCY)}' "
        f"and value='{escape_drive_query_literal(idempotency_key)}' }} "
        f"and '{escape_drive_query_literal(folder_id)}' in parents and trashed=false"
    )


def derive_idempotency_key(migration_input: LegacyMigrationInput, plan: DryRunResult) -> str:
    """Deterministic, opaque upload identity — no timestamp, UUID, or filename.

    Built only from material the executor already guarantees is stable for a
    given plan: the planner's canonical ``idempotency_key`` (source_system +
    source_instance_id + legacy_publication_id) plus the artifact's own
    SHA-256 and size. A retry after a lost response recomputes the exact same
    key from the same inputs.
    """
    artifact = migration_input.publication.artifact
    material = f"{plan.idempotency_key}:{artifact.pdf_sha256}:{artifact.pdf_size}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def map_drive_error(exc: DriveClientError) -> LegacyExecutorError:
    category = getattr(exc, "category", "unknown")
    message = str(exc) or category
    if category == "response_lost":
        return ResponseLostError(message)
    if category == "transient":
        return TransientBackendError(message)
    if category in ("authentication", "permission"):
        return TerminalBackendError(f"drive_{category}_failure:{message}")
    if category == "not_found":
        return TerminalBackendError(f"drive_not_found:{message}")
    if category == "validation":
        return ValidationError(message)
    return TerminalBackendError(f"drive_unknown_error:{message}")


def _safe_filename(name: str) -> str:
    base = Path(name).name
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return cleaned or "artifact.pdf"


class GoogleDriveLegacyArtifactStorage:
    """``LegacyArtifactStorage`` backed by an injected hermetic Drive client."""

    def __init__(self, *, drive_client: DriveFilesClient, folder_id: str) -> None:
        if drive_client is None:
            raise ValueError("drive_client_required")
        if not folder_id:
            raise ValueError("folder_id_required")
        self._client = drive_client
        self._folder_id = folder_id
        self.upload_count = 0

    def has_conflict(self, plan_digest: str) -> bool:
        # This adapter keeps no local cache of prior uploads, so it has no
        # cheap way to answer this precheck authoritatively. Real conflict
        # detection happens inside upload()'s narrow idempotency lookup,
        # which fails closed on any incompatible or ambiguous match.
        return False

    def use_verified_remote_asset(self, migration_input: LegacyMigrationInput, plan: DryRunResult) -> dict[str, Any]:
        remote_asset = migration_input.publication.remote_asset or {}
        file_id = str(remote_asset.get("storage_file_id") or "")
        if not file_id:
            raise ValidationError("remote_asset_missing_storage_file_id")
        try:
            metadata = self._client.get_metadata(file_id, fields=DRIVE_METADATA_FIELDS)
        except DriveClientError as exc:
            raise map_drive_error(exc) from exc
        if metadata is None:
            raise TerminalBackendError("drive_not_found")
        expected = migration_input.publication.artifact
        app_properties = metadata.get("appProperties") or {}
        sha256 = app_properties.get(APP_PROP_SHA256)
        if not sha256:
            # A pre-existing Drive file without our provenance property can
            # never be silently promoted to "strong" here, even though the
            # planner already required remote_asset_verification == "strong".
            raise ValidationError("legacy_asset_missing_sha256_provenance")
        if (
            metadata.get("mimeType") != PDF_MIME_TYPE
            or bool(metadata.get("trashed"))
            or int(metadata.get("size") or -1) != expected.pdf_size
            or sha256 != expected.pdf_sha256
        ):
            raise ValidationError("remote_asset_verification_failed")
        return {
            "storage_provider": "google_drive",
            "storage_file_id": file_id,
            "size": int(metadata["size"]),
            "sha256": sha256,
            "action": "reuse_verified_remote_asset",
        }

    def upload(self, migration_input: LegacyMigrationInput, plan: DryRunResult) -> dict[str, Any]:
        artifact = migration_input.publication.artifact
        path = artifact.local_artifact_path
        data = path.read_bytes()
        if not data.startswith(b"%PDF-"):
            raise ValidationError("artifact_not_pdf")
        sha256 = hashlib.sha256(data).hexdigest()
        size = len(data)
        if sha256 != artifact.pdf_sha256 or size != artifact.pdf_size:
            # Rehash immediately before sending bytes: defense in depth on
            # top of the executor's own pre-dispatch artifact revalidation.
            raise ValidationError("artifact_changed_after_plan")
        md5 = hashlib.md5(data).hexdigest()
        idempotency_key = derive_idempotency_key(migration_input, plan)
        query = build_idempotency_query(self._folder_id, idempotency_key)
        try:
            matches = self._client.find_by_idempotency(query=query, fields=DRIVE_METADATA_FIELDS, page_size=3)
        except DriveClientError as exc:
            raise map_drive_error(exc) from exc
        if len(matches) > 1:
            raise ConflictingStorageObjectError("drive_idempotency_lookup_ambiguous")
        if len(matches) == 1:
            existing = matches[0]
            if not self._is_compatible(existing, sha256=sha256, size=size, md5=md5):
                raise ConflictingStorageObjectError("drive_idempotency_conflict")
            return self._asset_from_metadata(existing, action="already_exists_same_content")
        app_properties = {
            APP_PROP_SCHEMA: APP_PROP_SCHEMA_VERSION,
            APP_PROP_SHA256: sha256,
            APP_PROP_IDEMPOTENCY: idempotency_key,
        }
        try:
            created = self._client.create_pdf(
                folder_id=self._folder_id,
                filename=_safe_filename(path.name),
                data=data,
                app_properties=app_properties,
            )
        except DriveClientError as exc:
            raise map_drive_error(exc) from exc
        if not self._is_compatible(created, sha256=sha256, size=size, md5=md5):
            raise TerminalBackendError("storage_integrity_mismatch")
        self.upload_count += 1
        return self._asset_from_metadata(created, action="drive_upload")

    def _is_compatible(self, metadata: dict[str, Any], *, sha256: str, size: int, md5: str) -> bool:
        if metadata.get("mimeType") != PDF_MIME_TYPE:
            return False
        if bool(metadata.get("trashed")):
            return False
        if int(metadata.get("size") or -1) != size:
            return False
        if metadata.get("md5Checksum") != md5:
            return False
        app_properties = metadata.get("appProperties") or {}
        if app_properties.get(APP_PROP_SHA256) != sha256:
            return False
        parents = metadata.get("parents") or []
        if not parents or parents[0] != self._folder_id:
            return False
        return True

    @staticmethod
    def _asset_from_metadata(metadata: dict[str, Any], *, action: str) -> dict[str, Any]:
        app_properties = metadata.get("appProperties") or {}
        return {
            "storage_provider": "google_drive",
            "storage_file_id": metadata["id"],
            "size": int(metadata["size"]),
            "sha256": app_properties.get(APP_PROP_SHA256),
            "action": action,
        }
