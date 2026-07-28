"""Community orchestration: validate a local PDF, publish it, read it, unpublish it.

The browser never supplies a file path - it sends identifiers, and this layer resolves
the PDF server-side from the authorized output directory and validates it before anything
is uploaded. Publishing creates a queued ``community_publish`` job that the worker runs;
the post only becomes visible after the upload is verified. Reading streams the PDF from
the storage provider without exposing the provider's file id or any credential.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from community_auth import (
    AuthenticationRequired,
    AuthorizationDenied,
    RequestPrincipal,
    ResourceNotFound,
)
from community_authorization import authorize_delete_file, authorize_manage_post, authorize_read_post
from community_store import CommunityStore, FileStatus, PostStatus, Visibility, normalize_tags
from job_store import JobStatus
import process_tree

PDF_MAGIC = b"%PDF"
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


class CommunityError(ValueError):
    """A community request was rejected (validation, authorization, duplication)."""


def sha256_of_file(path: Path, *, chunk: int = 1024 * 1024) -> tuple[str, int]:
    """Stream a file to compute its SHA-256 and size without loading it into memory."""
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
            total += len(block)
    return digest.hexdigest(), total


def safe_pdf_filename(series_slug: str, episode_number: str, fallback: str) -> str:
    base = f"{series_slug}_capitulo_{episode_number}" if series_slug and episode_number else Path(fallback).stem
    cleaned = _SAFE_FILENAME.sub("_", base).strip("_") or "capitulo"
    return f"{cleaned}.pdf"


def validate_local_pdf(pdf_path: Path, output_root: Path) -> None:
    """Validate a PDF is real and lives inside the authorized output root.

    Fail-closed against path traversal: the resolved path must be under ``output_root``.
    The file must exist, be a regular .pdf, start with the PDF magic bytes, and be
    non-empty. The browser never chooses this path; it is resolved server-side.
    """
    resolved = pdf_path.resolve()
    root = output_root.resolve()
    if root != resolved and root not in resolved.parents:
        raise CommunityError("pdf_outside_output_root")
    if not resolved.is_file():
        raise CommunityError("pdf_not_found")
    if resolved.suffix.lower() != ".pdf":
        raise CommunityError("not_a_pdf")
    with resolved.open("rb") as handle:
        header = handle.read(len(PDF_MAGIC))
    if header != PDF_MAGIC:
        raise CommunityError("bad_pdf_magic")
    if resolved.stat().st_size <= 0:
        raise CommunityError("empty_pdf")


class CommunityService:
    def __init__(self, store: CommunityStore, job_store, *, output_root: Path,
                 provider_name: str = "fake", community_db_path: str = "",
                 storage_config: dict | None = None,
                 build_command: Callable[[dict], list[str]] | None = None):
        self.store = store
        self.job_store = job_store
        self.output_root = Path(output_root)
        self.provider_name = provider_name
        self.community_db_path = community_db_path
        self.storage_config = storage_config or {}
        # How to turn a publish job into a runnable command; injected so tests can point
        # it at the fake community-publish runner. Defaults to a marker the worker
        # recognises to run the community publish runner by job_type.
        self._build_command = build_command or (lambda payload: ["community_publish"])

    # ---- draft + publish ----------------------------------------------------
    def create_draft(self, *, principal: RequestPrincipal, output_dir: str,
                     pdf_path: str = "",
                     source_job_id: str = "", source_run_id: str = "",
                     reuse_source_post: bool = True,
                     series_title: str = "", series_slug: str = "", episode_number: str = "",
                     title: str = "", description: str = "", visibility: str = Visibility.PUBLIC,
                     tags: Any = None,
                     ) -> dict[str, Any]:
        self._require_authenticated(principal)
        if visibility not in Visibility.ALL:
            raise CommunityError("invalid_visibility")
        out = Path(output_dir).resolve()
        root = self.output_root.resolve()
        if root != out and root not in out.parents:
            raise CommunityError("output_outside_root")
        if pdf_path:
            pdf = Path(pdf_path).resolve()
            if out not in pdf.parents:
                raise CommunityError("pdf_outside_output_dir")
        else:
            pdf = self._resolve_pdf(out)
        validate_local_pdf(pdf, self.output_root)
        try:
            normalized_tags = normalize_tags(tags)
        except ValueError as exc:
            raise CommunityError(str(exc)) from exc
        post_fields = {
            "user_id": principal.user_id,
            "source_job_id": source_job_id,
            "source_run_id": source_run_id,
            "series_title": series_title,
            "series_slug": series_slug,
            "episode_number": episode_number,
            "output_dir": str(out),
            "title": title or series_title,
            "description": description,
            "visibility": visibility,
            "tags": normalized_tags,
        }
        if source_job_id and reuse_source_post:
            post_id, created = self.store.create_or_get_source_post(**post_fields)
            if not created:
                existing = self.store.get_post(post_id) or {}
                comparable = (
                    "source_run_id",
                    "series_title",
                    "series_slug",
                    "episode_number",
                    "output_dir",
                    "title",
                    "description",
                    "visibility",
                    "tags",
                )
                if any(
                    str(existing.get(key) or "") != str(post_fields.get(key) or "")
                    for key in comparable
                ):
                    raise CommunityError("source_publish_conflict")
        else:
            post_id = self.store.create_post(**post_fields)
        return {"post_id": post_id, "pdf_path": str(pdf)}

    def _resolve_pdf(self, output_dir: Path) -> Path:
        manifest = output_dir / "run_manifest.json"
        if manifest.is_file():
            import json
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                name = data.get("pdf_filename") or Path(data.get("pdf_path") or "").name
                if name and (output_dir / name).is_file():
                    return output_dir / name
            except (ValueError, OSError):
                pass
        pdfs = sorted(output_dir.glob("*.pdf"))
        if not pdfs:
            raise CommunityError("pdf_not_found")
        return pdfs[0]

    def request_publish(self, post_id: str, *, principal: RequestPrincipal,
                        pdf_path: str = "",
                        force_new_version: bool = False) -> dict[str, Any]:
        self._require_authenticated(principal)
        post = self.store.get_post(post_id)
        if not post:
            raise ResourceNotFound("post_not_found")
        authorize_manage_post(principal, post)
        if post["status"] in {PostStatus.BLOCKED, PostStatus.DELETED}:
            raise AuthorizationDenied("post_not_publishable")
        actor = principal.user_id

        output_dir = self._post_output_dir(post)
        if pdf_path:
            pdf = Path(pdf_path).resolve()
            if output_dir not in pdf.parents:
                raise CommunityError("pdf_outside_output_dir")
        elif post.get("source_job_id"):
            # Source jobs must use the exact runner-recorded artifact supplied by the
            # authenticated API resolver; never fall back to a directory glob.
            raise ResourceNotFound("output_not_found")
        else:
            pdf = self._resolve_pdf(output_dir)
        validate_local_pdf(pdf, self.output_root)
        sha256, size = sha256_of_file(pdf)

        # Refresh after hashing: a runner or unpublish can commit while the local file
        # is read.  Decisions below must use the current post/file pair, not the stale
        # row loaded at method entry.
        post = self.store.get_post(post_id)
        if not post:
            raise ResourceNotFound("post_not_found")
        authorize_manage_post(principal, post)
        if post["status"] in {PostStatus.BLOCKED, PostStatus.DELETED}:
            raise AuthorizationDenied("post_not_publishable")
        filename = safe_pdf_filename(post["series_slug"] or "", post["episode_number"] or "", pdf.name)
        preparation = self.store.prepare_publish_attempt(
            post_id=post_id,
            sha256=sha256,
            actor_id=actor,
            allow_duplicate=force_new_version,
        )
        outcome = preparation.get("outcome")
        if outcome in {"active", "completed"}:
            return {
                "post_id": post_id,
                "file_id": preparation["file_id"],
                "job_id": preparation["job_id"],
            }
        if outcome != "needs_job":
            errors = {
                "duplicate": "duplicate_pdf_already_published",
                "not_publishable": "post_not_publishable",
                "unavailable": "pdf_version_unavailable",
                "conflict": "publish_state_conflict",
                "missing": "post_not_found",
            }
            raise CommunityError(errors.get(str(outcome), "publish_state_conflict"))

        file_id = preparation.get("file_id") or uuid.uuid4().hex
        payload = {
            "post_id": post_id, "file_id": file_id, "user_id": actor,
            "local_pdf_path": str(pdf), "pdf_filename": filename, "pdf_size": size,
            "pdf_sha256": sha256, "series_slug": post["series_slug"] or "series",
            "series_title": post["series_title"] or "", "episode_number": post["episode_number"] or "",
            "storage_provider": self.provider_name, "visibility": post["visibility"],
        }
        command = self._build_command(payload)
        # Create the job first in a non-claimable state.  Only the atomic community
        # reservation below may make it QUEUED; a hard crash at either side is then
        # recoverable without a phantom upload_job_id or a second runner.
        job_id = uuid.uuid4().hex
        process = process_tree.snapshot(os.getpid()) or {}
        self.job_store.create_job(
            job_id=job_id,
            source_url="", output_dir=str(output_dir), command=command,
            configuration={
                "job_type": "community_publish",
                "community_db": self.community_db_path,
                "storage": {
                    "storage_provider": self.provider_name,
                    **self.storage_config,
                    "owner_id": actor,
                },
                **payload,
            },
            series_title=post["series_title"] or "", series_slug=post["series_slug"] or "",
            episode_number=post["episode_number"] or "",
            initial_status=JobStatus.STAGING,
            staging_owner_pid=os.getpid(),
            staging_owner_create_time=process.get("create_time"),
        )
        try:
            reservation = self.store.activate_publish_attempt(
                post_id=post_id,
                file_id=file_id,
                upload_job_id=job_id,
                filename=filename,
                mime_type="application/pdf",
                size_bytes=size,
                sha256=sha256,
                storage_provider=self.provider_name,
                actor_id=actor,
                allow_duplicate=force_new_version,
            )
        except BaseException:
            # The community transaction may have committed before the connection
            # reported an error.  Keep the job non-claimable and release the API
            # process lease so the worker can classify the authoritative community
            # state instead of guessing FAILED from an ambiguous cross-DB outcome.
            try:
                self.job_store.update_fields(
                    job_id, worker_pid=None, worker_create_time=None)
            except Exception:
                pass
            raise

        outcome = reservation.get("outcome")
        if outcome != "reserved":
            try:
                self.job_store.transition(job_id, JobStatus.CANCELLED)
            except Exception:
                pass
            if outcome in {"active", "completed"}:
                return {
                    "post_id": post_id,
                    "file_id": reservation["file_id"],
                    "job_id": reservation["job_id"],
                }
            errors = {
                "duplicate": "duplicate_pdf_already_published",
                "not_publishable": "post_not_publishable",
                "unavailable": "pdf_version_unavailable",
                "conflict": "publish_state_conflict",
                "missing": "post_not_found",
            }
            raise CommunityError(errors.get(str(outcome), "publish_state_conflict"))

        try:
            self.job_store.transition(
                job_id,
                JobStatus.QUEUED,
                queued_at=time.time(),
                worker_pid=None,
                worker_create_time=None,
            )
        except BaseException:
            # The linked STAGING job is safe for worker recovery even while this API
            # process remains alive; clear its staging-owner lease before propagating.
            try:
                self.job_store.update_fields(
                    job_id, worker_pid=None, worker_create_time=None)
            except Exception:
                pass
            raise
        return {
            "post_id": post_id,
            "file_id": reservation["file_id"],
            "job_id": reservation["job_id"],
        }

    def _post_output_dir(self, post: dict[str, Any]) -> Path:
        candidate = post.get("output_dir") or ""
        if not candidate and post.get("source_job_id"):
            job = self.job_store.get_job(post["source_job_id"])
            candidate = (job or {}).get("output_dir") or ""
        if not candidate:
            raise CommunityError("output_not_found")
        out = Path(candidate).resolve()
        root = self.output_root.resolve()
        if root != out and root not in out.parents:
            raise CommunityError("output_outside_root")
        return out

    # ---- feed + read --------------------------------------------------------
    def feed(self, **kwargs: Any) -> list[dict[str, Any]]:
        posts = self.store.feed(require_moderation=False, require_verified_file=True, **kwargs)
        return [self._card(post) for post in posts]

    @staticmethod
    def _card(post: dict[str, Any]) -> dict[str, Any]:
        # A feed card exposes only metadata; never the storage file id.
        author_id = str(post.get("user_id") or "")
        return {
            "publication_id": post["id"],
            "post_id": post["id"],
            "series_title": post["series_title"],
            "series_slug": post["series_slug"],
            "episode_number": post["episode_number"],
            "title": post["title"],
            "description": post["description"],
            "tags": list(post.get("tags") or []),
            "cover_reference": post["cover_reference"],
            "user_id": post["user_id"],
            "author": {
                "user_id": author_id,
                "display_name": str(post.get("author_display_name") or "Usuário"),
                "avatar_url": f"/api/community/profiles/{author_id}/avatar" if post.get("author_avatar_object_key") else "",
                "public_role": str(post.get("author_public_role") or ""),
            },
            "views": post["views"],
            "published_at": post["published_at"],
            "status": post["status"],
        }

    def resolve_readable_file(self, post_id: str, *, principal: RequestPrincipal) -> dict[str, Any]:
        """Authorize from DB metadata before returning an internal storage file record."""
        if not isinstance(principal, RequestPrincipal):
            raise AuthenticationRequired("authentication_required")
        post = self.store.get_post(post_id)
        if not post:
            raise ResourceNotFound("post_not_found")
        authorize_read_post(principal, post)
        file = self.store.file_for_post(post_id)
        if not file or file["upload_status"] != FileStatus.VERIFIED or not file["storage_file_id"]:
            raise ResourceNotFound("post_not_found")
        return file

    # ---- lifecycle ----------------------------------------------------------
    def unpublish(self, post_id: str, *, principal: RequestPrincipal) -> None:
        post = self._managed_post(post_id, principal)
        if post["status"] in {PostStatus.BLOCKED, PostStatus.DELETED}:
            raise ResourceNotFound("post_not_found")
        self.store.set_post_status(post_id, PostStatus.UNPUBLISHED, actor_id=principal.user_id)
        file = self.store.file_for_post(post_id)
        if (
            file
            and file.get("upload_job_id")
            and file.get("upload_status") in {
                FileStatus.PENDING,
                FileStatus.UPLOADING,
                FileStatus.VERIFYING,
            }
        ):
            self.store.invalidate_publish_file(file["id"], file["upload_job_id"])
            self.job_store.request_cancel(file["upload_job_id"])
            self.store.add_event(
                post_id,
                principal.user_id,
                "publish_cancel_requested",
                {"file_id": file["id"], "job_id": file["upload_job_id"]},
            )

    def _managed_post(self, post_id: str, principal: RequestPrincipal) -> dict[str, Any]:
        self._require_authenticated(principal)
        post = self.store.get_post(post_id)
        if not post:
            raise ResourceNotFound("post_not_found")
        authorize_manage_post(principal, post)
        return post

    def delete_remote_file(self, post_id: str, *, provider, principal: RequestPrincipal,
                           to_trash: bool = True) -> None:
        """Explicit admin action to remove the remote file; never automatic on unpublish."""
        self._require_authenticated(principal)
        post = self.store.get_post(post_id)
        if not post:
            raise ResourceNotFound("post_not_found")
        authorize_delete_file(principal, post)
        file = self.store.file_for_post(post_id)
        if (
            not file
            or file["upload_status"] != FileStatus.VERIFIED
            or not file["storage_file_id"]
        ):
            return
        self.store.update_file(file["id"], upload_status=FileStatus.DELETING)
        if to_trash:
            provider.move_to_trash(file["storage_file_id"])
        else:
            provider.delete_file(file["storage_file_id"])
        self.store.update_file(file["id"], upload_status=FileStatus.DELETED, deleted_at=time.time())
        self.store.add_event(post_id, principal.user_id, "file_deleted",
                             {"to_trash": to_trash})

    @staticmethod
    def _require_authenticated(principal: RequestPrincipal) -> None:
        if not isinstance(principal, RequestPrincipal) or not principal.authenticated:
            raise AuthenticationRequired("authentication_required")
