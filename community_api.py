"""Wires the community components from configuration for the API layer.

Reads the storage choice from the environment with a safe local default (a private
filesystem-backed fake under .cache, so local development works without Google Drive),
builds the CommunityStore and CommunityService, and exposes the feed / publish /
unpublish / read-stream operations the HTTP endpoints call. The Google Drive provider is
only built when configured with real OAuth, which this task does not run.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from community_service import CommunityService, CommunityError, LOCAL_USER_ID
from community_storage import FilesystemStorageProvider, FakeStorageProvider, StorageError
from community_store import CommunityStore
from ui_helpers import OUTPUT_ROOT, REPO_ROOT

COMMUNITY_DB_PATH = REPO_ROOT / ".cache" / "runtime" / "community.sqlite3"
COMMUNITY_STORAGE_ROOT = REPO_ROOT / ".cache" / "runtime" / "community_storage"


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
                 output_root: Path = OUTPUT_ROOT, user_id: str = LOCAL_USER_ID):
        self.store = CommunityStore(community_db_path)
        self.service = CommunityService(
            self.store, job_store, output_root=Path(output_root),
            provider_name=storage_provider_name(), community_db_path=str(community_db_path),
            storage_config={k: v for k, v in _storage_config().items() if k != "storage_provider"},
            user_id=user_id)
        self.user_id = user_id

    def close(self) -> None:
        self.store.close()

    # ---- operations for the endpoints --------------------------------------
    def publish(self, payload: dict[str, Any]) -> dict[str, Any]:
        output_dir = self._resolve_output_dir(payload)
        draft = self.service.create_draft(
            output_dir=output_dir, source_job_id=str(payload.get("source_job_id") or ""),
            source_run_id=str(payload.get("source_run_id") or ""),
            series_title=str(payload.get("series_title") or ""),
            series_slug=str(payload.get("series_slug") or ""),
            episode_number=str(payload.get("episode_number") or ""),
            title=str(payload.get("title") or ""),
            description=str(payload.get("description") or "")[:2000],
            visibility=str(payload.get("visibility") or "public"))
        return self.service.request_publish(draft["post_id"],
                                            force_new_version=bool(payload.get("force_new_version")))

    def _resolve_output_dir(self, payload: dict[str, Any]) -> str:
        # The browser sends a slug or a job id, never a path. Resolve server-side and
        # constrain to OUTPUT_ROOT.
        slug = str(payload.get("slug") or "").strip()
        job_id = str(payload.get("source_job_id") or "").strip()
        if job_id:
            job = self.service.job_store.get_job(job_id)
            if job and job.get("output_dir"):
                return job["output_dir"]
        if slug:
            from ui_helpers import sanitize_output_name
            return str((OUTPUT_ROOT / sanitize_output_name(slug)).resolve())
        raise CommunityError("missing_output_identifier")

    def unpublish(self, post_id: str) -> dict[str, Any]:
        self.service.unpublish(post_id)
        return {"ok": True}

    def feed(self, *, series_slug: str = "", query: str = "", limit: int = 50,
             offset: int = 0) -> dict[str, Any]:
        cards = self.service.feed(series_slug=series_slug, query=query,
                                  limit=min(100, max(1, limit)), offset=max(0, offset))
        return {"posts": cards, "count": len(cards)}

    def my_posts(self) -> dict[str, Any]:
        posts = self.store.list_user_posts(self.user_id)
        return {"posts": [self.service._card(p) | {"visibility": p["visibility"]} for p in posts]}

    def open_pdf(self, post_id: str, *, range_header: str = ""):
        """Return (metadata, StorageStream) for the read endpoint, or raise CommunityError."""
        file = self.service.resolve_readable_file(post_id)
        self.store.increment_views(post_id)
        provider = build_read_provider()
        total = int(file["size_bytes"])
        start, end = _parse_range(range_header, total)
        stream = provider.open_stream(file["storage_file_id"], start=start, end=end)
        return {
            "filename": _safe_disposition_name(file["filename"]),
            "mime_type": "application/pdf",
            "total_size": total,
            "start": stream.start,
            "end": stream.end,
            "content_length": stream.content_length,
            "partial": range_header != "",
        }, stream


def _parse_range(range_header: str, total: int) -> tuple[int | None, int | None]:
    if not range_header or not range_header.startswith("bytes="):
        return None, None
    spec = range_header.split("=", 1)[1].split(",", 1)[0].strip()
    lo_s, _, hi_s = spec.partition("-")
    try:
        lo = int(lo_s) if lo_s else 0
        hi = int(hi_s) if hi_s else total - 1
    except ValueError:
        return None, None
    lo = max(0, lo)
    hi = min(total - 1, hi)
    if lo > hi:
        return 0, min(total - 1, 0)
    return lo, hi


def _safe_disposition_name(name: str) -> str:
    # Strip anything that could break the Content-Disposition header.
    import re
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', "_", Path(name).name).strip("_")
    return cleaned or "capitulo.pdf"
