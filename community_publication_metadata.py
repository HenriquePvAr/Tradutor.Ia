"""Publication metadata boundary for the Supabase-target Community model.

The PDF bytes stay in the configured ``StorageProvider`` (Google Drive in
production).  This boundary stores only structured publication metadata and an
opaque server-side storage reference.  It is intentionally injectable so tests
exercise the publish/storage contract with zero Supabase network calls.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol


class PublicationMetadataError(RuntimeError):
    """Safe metadata-layer failure used by the publish runner."""


@dataclass(frozen=True)
class PublicationMetadata:
    publication_id: str
    owner_user_id: str
    job_id: str
    run_id: str
    artifact_sha256: str
    artifact_size_bytes: int
    mime_type: str
    storage_provider: str
    storage_reference: str
    publication_status: str
    title: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def public(self) -> dict[str, Any]:
        """Metadata safe for future viewer routing; never exposes local paths."""
        return {
            "publication_id": self.publication_id,
            "owner_user_id": self.owner_user_id,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "artifact_sha256": self.artifact_sha256,
            "artifact_size_bytes": self.artifact_size_bytes,
            "mime_type": self.mime_type,
            "storage_provider": self.storage_provider,
            "publication_status": self.publication_status,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class PublicationMetadataRepository(Protocol):
    def reserve(self, metadata: dict[str, Any]) -> None: ...
    def finalize(self, metadata: dict[str, Any]) -> None: ...
    def mark_failed(self, publication_id: str, *, reason: str) -> None: ...
    def get_publication(self, publication_id: str) -> PublicationMetadata | None: ...


class NullPublicationMetadataRepository:
    """Default legacy bridge: no remote metadata side effect."""

    def reserve(self, metadata: dict[str, Any]) -> None:
        return None

    def finalize(self, metadata: dict[str, Any]) -> None:
        return None

    def mark_failed(self, publication_id: str, *, reason: str) -> None:
        return None

    def get_publication(self, publication_id: str) -> PublicationMetadata | None:
        return None


class FakePublicationMetadataRepository:
    """Hermetic Supabase-shaped metadata repository for tests."""

    def __init__(self, *, fail_reserve: bool = False, fail_finalize_once: bool = False):
        self.fail_reserve = fail_reserve
        self.fail_finalize_once = fail_finalize_once
        self.reserve_calls = 0
        self.finalize_calls = 0
        self.failed_calls: list[dict[str, str]] = []
        self._rows: dict[str, PublicationMetadata] = {}
        self._lock = threading.Lock()

    def reserve(self, metadata: dict[str, Any]) -> None:
        with self._lock:
            self.reserve_calls += 1
            if self.fail_reserve:
                raise PublicationMetadataError("metadata_reservation_failed")
            publication_id = str(metadata["publication_id"])
            now = time.time()
            existing = self._rows.get(publication_id)
            if existing and existing.publication_status == "published":
                return
            self._rows[publication_id] = PublicationMetadata(
                publication_id=publication_id,
                owner_user_id=str(metadata["owner_user_id"]),
                job_id=str(metadata.get("job_id") or ""),
                run_id=str(metadata.get("run_id") or ""),
                artifact_sha256=str(metadata["artifact_sha256"]),
                artifact_size_bytes=int(metadata["artifact_size_bytes"]),
                mime_type=str(metadata.get("mime_type") or "application/pdf"),
                storage_provider=str(metadata["storage_provider"]),
                storage_reference=str(metadata.get("storage_reference") or ""),
                publication_status="reserved",
                title=str(metadata.get("title") or ""),
                created_at=existing.created_at if existing else now,
                updated_at=now,
            )

    def finalize(self, metadata: dict[str, Any]) -> None:
        with self._lock:
            self.finalize_calls += 1
            if self.fail_finalize_once:
                self.fail_finalize_once = False
                raise PublicationMetadataError("metadata_finalization_failed")
            if not str(metadata.get("storage_reference") or ""):
                raise PublicationMetadataError("missing_storage_reference")
            publication_id = str(metadata["publication_id"])
            existing = self._rows.get(publication_id)
            if (
                existing
                and existing.publication_status == "published"
                and existing.storage_reference == str(metadata["storage_reference"])
            ):
                return
            now = time.time()
            self._rows[publication_id] = PublicationMetadata(
                publication_id=publication_id,
                owner_user_id=str(metadata["owner_user_id"]),
                job_id=str(metadata.get("job_id") or ""),
                run_id=str(metadata.get("run_id") or ""),
                artifact_sha256=str(metadata["artifact_sha256"]),
                artifact_size_bytes=int(metadata["artifact_size_bytes"]),
                mime_type=str(metadata.get("mime_type") or "application/pdf"),
                storage_provider=str(metadata["storage_provider"]),
                storage_reference=str(metadata["storage_reference"]),
                publication_status="published",
                title=str(metadata.get("title") or ""),
                created_at=existing.created_at if existing else now,
                updated_at=now,
            )

    def mark_failed(self, publication_id: str, *, reason: str) -> None:
        with self._lock:
            self.failed_calls.append({
                "publication_id": str(publication_id),
                "reason": str(reason),
            })

    def get_publication(self, publication_id: str) -> PublicationMetadata | None:
        with self._lock:
            return self._rows.get(str(publication_id))


def build_publication_metadata_repository(config: dict[str, Any] | None) -> PublicationMetadataRepository:
    provider = str((config or {}).get("provider") or "none").strip().lower()
    if provider in {"", "none", "null"}:
        return NullPublicationMetadataRepository()
    if provider == "fake":
        return FakePublicationMetadataRepository()
    raise PublicationMetadataError("unsupported_publication_metadata_provider")
