"""Explicit, transactional publishing of a local PDF into a social chapter.

Nothing publishes automatically. The only entry point is an explicit, authenticated
"publish" action. The service reuses the existing (audited) community publish flow to
upload the local translation-job PDF to the private Google Drive, then — only after the
upload is verified — links the social chapter to that private publication and applies the
chosen status. Ownership always comes from the principal; the browser never sends a path,
filename, owner id, storage id or Drive id. A publish intent makes the async upload
idempotent so a double click cannot start two uploads.
"""

from __future__ import annotations

from typing import Any

from community_auth import AuthenticationRequired, RequestPrincipal
from community_store import FileStatus, PostStatus
from chapter_asset_repository import AssetConflict, AssetNotFound
from job_store import JobStatus
from supabase_social import SocialNotFound, SocialValidationError

TARGET_STATUSES = ("private", "community")
_HEX32 = set("0123456789abcdef")


class SocialPdfPublishingService:
    def __init__(self, community_api, asset_repo, social_repo, job_store):
        self._community = community_api
        self._assets = asset_repo
        self._social = social_repo
        self._jobs = job_store

    # ---- ownership --------------------------------------------------------------
    def _require_authenticated(self, principal: RequestPrincipal) -> None:
        if not isinstance(principal, RequestPrincipal) or not principal.authenticated:
            raise AuthenticationRequired("authentication_required")

    def _require_chapter_owner(self, token: str, principal: RequestPrincipal, chapter_id: str) -> dict[str, Any]:
        """Prove the caller owns the chapter's work (via the user's token + RLS)."""
        chapter = self._social.get_chapter(token, chapter_id)  # visible or SocialNotFound
        work = self._social.get_work(token, str(chapter.get("work_id") or ""))
        if str(work.get("owner_id") or "") != principal.user_id:
            raise SocialNotFound()  # anti-enumeration: not the owner → 404
        return chapter

    # ---- listing publishable local results -------------------------------------
    def list_local_results(self, principal: RequestPrincipal) -> dict[str, Any]:
        """Only app-registered translation jobs owned by this user; never a path/scan."""
        self._require_authenticated(principal)
        items = []
        for job in self._jobs.list_jobs(statuses=None, limit=200):
            config = job.get("configuration") or {}
            if config.get("job_type") != "translation":
                continue
            if str(config.get("community_owner_id") or "") != principal.user_id:
                continue
            if job.get("status") not in {JobStatus.FINISHED, JobStatus.REVIEW_REQUIRED}:
                continue
            if job.get("exit_code") not in (0, "0"):
                continue
            items.append({
                "source_job_id": job["id"],
                "title": str(config.get("chapter_name") or job.get("series_title") or "capítulo"),
                "created_at": job.get("created_at"),
                "state": job.get("status"),
            })
        return {"items": items}

    # ---- explicit publish -------------------------------------------------------
    def publish_pdf(self, token: str, principal: RequestPrincipal, chapter_id: str,
                    source_job_id: str, target_status: str, *, idempotency_key: str = "") -> dict[str, Any]:
        self._require_authenticated(principal)
        if target_status not in TARGET_STATUSES:
            raise SocialValidationError("invalid_target_status")
        source_job_id = str(source_job_id or "")
        if len(source_job_id) != 32 or any(c not in _HEX32 for c in source_job_id):
            raise SocialNotFound()  # opaque id only; bad shape → 404
        self._require_chapter_owner(token, principal, chapter_id)

        existing = self._assets.get_intent(chapter_id, principal.user_id)
        if existing and existing["source_job_id"] == source_job_id:
            # Idempotent: a duplicate click returns the same in-flight publication.
            return self.publish_status(token, principal, chapter_id)

        # Start the private upload through the existing community flow. It validates the
        # job ownership, the manifest, the PDF (in-root, magic bytes), size and checksum,
        # and uploads to the private Drive. visibility stays private at the community layer;
        # the social status is applied only after verification below.
        result = self._community.publish(
            {"source_job_id": source_job_id, "visibility": "private"}, principal=principal)
        publication_id = result["post_id"]
        self._assets.set_intent(chapter_id, publication_id, target_status,
                                principal.user_id, source_job_id, idempotency_key)
        return {"status": "pending"}

    def publish_status(self, token: str, principal: RequestPrincipal, chapter_id: str) -> dict[str, Any]:
        self._require_authenticated(principal)
        self._require_chapter_owner(token, principal, chapter_id)
        intent = self._assets.get_intent(chapter_id, principal.user_id)
        if not intent:
            # Nothing in flight: report from the current link state.
            status = self._assets.get_asset_status(chapter_id, is_owner=True)
            return {"status": "published" if status.get("available") else "none"}
        publication_id = intent["publication_id"]
        post = self._community.store.get_post(publication_id)
        file = self._community.store.file_for_post(publication_id) if post else None
        if not post or (post.get("user_id") or "") != principal.user_id:
            self._assets.clear_intent(chapter_id, principal.user_id)
            return {"status": "failed"}
        if post.get("status") == PostStatus.FAILED or (file and file.get("upload_status") == FileStatus.FAILED):
            # Failure: keep the chapter invisible; nothing to link.
            self._assets.clear_intent(chapter_id, principal.user_id)
            return {"status": "failed"}
        verified = bool(file and file.get("upload_status") == FileStatus.VERIFIED
                        and file.get("storage_file_id"))
        if not verified:
            return {"status": "pending"}
        # Verified: link the chapter → publication and apply the chosen status. Only now
        # can the chapter become visible with a valid asset.
        is_replace = intent["target_status"] == "__replace__"
        try:
            if is_replace:
                self._assets.replace_asset(chapter_id, publication_id, principal.user_id)
            else:
                self._assets.link_asset(chapter_id, publication_id, principal.user_id)
        except AssetConflict:
            # Already linked (e.g. a re-publish): swap atomically to the new publication.
            self._assets.replace_asset(chapter_id, publication_id, principal.user_id)
        except AssetNotFound:
            self._assets.clear_intent(chapter_id, principal.user_id)
            return {"status": "failed"}
        if not is_replace:
            # Apply the chosen visibility only after a valid asset is linked.
            self._social.update_chapter(token, chapter_id, {"status": intent["target_status"]})
        self._assets.clear_intent(chapter_id, principal.user_id)
        return {"status": "published", "target_status": None if is_replace else intent["target_status"]}

    # ---- asset lifecycle (owner-only) ------------------------------------------
    def get_asset(self, token: str, principal: RequestPrincipal, chapter_id: str) -> dict[str, Any]:
        self._require_authenticated(principal)
        # Any authenticated viewer who can see the chapter may query availability; only the
        # owner gets the extra (still non-sensitive) fields.
        self._social.get_chapter(token, chapter_id)  # visible or SocialNotFound
        is_owner = self._is_owner(token, principal, chapter_id)
        return self._assets.get_asset_status(chapter_id, is_owner=is_owner)

    def replace_asset(self, token: str, principal: RequestPrincipal, chapter_id: str,
                      source_job_id: str, *, idempotency_key: str = "") -> dict[str, Any]:
        self._require_authenticated(principal)
        source_job_id = str(source_job_id or "")
        if len(source_job_id) != 32 or any(c not in _HEX32 for c in source_job_id):
            raise SocialNotFound()
        self._require_chapter_owner(token, principal, chapter_id)
        # Upload the new PDF first; the old link stays until the new one is verified.
        result = self._community.publish(
            {"source_job_id": source_job_id, "visibility": "private"}, principal=principal)
        self._assets.set_intent(chapter_id, result["post_id"], "__replace__",
                                principal.user_id, source_job_id, idempotency_key)
        return {"status": "pending"}

    def unlink_asset(self, token: str, principal: RequestPrincipal, chapter_id: str) -> dict[str, Any]:
        self._require_authenticated(principal)
        self._require_chapter_owner(token, principal, chapter_id)
        self._assets.unlink_asset(chapter_id, principal.user_id)
        self._assets.clear_intent(chapter_id, principal.user_id)
        return {"ok": True}

    def _is_owner(self, token: str, principal: RequestPrincipal, chapter_id: str) -> bool:
        try:
            self._require_chapter_owner(token, principal, chapter_id)
            return True
        except SocialNotFound:
            return False
