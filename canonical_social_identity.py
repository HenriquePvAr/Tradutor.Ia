"""Canonical source -> social work/chapter identity materialization.

This boundary is deliberately server-owned.  Browser payloads never provide the
source provider/key pair or the resulting work/chapter ids; those values are
derived from trusted job/manifests and materialized before any publication
attempt reaches storage metadata or Drive.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import threading
import uuid
from typing import Any, Protocol

from community_auth import RequestPrincipal
from community_service import CommunityError
from community_publication_metadata import (
    PublicationMetadataRepositoryConfig,
    SecretHeaderDict,
)
from supabase_social import SocialConfig


class CanonicalSocialIdentityError(CommunityError):
    """Safe canonical social identity failure."""


@dataclass(frozen=True)
class CanonicalSourceKeys:
    source_provider: str
    source_work_key: str
    source_chapter_key: str
    source_identity_hash: str
    identity_version: int
    work_title: str
    work_slug: str
    chapter_number: float
    chapter_title: str
    source_job_id: str = ""
    source_run_id: str = ""
    reconstruction_job_id: str = ""
    reconstruction_run_id: str = ""


@dataclass(frozen=True)
class CanonicalSocialIdentity:
    work_id: str
    chapter_id: str
    work_created: bool
    chapter_created: bool


class CanonicalSocialIdentityMaterializer(Protocol):
    def resolve_or_materialize(
        self,
        principal: RequestPrincipal,
        identity: CanonicalSourceKeys,
    ) -> CanonicalSocialIdentity: ...


def _stable_source_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _source_identity_from_job(job: dict[str, Any], job_store: Any) -> CanonicalSourceKeys:
    config = job.get("configuration") if isinstance(job.get("configuration"), dict) else {}
    source_job = job
    reconstruction_job_id = ""
    reconstruction_run_id = ""
    if config.get("job_type") == "artifact_reconstruction":
        parent_id = str(job.get("parent_job_id") or config.get("source_job_id") or "")
        parent = job_store.get_job(parent_id) if parent_id else None
        if not parent:
            raise CanonicalSocialIdentityError("canonical_source_identity_unavailable")
        source_job = parent
        reconstruction_job_id = str(job.get("id") or "")
        reconstruction_run_id = str(job.get("run_id") or "")
    source_config = (
        source_job.get("configuration")
        if isinstance(source_job.get("configuration"), dict)
        else {}
    )
    canonical = (
        source_config.get("canonical_identity")
        if isinstance(source_config.get("canonical_identity"), dict)
        else None
    )
    if canonical is None:
        analysis = source_config.get("source_analysis")
        if isinstance(analysis, dict):
            canonical = analysis.get("canonical_identity")
    if not isinstance(canonical, dict):
        analysis = source_job.get("source_analysis")
        if isinstance(analysis, dict):
            canonical = analysis.get("canonical_identity")
    if not isinstance(canonical, dict):
        raise CanonicalSocialIdentityError("canonical_source_identity_unavailable")

    provider = str(canonical.get("adapter_name") or canonical.get("provider") or "").strip().lower()
    series_id = str(
        canonical.get("series_identifier")
        or canonical.get("series_id")
        or ""
    ).strip()
    episode_id = str(
        canonical.get("episode_identifier")
        or canonical.get("episode_id")
        or ""
    ).strip()
    if provider != "webtoons" or not series_id.isdigit() or not episode_id.isdigit():
        raise CanonicalSocialIdentityError("canonical_source_identity_unavailable")
    series_slug = str(canonical.get("series_slug") or "").strip().lower()
    episode_label = str(
        canonical.get("episode_label") or canonical.get("episode_slug") or f"episode-{episode_id}"
    ).strip()
    identity_version = int(canonical.get("schema_version") or 1)
    work_key = f"title_no={series_id}"
    chapter_key = f"title_no={series_id}:episode_no={episode_id}"
    identity_payload = {
        "identity_version": identity_version,
        "source_provider": provider,
        "source_work_key": work_key,
        "source_chapter_key": chapter_key,
    }
    expected_hash = _stable_source_hash(identity_payload)
    observed_hash = str(canonical.get("identity_hash") or "").strip().lower()
    source_hash = observed_hash if re.fullmatch(r"[0-9a-f]{64}", observed_hash) else expected_hash
    work_slug = _presentation_slug(provider, series_id, series_slug)
    work_title = _presentation_title(source_job, series_slug, series_id)
    return CanonicalSourceKeys(
        source_provider=provider,
        source_work_key=work_key,
        source_chapter_key=chapter_key,
        source_identity_hash=source_hash,
        identity_version=identity_version,
        work_title=work_title,
        work_slug=work_slug,
        chapter_number=float(episode_id),
        chapter_title=episode_label,
        source_job_id=str(source_job.get("id") or ""),
        source_run_id=str(source_job.get("run_id") or ""),
        reconstruction_job_id=reconstruction_job_id,
        reconstruction_run_id=reconstruction_run_id,
    )


def canonical_source_identity_from_publish_source(
    source: dict[str, str],
    job_store: Any,
) -> CanonicalSourceKeys:
    job_id = str(source.get("source_job_id") or "")
    if not job_id:
        raise CanonicalSocialIdentityError("canonical_source_identity_unavailable")
    job = job_store.get_job(job_id)
    if not job:
        raise CanonicalSocialIdentityError("canonical_source_identity_unavailable")
    return _source_identity_from_job(job, job_store)


def _presentation_slug(provider: str, series_id: str, series_slug: str) -> str:
    base = re.sub(r"[^a-z0-9-]+", "-", series_slug.strip().lower()).strip("-")
    if not base:
        base = f"{provider}-work"
    suffix = hashlib.sha256(f"{provider}:{series_id}".encode("utf-8")).hexdigest()[:12]
    slug = f"{base}-{suffix}"
    return slug[:120].strip("-") or f"{provider}-{suffix}"


def _presentation_title(job: dict[str, Any], series_slug: str, series_id: str) -> str:
    config = job.get("configuration") if isinstance(job.get("configuration"), dict) else {}
    raw = str(job.get("series_title") or config.get("chapter_name") or series_slug or series_id)
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw:
        raw = f"Source work {series_id}"
    return raw[:200]


class NullCanonicalSocialIdentityMaterializer:
    provider = "none"

    def resolve_or_materialize(
        self,
        principal: RequestPrincipal,
        identity: CanonicalSourceKeys,
    ) -> CanonicalSocialIdentity:
        raise CanonicalSocialIdentityError("canonical_social_identity_backend_unavailable")


class FakeCanonicalSocialIdentityMaterializer:
    """Thread-safe hermetic materializer mirroring the DB identity contract."""

    provider = "fake"

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self._lock = threading.Lock()
        self.work_mappings: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.chapter_mappings: dict[tuple[tuple[str, str, str], str], dict[str, Any]] = {}
        self.works: dict[str, dict[str, Any]] = {}
        self.chapters: dict[str, dict[str, Any]] = {}

    def resolve_or_materialize(
        self,
        principal: RequestPrincipal,
        identity: CanonicalSourceKeys,
    ) -> CanonicalSocialIdentity:
        if self.fail:
            raise CanonicalSocialIdentityError("canonical_social_identity_backend_unavailable")
        owner = str(principal.user_id or "")
        work_key = (owner, identity.source_provider, identity.source_work_key)
        chapter_key = (work_key, identity.source_chapter_key)
        with self._lock:
            work = self.work_mappings.get(work_key)
            work_created = False
            if work is None:
                work_id = str(uuid.uuid4())
                work = {
                    "work_id": work_id,
                    "source_identity_hash": identity.source_identity_hash,
                    "identity_version": identity.identity_version,
                }
                self.work_mappings[work_key] = work
                self.works[work_id] = {"owner_id": owner, "slug": identity.work_slug}
                work_created = True
            elif int(work["identity_version"]) != int(identity.identity_version):
                raise CanonicalSocialIdentityError("canonical_social_identity_conflict")
            if self.works.get(work["work_id"], {}).get("owner_id") != owner:
                raise CanonicalSocialIdentityError("canonical_social_identity_conflict")
            chapter = self.chapter_mappings.get(chapter_key)
            chapter_created = False
            if chapter is None:
                chapter_id = str(uuid.uuid4())
                chapter = {
                    "chapter_id": chapter_id,
                    "source_identity_hash": identity.source_identity_hash,
                    "identity_version": identity.identity_version,
                }
                self.chapter_mappings[chapter_key] = chapter
                self.chapters[chapter_id] = {
                    "work_id": work["work_id"],
                    "chapter_number": identity.chapter_number,
                }
                chapter_created = True
            elif (
                chapter["source_identity_hash"] != identity.source_identity_hash
                or int(chapter["identity_version"]) != int(identity.identity_version)
            ):
                raise CanonicalSocialIdentityError("canonical_social_identity_conflict")
            if self.chapters.get(chapter["chapter_id"], {}).get("work_id") != work["work_id"]:
                raise CanonicalSocialIdentityError("canonical_social_identity_conflict")
            return CanonicalSocialIdentity(
                work_id=work["work_id"],
                chapter_id=chapter["chapter_id"],
                work_created=work_created,
                chapter_created=chapter_created,
            )


RPC_RESOLVE_OR_MATERIALIZE = "resolve_or_materialize_community_source_identity"


class SupabaseCanonicalSocialIdentityMaterializer:
    """Backend-only materializer over a narrow Supabase RPC."""

    provider = "supabase"

    def __init__(self, config: SocialConfig, *, secret_key: str, transport=None):
        secret = str(secret_key or "").strip()
        if not secret or not secret.startswith("sb_secret_"):
            raise CanonicalSocialIdentityError("canonical_social_identity_backend_unavailable")
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

    def resolve_or_materialize(
        self,
        principal: RequestPrincipal,
        identity: CanonicalSourceKeys,
    ) -> CanonicalSocialIdentity:
        payload = {
            "p_owner_id": str(principal.user_id or ""),
            "p_source_provider": identity.source_provider,
            "p_source_work_key": identity.source_work_key,
            "p_source_chapter_key": identity.source_chapter_key,
            "p_source_identity_hash": identity.source_identity_hash,
            "p_source_identity_version": identity.identity_version,
            "p_work_title": identity.work_title,
            "p_work_slug": identity.work_slug,
            "p_chapter_number": identity.chapter_number,
            "p_chapter_title": identity.chapter_title,
            "p_first_source_job_id": identity.source_job_id,
            "p_first_source_run_id": identity.source_run_id,
            "p_first_reconstruction_job_id": identity.reconstruction_job_id,
            "p_first_reconstruction_run_id": identity.reconstruction_run_id,
        }
        url = f"{self._config.rest_url}/rpc/{RPC_RESOLVE_OR_MATERIALIZE}"
        try:
            resp = self._transport_client().request(
                "POST",
                url,
                headers=self._headers(),
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )
        except Exception as exc:
            raise CanonicalSocialIdentityError(
                "canonical_social_identity_backend_unavailable"
            ) from exc
        parsed: Any = None
        raw = resp.content or b""
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                parsed = None
        if resp.status >= 400:
            if resp.status in {401, 403}:
                code = "canonical_social_identity_backend_not_authorized"
            elif resp.status == 409:
                code = "canonical_social_identity_conflict"
            elif resp.status in {400, 422}:
                code = "canonical_social_identity_invariant_failed"
            elif resp.status >= 500:
                code = "canonical_social_identity_backend_unavailable"
            else:
                code = "canonical_social_identity_backend_error"
            raise CanonicalSocialIdentityError(code)
        row = parsed[0] if isinstance(parsed, list) and parsed else parsed
        if not isinstance(row, dict):
            raise CanonicalSocialIdentityError("canonical_social_identity_malformed_response")
        work_id = str(row.get("work_id") or "")
        chapter_id = str(row.get("chapter_id") or "")
        if not work_id or not chapter_id:
            raise CanonicalSocialIdentityError("canonical_social_identity_malformed_response")
        return CanonicalSocialIdentity(
            work_id=work_id,
            chapter_id=chapter_id,
            work_created=bool(row.get("work_created")),
            chapter_created=bool(row.get("chapter_created")),
        )


def build_canonical_social_identity_materializer(
    config: dict[str, Any] | None,
) -> CanonicalSocialIdentityMaterializer:
    provider = str((config or {}).get("provider") or "none").strip().lower()
    if provider in {"", "none", "null", "offline"}:
        return NullCanonicalSocialIdentityMaterializer()
    if provider == "fake":
        return FakeCanonicalSocialIdentityMaterializer()
    if provider == "supabase":
        cfg = PublicationMetadataRepositoryConfig(config or {})
        secret_key = str(cfg.get("secret_key") or cfg.get("backend_secret_key") or "").strip()
        secret_env_var = str(cfg.get("secret_env_var") or "").strip()
        if not secret_key and secret_env_var:
            import os

            secret_key = str(os.environ.get(secret_env_var) or "").strip()
        url = str(cfg.get("url") or "").strip().rstrip("/")
        if not url or not secret_key.startswith("sb_secret_"):
            raise CanonicalSocialIdentityError("canonical_social_identity_backend_unavailable")
        social_config = SocialConfig(url=url, publishable_key="server-side-rpc")
        return SupabaseCanonicalSocialIdentityMaterializer(
            social_config, secret_key=secret_key, transport=cfg.get("transport")
        )
    raise CanonicalSocialIdentityError("canonical_social_identity_backend_unavailable")
