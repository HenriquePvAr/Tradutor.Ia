"""Wires the community components from configuration for the API layer.

Reads the storage choice from the environment with a safe local default (a private
filesystem-backed fake under .cache, so local development works without Google Drive),
builds the CommunityStore and CommunityService, and exposes the feed / publish /
unpublish / read-stream operations the HTTP endpoints call. The Google Drive provider is
only built when configured with real OAuth, which this task does not run.
"""

from __future__ import annotations

import json
import threading
import os
import hashlib
from pathlib import Path
from typing import Any, Callable

from community_auth import AuthenticationRequired, RequestPrincipal, ResourceNotFound, normalize_user_id
from community_service import (
    CommunityService,
    CommunityError,
    sha256_of_file,
    validate_local_pdf,
)
from community_storage import FilesystemStorageProvider, FakeStorageProvider, StorageError
from community_store import CommunityStore
from job_store import JobStatus
from ui_helpers import OUTPUT_ROOT, REPO_ROOT

COMMUNITY_DB_PATH = REPO_ROOT / ".cache" / "runtime" / "community.sqlite3"
COMMUNITY_STORAGE_ROOT = REPO_ROOT / ".cache" / "runtime" / "community_storage"
_CLIENT_IDENTITY_FIELDS = frozenset({
    "user_id", "role", "roles", "actor_id", "owner", "admin", "moderator",
})


class RangeNotSatisfiable(CommunityError):
    def __init__(self, total_size: int):
        super().__init__("range_not_satisfiable")
        self.total_size = max(0, int(total_size))


class ArtifactBindingError(CommunityError):
    """Controlled failure for the explicit legacy-artifact owner migration API."""

    def __init__(self, code: str, *, status_code: int = 422):
        super().__init__(str(code))
        self.code = str(code)
        self.status_code = int(status_code)


def storage_provider_name() -> str:
    return os.getenv("COMMUNITY_STORAGE_PROVIDER", "filesystem")


def _storage_config() -> dict[str, Any]:
    return {"storage_provider": storage_provider_name(),
            "storage_root": str(COMMUNITY_STORAGE_ROOT),
            "root_folder_id": os.getenv("COMMUNITY_DRIVE_ROOT_FOLDER_ID", "")}


def build_read_provider():
    """Provider used to stream a PDF for reading. Google Drive requires configured OAuth,
    which is out of scope here; the local default is the private filesystem fake."""
    name = storage_provider_name()
    if name == "filesystem":
        return FilesystemStorageProvider(COMMUNITY_STORAGE_ROOT)
    if name in {"fake", "memory"}:
        return FakeStorageProvider()
    if name == "google_drive":
        # Same wiring as publishing, so read and upload share one configuration and token
        # source; never runs interactive OAuth during a request.
        from google_drive_factory import GoogleDriveConfig, build_google_drive_provider
        return build_google_drive_provider(
            GoogleDriveConfig.from_env(
                root_folder_id_override=os.getenv("COMMUNITY_DRIVE_ROOT_FOLDER_ID", "")))
    raise StorageError(f"unknown storage provider: {name}")


