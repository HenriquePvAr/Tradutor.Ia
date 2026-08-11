"""Publication metadata boundary for the Supabase-target Community model.

The PDF bytes stay in the configured ``StorageProvider`` (Google Drive in
production).  This boundary stores only structured publication metadata and an
opaque server-side storage reference.  It is intentionally injectable so tests
exercise the publish/storage contract with zero Supabase network calls.
"""

from __future__ import annotations

import threading
import time
import json
import os
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from supabase_social import SocialConfig


class PublicationMetadataError(RuntimeError):
    """Safe metadata-layer failure used by the publish runner."""


SENSITIVE_CONFIG_KEYS = frozenset({
    "supabase_secret_key",
    "supabase_service_role_key",
    "authorization",
    "apikey",
    "token",
    "access_token",
    "refresh_token",
    "password",
    "secret",
    "secret_key",
    "backend_secret_key",
})


def _is_sensitive_key(key: Any) -> bool:
    normalized = str(key or "").strip().lower()
    return any(marker in normalized for marker in SENSITIVE_CONFIG_KEYS)


def redacted_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a diagnostics-safe config view without secret values."""

    safe: dict[str, Any] = {}
    for key, value in dict(config or {}).items():
        safe[key] = "<redacted>" if _is_sensitive_key(key) else value
    return safe


class PublicationMetadataRepositoryConfig(dict):
    """Dict-compatible config whose repr/str never reveal secret-bearing fields."""

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.update(*args, **kwargs)

    def __setitem__(self, key, value):
        super().__setitem__(key, _wrap_secret_value(key, value))

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def __repr__(self) -> str:
        return repr(redacted_config(self))

    __str__ = __repr__

    def safe_debug_dict(self) -> dict[str, Any]:
        return redacted_config(self)


class SecretHeaderDict(dict):
    """Dict-compatible HTTP headers whose repr/str redact credential values."""

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.update(*args, **kwargs)

    def __setitem__(self, key, value):
        super().__setitem__(key, _wrap_secret_value(key, value))

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def __repr__(self) -> str:
        return repr(redacted_config(self))

    __str__ = __repr__


class SecretValue(str):
    """String value with redacted representation for assertion/log diffs."""

    def __repr__(self) -> str:
        return "'<redacted>'"


def _wrap_secret_value(key: Any, value: Any) -> Any:
    if _is_sensitive_key(key) and isinstance(value, str):
        return SecretValue(value)
    return value


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
    def record_storage(self, metadata: dict[str, Any]) -> None: ...
    def finalize(self, metadata: dict[str, Any]) -> None: ...
    def mark_failed(self, publication_id: str, *, reason: str) -> None: ...
    def get_publication(self, publication_id: str) -> PublicationMetadata | None: ...
    def list_publications(self, *, owner_user_id: str | None = None,
                          status: str = "published", limit: int = 100) -> list[PublicationMetadata]: ...


class NullPublicationMetadataRepository:
    """Default legacy bridge: no remote metadata side effect."""

    def reserve(self, metadata: dict[str, Any]) -> None:
        return None

    def record_storage(self, metadata: dict[str, Any]) -> None:
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

    def record_storage(self, metadata: dict[str, Any]) -> None:
        with self._lock:
            if not str(metadata.get("storage_reference") or ""):
                raise PublicationMetadataError("missing_storage_reference")
            publication_id = str(metadata["publication_id"])
            existing = self._rows.get(publication_id)
            if not existing:
                raise PublicationMetadataError("publication_metadata_not_reserved")
            if existing.owner_user_id != str(metadata["owner_user_id"]):
                raise PublicationMetadataError("publication_owner_mismatch")
            if existing.artifact_sha256 != str(metadata["artifact_sha256"]):
                raise PublicationMetadataError("publication_artifact_mismatch")
            if existing.artifact_size_bytes != int(metadata["artifact_size_bytes"]):
                raise PublicationMetadataError("publication_artifact_mismatch")
            storage_reference = str(metadata["storage_reference"])
            if existing.storage_reference and existing.storage_reference != storage_reference:
                raise PublicationMetadataError("storage_reference_conflict")
            if existing.publication_status in {"failed_terminal"}:
                raise PublicationMetadataError("invalid_publication_transition")
            self._rows[publication_id] = PublicationMetadata(
                **{
                    **existing.__dict__,
                    "storage_reference": storage_reference,
                    "publication_status": (
                        "published" if existing.publication_status == "published" else "uploaded"
                    ),
                    "updated_at": time.time(),
                }
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
            if not existing:
                raise PublicationMetadataError("publication_metadata_not_reserved")
            if (
                existing
                and existing.publication_status == "published"
                and existing.storage_reference == str(metadata["storage_reference"])
            ):
                return
            if existing.publication_status not in {"uploaded", "finalizing"}:
                raise PublicationMetadataError("invalid_publication_transition")
            if existing.storage_reference != str(metadata["storage_reference"]):
                raise PublicationMetadataError("storage_reference_conflict")
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


RPC_RESERVE = "reserve_community_publication_artifact"
RPC_RECORD_STORAGE = "record_community_publication_storage"
RPC_FINALIZE = "finalize_community_publication_artifact"
RPC_MARK_FAILURE = "mark_community_publication_artifact_failure"


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
    """Backend-only publication artifact metadata over narrow Supabase RPCs.

    The token passed here must be provisioned by the server environment. Browser callers
    never provide or see storage_reference; public DTOs must use ``PublicationMetadata.public``.
    Transport is injectable so tests are hermetic and perform no network I/O.
    """

    provider = "supabase"

    def __init__(self, config: SocialConfig, *, secret_key: str, transport=None):
        secret = str(secret_key or "").strip()
        if not secret or not secret.startswith("sb_secret_"):
            raise PublicationMetadataError("supabase_publication_metadata_not_configured")
        self._config = config
        self._secret_key = secret
        self._transport = transport

    def _transport_client(self):
        if self._transport is not None:
            return self._transport
        from google_drive_transport import RequestsHttpTransport
        return RequestsHttpTransport(connect_timeout=10.0, read_timeout=20.0)

    def _headers(self) -> dict[str, str]:
        return SecretHeaderDict({
            "apikey": self._secret_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def _rpc(self, function_name: str, payload: dict[str, Any]) -> Any:
        url = f"{self._config.rest_url}/rpc/{function_name}"
        try:
            resp = self._transport_client().request(
                "POST",
                url,
                headers=self._headers(),
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )
        except Exception as exc:
            raise PublicationMetadataError("publication_metadata_backend_unavailable") from exc
        raw = resp.content or b""
        parsed: Any = None
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                parsed = None
        if resp.status >= 400:
            if resp.status in {401, 403}:
                code = "publication_metadata_backend_not_authorized"
            elif resp.status == 409:
                code = "publication_metadata_conflict"
            elif resp.status in {400, 422}:
                code = "publication_metadata_invariant_failed"
            elif resp.status >= 500:
                code = "publication_metadata_backend_unavailable"
            else:
                code = "publication_metadata_backend_error"
            raise PublicationMetadataError(code)
        return parsed

    @staticmethod
    def _metadata_from_rpc_result(parsed: Any) -> PublicationMetadata:
        if isinstance(parsed, list):
            if not parsed:
                raise PublicationMetadataError("publication_metadata_not_found")
            row = parsed[0]
        else:
            row = parsed
        if not isinstance(row, dict):
            raise PublicationMetadataError("publication_metadata_malformed_response")
        return _metadata_from_row(row)

    @staticmethod
    def _base_payload(metadata: dict[str, Any]) -> dict[str, Any]:
        return {
            "p_publication_id": str(metadata["publication_id"]),
            "p_owner_user_id": str(metadata["owner_user_id"]),
            "p_job_id": str(metadata.get("job_id") or ""),
            "p_run_id": str(metadata.get("run_id") or ""),
            "p_artifact_sha256": str(metadata["artifact_sha256"]),
            "p_artifact_size_bytes": int(metadata["artifact_size_bytes"]),
            "p_mime_type": str(metadata.get("mime_type") or "application/pdf"),
            "p_storage_provider": str(metadata["storage_provider"]),
            "p_title": str(metadata.get("title") or ""),
        }

    def reserve(self, metadata: dict[str, Any]) -> None:
        self._rpc(RPC_RESERVE, self._base_payload(metadata))

    def record_storage(self, metadata: dict[str, Any]) -> None:
        storage_reference = str(metadata.get("storage_reference") or "")
        if not storage_reference:
            raise PublicationMetadataError("missing_storage_reference")
        payload = {
            "p_publication_id": str(metadata["publication_id"]),
            "p_owner_user_id": str(metadata["owner_user_id"]),
            "p_artifact_sha256": str(metadata["artifact_sha256"]),
            "p_artifact_size_bytes": int(metadata["artifact_size_bytes"]),
            "p_storage_provider": str(metadata["storage_provider"]),
            "p_storage_reference": storage_reference,
        }
        self._rpc(RPC_RECORD_STORAGE, payload)

    def finalize(self, metadata: dict[str, Any]) -> None:
        payload = {
            "p_publication_id": str(metadata["publication_id"]),
            "p_owner_user_id": str(metadata["owner_user_id"]),
            "p_artifact_sha256": str(metadata["artifact_sha256"]),
            "p_artifact_size_bytes": int(metadata["artifact_size_bytes"]),
        }
        self._rpc(RPC_FINALIZE, payload)

    def mark_failed(self, publication_id: str, *, reason: str) -> None:
        self._rpc(RPC_MARK_FAILURE, {
            "p_publication_id": str(publication_id),
            "p_failure_status": "failed_retryable",
            "p_last_error_code": str(reason or "failed"),
        })

    def get_publication(self, publication_id: str) -> PublicationMetadata | None:
        raise PublicationMetadataError("publication_metadata_read_rpc_not_configured")

    def list_publications(self, *, owner_user_id: str | None = None,
                          status: str = "published", limit: int = 100) -> list[PublicationMetadata]:
        raise PublicationMetadataError("publication_metadata_read_rpc_not_configured")


def build_publication_metadata_repository(config: dict[str, Any] | None) -> PublicationMetadataRepository:
    provider = str((config or {}).get("provider") or "none").strip().lower()
    if provider in {"", "none", "null"}:
        return NullPublicationMetadataRepository()
    if provider == "fake":
        return FakePublicationMetadataRepository()
    if provider == "supabase":
        cfg = config or {}
        secret_key = str(cfg.get("secret_key") or cfg.get("backend_secret_key") or "").strip()
        secret_env_var = str(cfg.get("secret_env_var") or "").strip()
        if not secret_key and secret_env_var:
            secret_key = str(os.environ.get(secret_env_var) or "").strip()
        if not secret_key or not secret_key.startswith("sb_secret_"):
            raise PublicationMetadataError("supabase_publication_metadata_not_configured")
        url = str(cfg.get("url") or "").strip().rstrip("/")
        if not url:
            raise PublicationMetadataError("supabase_publication_metadata_not_configured")
        social_config = SocialConfig(url=url, publishable_key="server-side-rpc")
        return SupabasePublicationMetadataRepository(
            social_config, secret_key=secret_key, transport=cfg.get("transport"))
    raise PublicationMetadataError("unsupported_publication_metadata_provider")
