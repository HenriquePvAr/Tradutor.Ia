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
import math
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

# --- Hard create deadline contract ------------------------------------------
#
# STORAGE CREATE DEADLINE (this contract) vs RESERVATION LEASE TTL (a future,
# NOT-yet-implemented concern) are two different numbers answering two
# different questions:
#   - create deadline:  how long is ONE external create/upload attempt
#                        allowed to run before this adapter must treat its
#                        outcome as unknown/failed?
#   - lease TTL:         how long can another worker be blocked from
#                         attempting the same logical create while this one
#                         is in flight? (see minimum_safe_create_lease_ttl)
#
# A future lease TTL is only defensible once it is proven to exceed this
# deadline plus a safety margin (see minimum_safe_create_lease_ttl below).
# Reservation/leasing itself is out of scope here.
#
# Bounds are sized for a PDF upload (not an instant metadata call): a few
# seconds is too tight for a real multipart/resumable Drive upload under
# normal network conditions, and an unbounded value defeats the point of a
# hard deadline entirely. These are conservative placeholders for this
# hermetic phase -- the real DriveFilesClient implementation must validate
# its own operational numbers against actual Drive upload behavior during
# the integration phase; this contract only guarantees "bounded and
# explicit", not "final".
MIN_CREATE_DEADLINE_SECONDS = 5.0
MAX_CREATE_DEADLINE_SECONDS = 300.0


def validate_create_deadline_seconds(value: Any) -> float:
    """Fail-closed validation for the hard Drive create deadline.

    Rejects everything that would make the deadline effectively unbounded or
    meaningless: missing (``None``), non-numeric, ``bool`` (a ``bool`` is an
    ``int`` subclass but is never a legitimate deadline value), ``NaN``,
    ``Infinity``, non-positive, or outside ``[MIN_CREATE_DEADLINE_SECONDS,
    MAX_CREATE_DEADLINE_SECONDS]``. Raises before any Drive call is made.
    """
    if value is None:
        raise ValueError("create_deadline_seconds_required")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("create_deadline_seconds_must_be_numeric")
    numeric = float(value)
    if math.isnan(numeric) or math.isinf(numeric):
        raise ValueError("create_deadline_seconds_must_be_finite")
    if numeric <= 0:
        raise ValueError("create_deadline_seconds_must_be_positive")
    if not (MIN_CREATE_DEADLINE_SECONDS <= numeric <= MAX_CREATE_DEADLINE_SECONDS):
        raise ValueError("create_deadline_seconds_out_of_bounds")
    return numeric


def minimum_safe_create_lease_ttl(create_deadline_seconds: float, safety_margin_seconds: float) -> float:
    """Formal relationship a future reservation lease TTL must satisfy.

    Conceptual only -- no reservation/lease is implemented by this module.
    A future lease generation must pick ``lease_create_ttl`` such that::

        lease_create_ttl > create_deadline_seconds + safety_margin_seconds

    ``safety_margin_seconds`` must cover at least: scheduler dispatch delay
    before the create attempt actually starts, the HTTP response returning
    after the provider-side operation finishes, this adapter's own
    post-create metadata verification (``_is_compatible``), and the
    provenance DB round trip that durably records completion. It must NOT
    be inflated to also cover ``LegacyMigrationExecutor``'s overall retry
    budget (``max_attempts``): each retry attempt gets its own
    ``create_deadline_seconds`` window and, in a future design, its own
    lease generation -- the lease does not need to span every retry combined.
    """
    if safety_margin_seconds <= 0:
        raise ValueError("safety_margin_seconds_must_be_positive")
    return create_deadline_seconds + safety_margin_seconds


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

    ``create_pdf`` requires an explicit, already-validated
    ``create_deadline_seconds`` (see ``validate_create_deadline_seconds``).
    Requirements a real implementation must satisfy that this hermetic
    protocol cannot enforce by itself:
      - The deadline must bound the entire create/upload end-to-end, not
        just a single socket read per chunk. Drive PDF uploads may use
        multipart or resumable upload semantics; a real client must apply
        an end-to-end deadline across the whole resumable session, not a
        per-chunk timeout that lets the overall upload run unbounded.
        Resumable upload session semantics (chunking, restart tokens) need
        their own separate audit before that client is trusted.
      - connect/read/write/request timeouts must all be explicit; no
        operation may be left eternally active on the default SDK/transport
        timeout.
      - When the deadline is reached, the client must actively cancel or
        close the underlying request/connection where the transport
        supports it -- a caller-side ``timeout`` that merely stops waiting
        while the HTTP request keeps running in the background does NOT
        satisfy this contract.
      - An outcome that is unknown when the deadline is reached (the file
        may or may not have been created) must never be reported as a
        clean/safe failure. It must be surfaced as ``category="response_lost"``
        (recovery_required) so the caller performs the adapter's narrow
        idempotency lookup instead of blindly retrying a create.
    """

    def get_metadata(self, file_id: str, *, fields: tuple[str, ...]) -> dict[str, Any] | None: ...

    def find_by_idempotency(self, *, query: str, fields: tuple[str, ...], page_size: int) -> list[dict[str, Any]]: ...

    def create_pdf(
        self,
        *,
        folder_id: str,
        filename: str,
        data: bytes,
        app_properties: dict[str, str],
        create_deadline_seconds: float,
    ) -> dict[str, Any]: ...


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

    def __init__(self, *, drive_client: DriveFilesClient, folder_id: str, create_deadline_seconds: Any) -> None:
        if drive_client is None:
            raise ValueError("drive_client_required")
        if not folder_id:
            raise ValueError("folder_id_required")
        # Infrastructure policy, set once at construction by whoever wires
        # this backend -- never sourced from the environment, the legacy
        # manifest, or publication input. See validate_create_deadline_seconds.
        self._create_deadline_seconds = validate_create_deadline_seconds(create_deadline_seconds)
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
                create_deadline_seconds=self._create_deadline_seconds,
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
