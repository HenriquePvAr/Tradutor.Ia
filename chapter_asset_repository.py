"""Server-side link between a social chapter (Supabase) and a private PDF publication.

The Google Drive file id is NEVER stored here or exposed: this table only records that a
social ``chapter_id`` owned by ``owner_id`` points at an existing private ``publication_id``
(a community-store post whose verified file already holds the Drive id, server-side). The
Drive id is resolved at read time through the community store and streamed by the backend's
own Drive token — the browser never sees it.

Decision (documented): the linkage lives in server-side SQLite via this repository, not in
remote ``private.chapter_assets``. Writing to the private schema from the backend would
require ``service_role`` or the database password (anon/authenticated cannot reach it),
reintroducing an RLS-bypassing credential. SQLite needs no new secret. A future migration
to ``private.chapter_assets`` behind a dedicated least-privilege role stays documented.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any


class ChapterAssetError(Exception):
    """A safe, non-internal error for the asset linkage layer."""


class AssetNotFound(ChapterAssetError):
    """No usable asset for this chapter/owner — surfaced as 404 (anti-enumeration)."""


class AssetConflict(ChapterAssetError):
    """A link already exists; replacement must go through the explicit replace flow."""


class ChapterAssetRepository:
    """SQLite-backed chapter→publication links. Ownership always comes from the caller.

    The client can never supply storage_file_id/drive_file_id/owner_id/provider/path — the
    repository derives owner from the trusted principal and resolves the Drive id through
    the community store, keeping it fully server-side.
    """

    ALLOWED_PROVIDERS = ("google_drive", "filesystem")

    def __init__(self, db_path: str | Path, *, community_store=None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._community = community_store
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        # Additive: create the link table if absent. Never drops or rewrites existing data.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS social_chapter_assets (
                chapter_id TEXT PRIMARY KEY,
                publication_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                storage_provider TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                deleted_at REAL,
                version INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sca_owner ON social_chapter_assets(owner_id)"
        )

    # ---- internal helpers ----------------------------------------------------
    def _row(self, chapter_id: str) -> dict[str, Any] | None:
        r = self._conn.execute(
            "SELECT * FROM social_chapter_assets WHERE chapter_id=?", (chapter_id,)).fetchone()
        return dict(r) if r else None

    def _publication_file(self, publication_id: str, owner_id: str) -> dict[str, Any]:
        """Resolve the verified publication file server-side; never returned to a client."""
        if self._community is None:
            raise AssetNotFound()
        post = self._community.get_post(publication_id)
        if not post or str(post.get("user_id") or "") != owner_id:
            raise AssetNotFound()
        file = self._community.file_for_post(publication_id)
        # Import here to avoid a hard dependency cycle at module import time.
        from community_store import FileStatus, PostStatus
        if not file or file.get("upload_status") != FileStatus.VERIFIED or not file.get("storage_file_id"):
            raise AssetNotFound()
        if post.get("status") == PostStatus.DELETED:
            raise AssetNotFound()
        return file

    def _publication_storage_id(self, publication_id: str, owner_id: str) -> str:
        return str(self._publication_file(publication_id, owner_id)["storage_file_id"])

    # ---- link lifecycle (owner-only; owner_id from the principal) -------------
    def link_asset(self, chapter_id: str, publication_id: str, owner_id: str,
                   *, storage_provider: str = "google_drive") -> None:
        if storage_provider not in self.ALLOWED_PROVIDERS:
            raise ChapterAssetError("unsupported_provider")  # fail closed
        # Prove the publication is the owner's and has a verified file before linking.
        self._publication_storage_id(publication_id, owner_id)
        existing = self._row(chapter_id)
        if existing and existing.get("deleted_at") is None:
            raise AssetConflict()
        now = time.time()
        if existing:
            self._conn.execute(
                "UPDATE social_chapter_assets SET publication_id=?,owner_id=?,storage_provider=?,"
                "updated_at=?,deleted_at=NULL,version=version+1 WHERE chapter_id=?",
                (publication_id, owner_id, storage_provider, now, chapter_id))
        else:
            self._conn.execute(
                "INSERT INTO social_chapter_assets(chapter_id,publication_id,owner_id,"
                "storage_provider,created_at,updated_at,version) VALUES(?,?,?,?,?,?,1)",
                (chapter_id, publication_id, owner_id, storage_provider, now, now))

    def replace_asset(self, chapter_id: str, new_publication_id: str, owner_id: str) -> None:
        """Atomic swap: the old link stays until the new one is proven, then replaces it."""
        current = self._row(chapter_id)
        if not current or current.get("deleted_at") is not None or current["owner_id"] != owner_id:
            raise AssetNotFound()
        # Validate the new publication BEFORE mutating anything; failure preserves the old.
        self._publication_storage_id(new_publication_id, owner_id)
        self._conn.execute(
            "UPDATE social_chapter_assets SET publication_id=?,updated_at=?,version=version+1 "
            "WHERE chapter_id=? AND owner_id=? AND deleted_at IS NULL",
            (new_publication_id, time.time(), chapter_id, owner_id))

    def unlink_asset(self, chapter_id: str, owner_id: str) -> None:
        """Remove only the association (idempotent). Never touches the local file or Drive."""
        row = self._row(chapter_id)
        if not row or row["owner_id"] != owner_id:
            # Idempotent + anti-enumeration: unlinking a non-owned/absent link is a no-op.
            return
        if row.get("deleted_at") is None:
            self._conn.execute(
                "UPDATE social_chapter_assets SET deleted_at=?,updated_at=?,version=version+1 "
                "WHERE chapter_id=? AND owner_id=?",
                (time.time(), time.time(), chapter_id, owner_id))

    # ---- read paths ----------------------------------------------------------
    def get_asset_for_read(self, chapter_id: str) -> str:
        """Return the server-side Drive storage id for a linked, live publication."""
        return self.get_readable_file(chapter_id)["storage_file_id"]

    def get_readable_file(self, chapter_id: str) -> dict[str, Any]:
        """Server-side file record {storage_file_id, size_bytes} for streaming.

        Chapter visibility is enforced upstream (Supabase RLS via the user's token) BEFORE
        this is called; here we only resolve the linked file. Raises AssetNotFound (→404)
        when there is no usable link. The result is used only inside the backend.
        """
        row = self._row(chapter_id)
        if not row or row.get("deleted_at") is not None:
            raise AssetNotFound()
        file = self._publication_file(row["publication_id"], row["owner_id"])
        return {"storage_file_id": str(file["storage_file_id"]), "size_bytes": int(file["size_bytes"])}

    def get_asset_status(self, chapter_id: str, *, is_owner: bool) -> dict[str, Any]:
        """Booleans only — never publication_id, storage id, size, checksum or path."""
        row = self._row(chapter_id)
        linked = bool(row and row.get("deleted_at") is None)
        available = False
        if linked:
            try:
                self._publication_storage_id(row["publication_id"], row["owner_id"])
                available = True
            except AssetNotFound:
                available = False
        status: dict[str, Any] = {"linked": linked, "available": available}
        if is_owner and row:
            status["updated_at"] = row.get("updated_at")
            status["mime_type"] = "application/pdf"
        return status