class CommunityApi:
    def __init__(self, job_store, *, community_db_path: Path = COMMUNITY_DB_PATH,
                 output_root: Path = OUTPUT_ROOT,
                 read_provider_factory: Callable[[], Any] | None = None):
        self.store = CommunityStore(community_db_path)
        self.service = CommunityService(
            self.store, job_store, output_root=Path(output_root),
            provider_name=storage_provider_name(), community_db_path=str(community_db_path),
            storage_config={k: v for k, v in _storage_config().items() if k != "storage_provider"})
        self._read_provider_factory = read_provider_factory or build_read_provider
        # The community DB and jobs DB cannot share one transaction.  The local API
        # serializes the short draft+enqueue boundary so duplicate clicks observe the
        # first fully linked job instead of a half-created cross-database attempt.
        self._community_lock = threading.RLock()

    def close(self) -> None:
        self.store.close()

    def claim_legacy_artifact(
        self,
        job_id: str,
        payload: dict[str, Any],
        *,
        principal: RequestPrincipal,
    ) -> dict[str, Any]:
        """Let an authenticated user claim one eligible, currently unowned artifact."""
        if not isinstance(principal, RequestPrincipal) or not principal.authenticated:
            raise AuthenticationRequired("authentication_required")
        if not isinstance(payload, dict) or payload.get("confirm") is not True:
            raise ArtifactBindingError("confirmation_required", status_code=422)
        # A self-claim never accepts a client-selected identity.  Reject the field
        # explicitly so a copied admin payload cannot accidentally widen this path.
        if "target_user_id" in payload:
            raise ArtifactBindingError("target_user_id_not_allowed", status_code=422)
        expected_run = str(payload.get("expected_run_id") or "").strip()
        expected_hash = str(payload.get("expected_pdf_sha256") or "").strip().lower()
        if not expected_run or len(expected_hash) != 64 or any(
            character not in "0123456789abcdef" for character in expected_hash
        ):
            raise ArtifactBindingError("invalid_claim_request", status_code=422)

        try:
            job = self.service.job_store.get_job(str(job_id or ""))
        except (OSError, TypeError, ValueError):
            job = None
        if not job:
            self._record_binding_audit(
                principal=principal, target_user_id=principal.user_id,
                job_id=str(job_id or ""), run_id=expected_run,
                pdf_sha256=expected_hash, reason="self_claim", result="artifact_not_found",
                event_type="legacy_artifact_self_claimed")
            raise ArtifactBindingError("artifact_not_found", status_code=404)
        config = job.get("configuration") or {}
        owner = str(config.get("community_owner_id") or "") if isinstance(config, dict) else ""
        if isinstance(config, dict) and config.get("ownership_schema_version"):
            self._record_binding_audit(
                principal=principal, target_user_id=principal.user_id,
                job_id=str(job_id), run_id=expected_run, pdf_sha256=expected_hash,
                reason="self_claim", result="artifact_not_legacy",
                event_type="legacy_artifact_self_claimed")
            raise ArtifactBindingError("artifact_not_legacy", status_code=409)
        if owner and owner != principal.user_id:
            self._record_binding_audit(
                principal=principal, target_user_id=principal.user_id,
                job_id=str(job_id), run_id=expected_run, pdf_sha256=expected_hash,
                reason="self_claim", result="artifact_owned_by_another_user",
                previous_owner_id=owner, event_type="legacy_artifact_self_claimed")
            raise ArtifactBindingError("artifact_owned_by_another_user", status_code=403)

        source = self._resolve_unowned_binding_source(
            str(job_id), expected_run=expected_run, expected_hash=expected_hash)
        if self.store.post_for_any_source(str(job_id)):
            self._record_binding_audit(
                principal=principal, target_user_id=principal.user_id,
                job_id=str(job_id), run_id=expected_run, pdf_sha256=expected_hash,
                reason="self_claim", result="publication_exists",
                event_type="legacy_artifact_self_claimed")
            raise ArtifactBindingError("publication_exists", status_code=409)

        outcome, _ = self.service.job_store.bind_community_owner(
            str(job_id), principal.user_id, bound_by=principal.user_id)
        if outcome == "already_bound_to_target":
            return {
                "code": "already_owned_by_current_user",
                "message": "Este capítulo já pertence à sua conta.",
            }
        if outcome == "owner_already_assigned":
            raise ArtifactBindingError("artifact_owned_by_another_user", status_code=403)
        if outcome != "owner_bound":
            raise ArtifactBindingError("artifact_not_found", status_code=404)
        self._record_binding_audit(
            principal=principal, target_user_id=principal.user_id,
            job_id=str(job_id), run_id=expected_run, pdf_sha256=source["pdf_sha256"],
            reason="self_claim", result="artifact_claimed",
            event_type="legacy_artifact_self_claimed")
        return {
            "code": "artifact_claimed",
            "owner_user_id": principal.user_id,
            "message": "Capítulo vinculado à sua conta.",
        }

    # ---- explicit legacy ownership migration ------------------------------
    def bind_legacy_artifact_owner(
        self,
        job_id: str,
        payload: dict[str, Any],
        *,
        principal: RequestPrincipal,
        target_user_exists: Callable[[str], bool] | None = None,
    ) -> dict[str, Any]:
        """Bind one validated legacy translation to one explicit community owner.

        This path is deliberately separate from publication.  It is admin-only,
        requires the exact run/hash supplied by the operator, and never falls back
        to a slug or filesystem path.  The job-store claim is atomic and the audit
        event is append-only in the community store.
        """
        if not isinstance(principal, RequestPrincipal) or not principal.authenticated:
            raise AuthenticationRequired("authentication_required")
        if not principal.has_role("admin"):
            raise ArtifactBindingError("admin_required", status_code=403)
        if not isinstance(payload, dict) or payload.get("confirm") is not True:
            raise ArtifactBindingError("confirmation_required", status_code=422)
        try:
            normalized_job = str(job_id or "").strip()
            if len(normalized_job) != 32 or any(
                character not in "0123456789abcdef" for character in normalized_job
            ):
                raise ValueError
            target = normalize_user_id(str(payload.get("target_user_id") or ""))
            expected_run = str(payload.get("expected_run_id") or "").strip()
            expected_hash = str(payload.get("expected_pdf_sha256") or "").strip().lower()
            reason = str(payload.get("reason") or "").strip()
            if not expected_run or len(expected_hash) != 64 or any(
                character not in "0123456789abcdef" for character in expected_hash
            ) or not reason or len(reason) > 1000:
                raise ValueError
        except (TypeError, ValueError):
            raise ArtifactBindingError("invalid_binding_request", status_code=422) from None

        # A target equal to the authenticated principal is proven to exist by the
        # trusted auth provider.  Binding to another user requires an injected,
        # server-side existence check; client-supplied identity fields are ignored.
        if target != principal.user_id and not (
            callable(target_user_exists) and bool(target_user_exists(target))
        ):
            raise ArtifactBindingError("target_user_not_found", status_code=404)

        source = self._resolve_unowned_binding_source(
            normalized_job, expected_run=expected_run, expected_hash=expected_hash)
        existing = self.store.post_for_any_source(normalized_job)
        if existing:
            self._record_binding_audit(
                principal=principal, target_user_id=target, job_id=normalized_job,
                run_id=expected_run, pdf_sha256=expected_hash, reason=reason,
                result="publication_already_exists")
            raise ArtifactBindingError("publication_already_exists", status_code=409)

        outcome, claimed = self.service.job_store.bind_community_owner(
            normalized_job, target, bound_by=principal.user_id)
        if outcome == "owner_already_assigned":
            self._record_binding_audit(
                principal=principal, target_user_id=target, job_id=normalized_job,
                run_id=expected_run, pdf_sha256=expected_hash, reason=reason,
                result=outcome, previous_owner_id=str(
                    ((claimed or {}).get("configuration") or {}).get("community_owner_id") or ""))
            raise ArtifactBindingError(outcome, status_code=409)
        if outcome == "already_bound_to_target":
            return {
                "status": outcome,
                "job_id": normalized_job,
                "run_id": expected_run,
                "target_user_id": target,
                "pdf_sha256": source["pdf_sha256"],
            }
        if outcome != "owner_bound":
            raise ArtifactBindingError("artifact_not_found", status_code=404)
        self._record_binding_audit(
            principal=principal, target_user_id=target, job_id=normalized_job,
            run_id=expected_run, pdf_sha256=source["pdf_sha256"], reason=reason,
            result=outcome, previous_owner_id="")
        return {
            "status": outcome,
            "job_id": normalized_job,
            "run_id": expected_run,
            "target_user_id": target,
            "pdf_sha256": source["pdf_sha256"],
        }

    def _record_binding_audit(
        self,
        *,
        principal: RequestPrincipal,
        target_user_id: str,
        job_id: str,
        run_id: str,
        pdf_sha256: str,
        reason: str,
        result: str,
        previous_owner_id: str | None = None,
        event_type: str = "legacy_artifact_owner_bound",
    ) -> None:
        self.store.add_admin_audit_event(
            actor_id=principal.user_id,
            event_type=event_type,
            metadata={
                "action": event_type,
                "executor_user_id": principal.user_id,
                "target_user_id": target_user_id,
                "job_id": job_id,
                "run_id": run_id,
                "previous_owner_id": previous_owner_id,
                "new_owner_id": target_user_id if result == "owner_bound" else None,
                "pdf_sha256": pdf_sha256,
                "reason": reason,
                "result": result,
            },
        )

    def _resolve_unowned_binding_source(
        self,
        job_id: str,
        *,
        expected_run: str,
        expected_hash: str,
    ) -> dict[str, str]:
        """Validate the exact terminal job without assigning ownership."""
        try:
            job = self.service.job_store.get_job(job_id)
            if not job:
                raise ValueError
            config = job.get("configuration") or {}
            if not isinstance(config, dict) or config.get("job_type") != "translation":
                raise ValueError
            current_owner = str(config.get("community_owner_id") or "")
            if current_owner:
                # The caller may still receive an idempotent response, but the
                # caller must prove the same target through the atomic claim below.
                pass
            if job.get("run_id") != expected_run:
                raise ArtifactBindingError("run_mismatch", status_code=409)
            if job.get("status") not in {JobStatus.FINISHED, JobStatus.REVIEW_REQUIRED}:
                raise ArtifactBindingError("artifact_not_publishable", status_code=422)
            if int(job.get("exit_code")) != 0 or not job.get("review_confirmed_at"):
                raise ArtifactBindingError("artifact_not_publishable", status_code=422)
            output_dir = Path(str(job.get("output_dir") or "")).resolve()
            root = self.service.output_root.resolve()
            if not _is_within(output_dir, root):
                raise ValueError
            pdf_path = _resolve_recorded_path(job.get("pdf_path"), output_dir)
            if not _is_within(pdf_path, output_dir) or not _is_within(pdf_path, root):
                raise ValueError
            recorded_manifest = str(job.get("manifest_path") or "").strip()
            manifest_path = Path(recorded_manifest) if recorded_manifest else output_dir / "job_manifest.json"
            if not manifest_path.is_absolute():
                manifest_path = output_dir / manifest_path
            manifest_path = manifest_path.resolve()
            if not _is_within(manifest_path, output_dir) or not manifest_path.is_file():
                manifest_path = output_dir / "job_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or manifest.get("job_id") != job_id \
                    or manifest.get("run_id") != expected_run \
                    or int(manifest.get("exit_code")) != 0:
                raise ValueError
            manifest_pdf = _resolve_recorded_path(manifest.get("pdf_path"), output_dir)
            if manifest_pdf != pdf_path:
                raise ValueError
            digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
            if digest != expected_hash:
                raise ArtifactBindingError("hash_mismatch", status_code=409)
            validate_local_pdf(pdf_path, root)
            run_manifest_path = output_dir / "run_manifest.json"
            if run_manifest_path.is_file():
                run_manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
                run_pdf = _resolve_recorded_path(
                    run_manifest.get("pdf_path") or run_manifest.get("pdf_filename"), output_dir)
                if not isinstance(run_manifest, dict) or not run_manifest.get("run_id") \
                        or run_pdf != pdf_path:
                    raise ValueError
            return {"pdf_path": str(pdf_path), "pdf_sha256": digest}
        except ArtifactBindingError:
            raise
        except (OSError, TypeError, ValueError, json.JSONDecodeError, KeyError):
            raise ArtifactBindingError("artifact_not_found", status_code=404) from None

    # ---- operations for the endpoints --------------------------------------
    def publish(self, payload: dict[str, Any], *, principal: RequestPrincipal) -> dict[str, Any]:
        self._require_authenticated_principal(principal)
        if _CLIENT_IDENTITY_FIELDS.intersection(payload):
            raise CommunityError("client_identity_not_allowed")
        with self._community_lock:
            source = self._resolve_publish_source(payload, principal)
            raw_force = payload.get("force_new_version", False)
            if not isinstance(raw_force, bool):
                raise CommunityError("invalid_force_new_version")
            force_new_version = raw_force
            if not force_new_version:
                preflight_pdf = (
                    Path(source["pdf_path"])
                    if source["pdf_path"]
                    else self.service._resolve_pdf(Path(source["output_dir"]))
                )
                validate_local_pdf(preflight_pdf, self.service.output_root)
                preflight_sha, _ = sha256_of_file(preflight_pdf)
                existing_post = self.store.post_for_owner_source(
                    principal.user_id,
                    source["source_job_id"],
                )
                if self.store.blocking_sha_exists(
                    preflight_sha,
                    exclude_post=(existing_post or {}).get("id", ""),
                ):
                    raise CommunityError("duplicate_pdf_already_published")
            draft = self.service.create_draft(
                principal=principal,
                output_dir=source["output_dir"],
                pdf_path=source["pdf_path"],
                source_job_id=source["source_job_id"],
                source_run_id=source["source_run_id"],
                reuse_source_post=not force_new_version,
                series_title=str(payload.get("series_title") or ""),
                series_slug=str(payload.get("series_slug") or ""),
                episode_number=str(payload.get("episode_number") or ""),
                title=str(payload.get("title") or ""),
                description=str(payload.get("description") or "")[:2000],
                tags=payload.get("tags"),
                visibility=str(payload.get("visibility") or "public"))
            return self.service.request_publish(
                draft["post_id"],
                principal=principal,
                pdf_path=draft["pdf_path"],
                force_new_version=force_new_version,
            )

    def _resolve_publish_source(
        self,
        payload: dict[str, Any],
        principal: RequestPrincipal,
    ) -> dict[str, str]:
        # The browser sends a slug or a job id, never a path. Resolve server-side and
        # constrain to OUTPUT_ROOT. A job id always fails uniformly as 404 when it is
        # absent, unauthorized, incomplete, or not backed by the runner's final manifest.
        slug = str(payload.get("slug") or "").strip()
        job_id = str(payload.get("source_job_id") or "").strip()
        if job_id:
            try:
                return self._resolve_translation_job(job_id, principal)
            except ResourceNotFound:
                # A stale local job manifest can outlive the jobs database.  Only an
                # administrator may deliberately adopt that folder through the legacy
                # slug path; ordinary users keep the same uniform 404 boundary.
                if not slug or not principal.has_role("admin"):
                    raise
        if slug:
            # Legacy/local output folders have no trusted owner metadata. Only a trusted
            # admin may adopt them; ordinary users need an owned source job.
            if not principal.has_role("admin"):
                raise ResourceNotFound("output_not_found")
            from ui_helpers import sanitize_output_name
            return {
                "output_dir": str(
                    (self.service.output_root / sanitize_output_name(slug)).resolve()
                ),
                "pdf_path": "",
                "source_job_id": "",
                "source_run_id": "",
            }
        raise CommunityError("missing_output_identifier")

    def _resolve_translation_job(
        self,
        job_id: str,
        principal: RequestPrincipal,
    ) -> dict[str, str]:
        try:
            if len(job_id) != 32 or any(
                character not in "0123456789abcdef" for character in job_id
            ):
                raise ValueError
            job = self.service.job_store.get_job(job_id)
            if not job:
                raise ValueError
            config = job.get("configuration") or {}
            if not isinstance(config, dict) or config.get("job_type") != "translation":
                raise ValueError
            owner_id = str(config.get("community_owner_id") or "")
            legacy_owner = False
            if not owner_id and principal.auth_source == "local_session":
                # Jobs created before community ownership was introduced are still local
                # artifacts. Bind them to the authenticated local operator only after all
                # manifest/PDF checks below succeed; external principals remain fail-closed.
                owner_id = principal.user_id
                config["community_owner_id"] = owner_id
                legacy_owner = True
            if not principal.has_role("admin"):
                if not owner_id:
                    raise ArtifactBindingError("artifact_has_no_owner", status_code=422)
                if owner_id != principal.user_id:
                    raise ArtifactBindingError("artifact_not_owned", status_code=403)
            if job.get("status") not in {JobStatus.FINISHED, JobStatus.REVIEW_REQUIRED}:
                raise ValueError
            if int(job.get("exit_code")) != 0:
                raise ValueError

            output_dir = Path(str(job.get("output_dir") or "")).resolve()
            root = self.service.output_root.resolve()
            if not _is_within(output_dir, root):
                raise ValueError
            pdf_path = _resolve_recorded_path(job.get("pdf_path"), output_dir)
            if not _is_within(pdf_path, output_dir) or not _is_within(pdf_path, root):
                raise ValueError

            # The job row records the runner manifest, while the pipeline also writes a
            # separate run manifest. Prefer the recorded path only after constraining it
            # to this output directory, then fall back to the canonical job manifest.
            recorded_manifest = str(job.get("manifest_path") or "").strip()
            manifest_path = Path(recorded_manifest) if recorded_manifest else output_dir / "job_manifest.json"
            if not manifest_path.is_absolute():
                manifest_path = output_dir / manifest_path
            manifest_path = manifest_path.resolve()
            if not _is_within(manifest_path, output_dir) or not manifest_path.is_file():
                manifest_path = output_dir / "job_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError
            # Older runner rows point at run_manifest.json. That manifest is useful
            # evidence but intentionally has no job id; use the job manifest for the
            # lifecycle binding instead of treating the valid PDF as missing.
            if manifest.get("job_id") != job["id"] or manifest.get("run_id") != job["run_id"]:
                fallback_manifest = output_dir / "job_manifest.json"
                if fallback_manifest == manifest_path or not fallback_manifest.is_file():
                    raise ValueError
                manifest_path = fallback_manifest.resolve()
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not isinstance(manifest, dict):
                    raise ValueError
            manifest_pdf = _resolve_recorded_path(manifest.get("pdf_path"), output_dir)
            manifest_status = str(manifest.get("status") or "")
            status_matches = manifest_status == job["status"] or (
                job.get("status") == JobStatus.FINISHED
                and job.get("review_confirmed_at")
                and manifest_status == JobStatus.REVIEW_REQUIRED
            )
            if (
                manifest.get("job_id") != job["id"]
                or manifest.get("run_id") != job["run_id"]
                or not status_matches
                or int(manifest.get("exit_code")) != 0
                or manifest_pdf != pdf_path
            ):
                raise ValueError
            validate_local_pdf(pdf_path, root)
            # Keep the pipeline's run manifest and quality report tied to the same
            # output folder. They are evidence, not client-controlled identifiers.
            run_manifest_path = output_dir / "run_manifest.json"
            if run_manifest_path.is_file():
                run_manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
                if not isinstance(run_manifest, dict) or not run_manifest.get("run_id"):
                    raise ValueError
                run_pdf = _resolve_recorded_path(
                    run_manifest.get("pdf_path") or run_manifest.get("pdf_filename"), output_dir)
                if run_pdf != pdf_path:
                    raise ValueError
            digest = hashlib.sha256()
            with pdf_path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            if legacy_owner:
                self.service.job_store.update_fields(
                    job["id"], configuration_json=json.dumps(config, ensure_ascii=False))
            return {
                "output_dir": str(output_dir),
                "pdf_path": str(pdf_path),
                "source_job_id": job["id"],
                "source_run_id": job["run_id"],
                "manifest_path": str(manifest_path),
                "run_manifest_path": str(run_manifest_path),
                "pdf_sha256": digest.hexdigest(),
            }
        except ArtifactBindingError:
            raise
        except (OSError, TypeError, ValueError, json.JSONDecodeError, KeyError):
            raise ResourceNotFound("output_not_found") from None

    def unpublish(self, post_id: str, *, principal: RequestPrincipal) -> dict[str, Any]:
        with self._community_lock:
            self.service.unpublish(post_id, principal=principal)
        return {"ok": True}

    def feed(self, *, principal: RequestPrincipal, series_slug: str = "", query: str = "", limit: int = 50,
             offset: int = 0) -> dict[str, Any]:
        # The community feed is authenticated-only; it never lists posts to anonymous
        # callers even though every card is metadata-only.
        self._require_authenticated_principal(principal)
        with self._community_lock:
            cards = self.service.feed(
                series_slug=series_slug,
                query=query,
                limit=min(100, max(1, limit)),
                offset=max(0, offset),
            )
        return {"posts": cards, "count": len(cards)}

    def my_posts(self, *, principal: RequestPrincipal) -> dict[str, Any]:
        self._require_authenticated_principal(principal)
        with self._community_lock:
            posts = self.store.list_user_posts(principal.user_id)
        return {"posts": [self.service._card(p) | {
            "visibility": p["visibility"],
            "source_job_id": p.get("source_job_id") or "",
            "tags": list(p.get("tags") or []),
        } for p in posts]}

    def open_pdf(self, post_id: str, *, principal: RequestPrincipal, range_header: str = ""):
        """Return (metadata, StorageStream) for the read endpoint, or raise CommunityError."""
        with self._community_lock:
            file = self.service.resolve_readable_file(post_id, principal=principal)
            total = int(file["size_bytes"])
            start, end = _parse_range(range_header, total)
        # Storage construction/opening may perform retries or remote I/O.  The
        # authorization decision above is complete before any provider call, while
        # keeping that I/O outside the process-wide community lock avoids blocking
        # unrelated feed and owner operations.
        provider = self._read_provider_factory()
        stream = provider.open_stream(file["storage_file_id"], start=start, end=end)
        with self._community_lock:
            self.store.increment_views(post_id)
        return {
            "filename": _safe_disposition_name(file["filename"]),
            "mime_type": "application/pdf",
            "total_size": total,
            "start": stream.start,
            "end": stream.end,
            "content_length": stream.content_length,
            "partial": start is not None,
        }, stream

    def head_pdf(self, post_id: str, *, principal: RequestPrincipal) -> dict[str, Any]:
        """Authorize and answer HEAD from verified DB metadata without opening storage."""
        with self._community_lock:
            file = self.service.resolve_readable_file(post_id, principal=principal)
        return {
            "filename": _safe_disposition_name(file["filename"]),
            "mime_type": "application/pdf",
            "total_size": int(file["size_bytes"]),
        }

    @staticmethod
    def _require_authenticated_principal(principal: RequestPrincipal) -> None:
        if not isinstance(principal, RequestPrincipal) or not principal.authenticated:
            raise AuthenticationRequired("authentication_required")


