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
from urllib.parse import quote

from supabase_social import SocialConfig, SupabaseDataClient


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
    def list_publications(self, *, owner_user_id: str | None = None,
                          status: str = "published", limit: int = 100) -> list[PublicationMetadata]: ...


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

    def list_publications(self, *, owner_user_id: str | None = None,
                          status: str = "published", limit: int = 100) -> list[PublicationMetadata]:
        return []


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
            self._enforce_unique_active_artifact(publication_id, metadata)
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
            self._enforce_unique_active_artifact(publication_id, metadata)
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
            existing = self._rows.get(str(publication_id))
            if existing:
                self._rows[str(publication_id)] = PublicationMetadata(
                    **{**existing.__dict__, "publication_status": "failed_retryable",
                       "updated_at": time.time()}
                )

    def get_publication(self, publication_id: str) -> PublicationMetadata | None:
        with self._lock:
            return self._rows.get(str(publication_id))

    def list_publications(self, *, owner_user_id: str | None = None,
                          status: str = "published", limit: int = 100) -> list[PublicationMetadata]:
        with self._lock:
            rows = [r for r in self._rows.values() if r.publication_status == status]
            if owner_user_id is not None:
                rows = [r for r in rows if r.owner_user_id == str(owner_user_id)]
            rows.sort(key=lambda r: (r.created_at, r.publication_id), reverse=True)
            return rows[:max(1, min(int(limit), 100))]

    def _enforce_unique_active_artifact(self, publication_id: str, metadata: dict[str, Any]) -> None:
        owner = str(metadata["owner_user_id"])
        artifact_sha = str(metadata["artifact_sha256"])
        job_id = str(metadata.get("job_id") or "")
        run_id = str(metadata.get("run_id") or "")
        active = {"reserved", "uploading", "uploaded", "finalizing", "published"}
        for other_id, row in self._rows.items():
            if other_id == publication_id or row.owner_user_id != owner or row.publication_status not in active:
                continue
            if row.artifact_sha256 == artifact_sha:
                raise PublicationMetadataError("duplicate_artifact_publication")
            if job_id and run_id and row.job_id == job_id and row.run_id == run_id:
                raise PublicationMetadataError("duplicate_publication_run")


PUBLICATION_ARTIFACT_FIELDS = (
    "publication_id,owner_user_id,job_id,run_id,artifact_sha256,artifact_size_bytes,"
    "mime_type,storage_provider,storage_reference,publication_status,title,last_error_code,"
    "created_at,updated_at"
)
PUBLICATION_ARTIFACT_TABLE = "community_publication_artifacts"


def _q(value: Any) -> str:
    return quote(str(value), safe="")


def _metadata_from_row(row: dict[str, Any]) -> PublicationMetadata:
    return PublicationMetadata(
        publication_id=str(row.get("publication_id") or ""),
        owner_user_id=str(row.get("owner_user_id") or ""),
        job_id=str(row.get("job_id") or ""),
        run_id=str(row.get("run_id") or ""),
        artifact_sha256=str(row.get("artifact_sha256") or ""),
        artifact_size_bytes=int(row.get("artifact_size_bytes") or 0),
        mime_type=str(row.get("mime_type") or "application/pdf"),
        storage_provider=str(row.get("storage_provider") or ""),
        storage_reference=str(row.get("storage_reference") or ""),
        publication_status=str(row.get("publication_status") or ""),
        title=str(row.get("title") or ""),
        created_at=row.get("created_at") or 0.0,
        updated_at=row.get("updated_at") or 0.0,
    )


def _row_from_metadata(metadata: dict[str, Any], *, status: str, include_storage: bool) -> dict[str, Any]:
    row = {
        "publication_id": str(metadata["publication_id"]),
        "owner_user_id": str(metadata["owner_user_id"]),
        "job_id": str(metadata.get("job_id") or ""),
        "run_id": str(metadata.get("run_id") or ""),
        "artifact_sha256": str(metadata["artifact_sha256"]),
        "artifact_size_bytes": int(metadata["artifact_size_bytes"]),
        "mime_type": str(metadata.get("mime_type") or "application/pdf"),
        "storage_provider": str(metadata["storage_provider"]),
        "publication_status": status,
        "title": str(metadata.get("title") or ""),
        "last_error_code": None,
    }
    if include_storage:
        storage_reference = str(metadata.get("storage_reference") or "")
        if not storage_reference:
            raise PublicationMetadataError("missing_storage_reference")
        row["storage_reference"] = storage_reference
    return row


