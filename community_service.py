"""Community orchestration: validate a local PDF, publish it, read it, unpublish it.

The browser never supplies a file path - it sends identifiers, and this layer resolves
the PDF server-side from the authorized output directory and validates it before anything
is uploaded. Publishing creates a queued ``community_publish`` job that the worker runs;
the post only becomes visible after the upload is verified. Reading streams the PDF from
the storage provider without exposing the provider's file id or any credential.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Callable

from community_store import CommunityStore, FileStatus, Moderation, PostStatus, Visibility

PDF_MAGIC = b"%PDF"
LOCAL_USER_ID = "local"  # single local user until real auth exists
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
                 build_command: Callable[[dict], list[str]] | None = None,
                 user_id: str = LOCAL_USER_ID):
        self.store = store
        self.job_store = job_store
        self.output_root = Path(output_root)
        self.provider_name = provider_name
        self.community_db_path = community_db_path
        self.storage_config = storage_config or {}
        self.user_id = user_id
        # How to turn a publish job into a runnable command; injected so tests can point
        # it at the fake community-publish runner. Defaults to a marker the worker
        # recognises to run the community publish runner by job_type.
        self._build_command = build_command or (lambda payload: ["community_publish"])

    # ---- draft + publish ----------------------------------------------------
    def create_draft(self, *, output_dir: str, source_job_id: str = "", source_run_id: str = "",
                     series_title: str = "", series_slug: str = "", episode_number: str = "",
                     title: str = "", description: str = "", visibility: str = Visibility.PUBLIC,
                     user_id: str | None = None) -> dict[str, Any]:
        out = Path(output_dir).resolve()
        root = self.output_root.resolve()
        if root != out and root not in out.parents:
            raise CommunityError("output_outside_root")
        pdf = self._resolve_pdf(out)
        validate_local_pdf(pdf, self.output_root)
        post_id = self.store.create_post(
            user_id=user_id or self.user_id, source_job_id=source_job_id,
            source_run_id=source_run_id, series_title=series_title, series_slug=series_slug,
            episode_number=episode_number, output_dir=str(out), title=title or series_title,
            description=description, visibility=visibility)
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

    def request_publish(self, post_id: str, *, user_id: str | None = None,
                        force_new_version: bool = False) -> dict[str, Any]:
        post = self.store.get_post(post_id)
        if not post:
            raise CommunityError("post_not_found")
        actor = user_id or self.user_id
        if post["user_id"] != actor:
            raise CommunityError("not_authorized")
        if self.store.active_publish_exists(post_id):
            raise CommunityError("publish_already_active")

        output_dir = self._post_output_dir(post)
        pdf = self._resolve_pdf(output_dir)
        validate_local_pdf(pdf, self.output_root)
        sha256, size = sha256_of_file(pdf)

        duplicate = self.store.published_sha_exists(sha256, exclude_post=post_id)
        if duplicate and not force_new_version:
            raise CommunityError("duplicate_pdf_already_published")

        filename = safe_pdf_filename(post["series_slug"] or "", post["episode_number"] or "", pdf.name)
        file_id = self.store.create_file(
            post_id=post_id, filename=filename, mime_type="application/pdf",
            size_bytes=size, sha256=sha256, storage_provider=self.provider_name)
        payload = {
            "post_id": post_id, "file_id": file_id, "user_id": actor,
            "local_pdf_path": str(pdf), "pdf_filename": filename, "pdf_size": size,
            "pdf_sha256": sha256, "series_slug": post["series_slug"] or "series",
            "series_title": post["series_title"] or "", "episode_number": post["episode_number"] or "",
            "storage_provider": self.provider_name, "visibility": post["visibility"],
        }
        command = self._build_command(payload)
        job_id = self.job_store.create_job(
            source_url="", output_dir=str(output_dir), command=command,
            configuration={
                "job_type": "community_publish",
                "community_db": self.community_db_path,
                "storage": {"storage_provider": self.provider_name, **self.storage_config},
                **payload,
            },
            series_title=post["series_title"] or "", series_slug=post["series_slug"] or "",
            episode_number=post["episode_number"] or "")
        self.store.update_file(file_id, upload_job_id=job_id, upload_status=FileStatus.PENDING)
        self.store.set_post_status(post_id, PostStatus.PUBLISHING, actor_id=actor)
        self.store.add_event(post_id, actor, "publish_requested", {"file_id": file_id, "job_id": job_id})
        return {"post_id": post_id, "file_id": file_id, "job_id": job_id}

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
        posts = self.store.feed(**kwargs)
        return [self._card(post) for post in posts]

    @staticmethod
    def _card(post: dict[str, Any]) -> dict[str, Any]:
        # A feed card exposes only metadata; never the storage file id.
        return {
            "post_id": post["id"],
            "series_title": post["series_title"],
            "series_slug": post["series_slug"],
            "episode_number": post["episode_number"],
            "title": post["title"],
            "description": post["description"],
            "cover_reference": post["cover_reference"],
            "user_id": post["user_id"],
            "views": post["views"],
            "published_at": post["published_at"],
            "status": post["status"],
        }

    def resolve_readable_file(self, post_id: str, *, user_id: str | None = None) -> dict[str, Any]:
        """Return the storage file for a readable post, or raise. Never exposes the id."""
        post = self.store.get_post(post_id)
        if not post:
            raise CommunityError("post_not_found")
        if post["status"] != PostStatus.PUBLISHED:
            raise CommunityError("post_not_published")
        if post["moderation_status"] not in {Moderation.APPROVED, Moderation.PENDING}:
            raise CommunityError("post_blocked")
        if post["visibility"] == Visibility.PRIVATE and (user_id or self.user_id) != post["user_id"]:
            raise CommunityError("not_authorized")
        file = self.store.file_for_post(post_id)
        if not file or file["upload_status"] != FileStatus.VERIFIED or not file["storage_file_id"]:
            raise CommunityError("file_not_available")
        return file

    # ---- lifecycle ----------------------------------------------------------
    def unpublish(self, post_id: str, *, user_id: str | None = None) -> None:
        post = self._owned_post(post_id, user_id)
        self.store.set_post_status(post_id, PostStatus.UNPUBLISHED, actor_id=post["user_id"])

    def _owned_post(self, post_id: str, user_id: str | None) -> dict[str, Any]:
        post = self.store.get_post(post_id)
        if not post:
            raise CommunityError("post_not_found")
        if post["user_id"] != (user_id or self.user_id):
            raise CommunityError("not_authorized")
        return post

    def delete_remote_file(self, post_id: str, *, provider, user_id: str | None = None,
                           to_trash: bool = True) -> None:
        """Explicit admin action to remove the remote file; never automatic on unpublish."""
        self._owned_post(post_id, user_id)
        file = self.store.file_for_post(post_id)
        if not file or not file["storage_file_id"]:
            return
        self.store.update_file(file["id"], upload_status=FileStatus.DELETING)
        if to_trash:
            provider.move_to_trash(file["storage_file_id"])
        else:
            provider.delete_file(file["storage_file_id"])
        import time
        self.store.update_file(file["id"], upload_status=FileStatus.DELETED, deleted_at=time.time())
        self.store.add_event(post_id, user_id or self.user_id, "file_deleted",
                             {"to_trash": to_trash})