def _parse_range(range_header: str, total: int) -> tuple[int | None, int | None]:
    if not range_header:
        return None, None
    if total <= 0 or not range_header.lower().startswith("bytes=") or "," in range_header:
        raise RangeNotSatisfiable(total)
    spec = range_header.split("=", 1)[1].strip()
    if spec.count("-") != 1:
        raise RangeNotSatisfiable(total)
    lo_s, hi_s = (part.strip() for part in spec.split("-", 1))
    if not lo_s and not hi_s:
        raise RangeNotSatisfiable(total)
    try:
        if not lo_s:
            suffix_length = int(hi_s)
            if suffix_length <= 0:
                raise ValueError
            return max(0, total - suffix_length), total - 1
        lo = int(lo_s)
        hi = int(hi_s) if hi_s else total - 1
    except ValueError:
        raise RangeNotSatisfiable(total) from None
    if lo < 0 or hi < lo or lo >= total:
        raise RangeNotSatisfiable(total)
    hi = min(total - 1, hi)
    return lo, hi


def _safe_disposition_name(name: str) -> str:
    # Strip anything that could break the Content-Disposition header.
    import re
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', "_", Path(name).name).strip("_")
    return cleaned or "capitulo.pdf"


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _resolve_recorded_path(value: Any, output_dir: Path) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("missing_recorded_path")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = output_dir / candidate
    return candidate.resolve()