class SupabasePublicationMetadataRepository:
    """Backend-only publication artifact metadata over Supabase/PostgREST.

    The token passed here must be provisioned by the server environment. Browser callers
    never provide or see storage_reference; public DTOs must use ``PublicationMetadata.public``.
    Transport is injectable so tests are hermetic and perform no network I/O.
    """

    provider = "supabase"

    def __init__(self, config: SocialConfig, *, access_token: str, transport=None):
        if not str(access_token or "").strip():
            raise PublicationMetadataError("supabase_publication_metadata_not_configured")
        self._config = config
        self._access_token = str(access_token)
        self._transport = transport

    def _client(self) -> SupabaseDataClient:
        return SupabaseDataClient(self._config, self._access_token, transport=self._transport)

    def reserve(self, metadata: dict[str, Any]) -> None:
        self._client().upsert(
            PUBLICATION_ARTIFACT_TABLE,
            _row_from_metadata(metadata, status="reserved", include_storage=False),
            on_conflict="publication_id",
            returning=PUBLICATION_ARTIFACT_FIELDS,
        )

    def finalize(self, metadata: dict[str, Any]) -> None:
        rows = self._client().update(
            PUBLICATION_ARTIFACT_TABLE,
            match=f"publication_id=eq.{_q(metadata['publication_id'])}",
            row=_row_from_metadata(metadata, status="published", include_storage=True),
            returning=PUBLICATION_ARTIFACT_FIELDS,
        )
        if not rows:
            raise PublicationMetadataError("publication_metadata_not_reserved")

    def mark_failed(self, publication_id: str, *, reason: str) -> None:
        rows = self._client().update(
            PUBLICATION_ARTIFACT_TABLE,
            match=f"publication_id=eq.{_q(publication_id)}",
            row={"publication_status": "failed_retryable", "last_error_code": str(reason or "failed")},
            returning=PUBLICATION_ARTIFACT_FIELDS,
        )
        if not rows:
            raise PublicationMetadataError("publication_metadata_not_reserved")

    def get_publication(self, publication_id: str) -> PublicationMetadata | None:
        rows = self._client().select(
            PUBLICATION_ARTIFACT_TABLE,
            query=f"select={PUBLICATION_ARTIFACT_FIELDS}&publication_id=eq.{_q(publication_id)}&limit=1",
        )
        return _metadata_from_row(rows[0]) if rows else None

    def list_publications(self, *, owner_user_id: str | None = None,
                          status: str = "published", limit: int = 100) -> list[PublicationMetadata]:
        safe_limit = max(1, min(int(limit), 100))
        filters = [
            f"select={PUBLICATION_ARTIFACT_FIELDS}",
            f"publication_status=eq.{_q(status)}",
            "order=created_at.desc,publication_id.desc",
            f"limit={safe_limit}",
        ]
        if owner_user_id:
            filters.insert(2, f"owner_user_id=eq.{_q(owner_user_id)}")
        return [_metadata_from_row(r) for r in self._client().select(
            PUBLICATION_ARTIFACT_TABLE, query="&".join(filters))]


def build_publication_metadata_repository(config: dict[str, Any] | None) -> PublicationMetadataRepository:
    provider = str((config or {}).get("provider") or "none").strip().lower()
    if provider in {"", "none", "null"}:
        return NullPublicationMetadataRepository()
    if provider == "fake":
        return FakePublicationMetadataRepository()
    if provider == "supabase":
        cfg = config or {}
        token = str(cfg.get("access_token") or cfg.get("backend_access_token") or "").strip()
        if not token:
            raise PublicationMetadataError("supabase_publication_metadata_not_configured")
        url = str(cfg.get("url") or "").strip().rstrip("/")
        key = str(cfg.get("publishable_key") or "").strip()
        if not url or not key:
            raise PublicationMetadataError("supabase_publication_metadata_not_configured")
        social_config = SocialConfig(url=url, publishable_key=key)
        return SupabasePublicationMetadataRepository(
            social_config, access_token=token, transport=cfg.get("transport"))
    raise PublicationMetadataError("unsupported_publication_metadata_provider")
