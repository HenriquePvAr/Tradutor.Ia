"""Community database — the single source of truth for posts and their files.

Posts, files and an audit log live here, never in the storage backend: the feed and the
reader query this database, and Google Drive only ever holds PDF bytes. Kept in its own
SQLite file so it can be backed up and evolved independently of the job queue. No PDF is
ever stored as a BLOB - only a reference to the file in the storage provider.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1


class PostStatus:
    DRAFT = "draft"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    UNPUBLISHED = "unpublished"
    BLOCKED = "blocked"
    FAILED = "failed"
    DELETED = "deleted"
    ALL = frozenset({DRAFT, PUBLISHING, PUBLISHED, UNPUBLISHED, BLOCKED, FAILED, DELETED})


class FileStatus:
    PENDING = "pending"
    UPLOADING = "uploading"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"
    ALL = frozenset({PENDING, UPLOADING, VERIFYING, VERIFIED, FAILED, DELETING, DELETED})


class Moderation:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    BLOCKED = "blocked"


class Visibility:
    PUBLIC = "public"
    UNLISTED = "unlisted"
    PRIVATE = "private"
    ALL = frozenset({PUBLIC, UNLISTED, PRIVATE})


class CommunityStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    # ---- schema -------------------------------------------------------------
    def _migrate(self) -> None:
        self._conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if (int(row["value"]) if row else 0) < 1:
            self._create_v1()
        self._conn.execute(
            "INSERT INTO meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(SCHEMA_VERSION),))

    def _create_v1(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS community_posts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                source_job_id TEXT,
                source_run_id TEXT,
                series_title TEXT,
                series_slug TEXT,
                episode_number TEXT,
                output_dir TEXT,
                title TEXT,
                description TEXT,
                cover_reference TEXT,
                status TEXT NOT NULL,
                visibility TEXT NOT NULL,
                moderation_status TEXT NOT NULL,
                views INTEGER DEFAULT 0,
                published_at REAL,
                unpublished_at REAL,
                created_at REAL,
                updated_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_posts_status ON community_posts(status, published_at);
            CREATE INDEX IF NOT EXISTS idx_posts_series ON community_posts(series_slug, episode_number);
            CREATE INDEX IF NOT EXISTS idx_posts_user ON community_posts(user_id);
            CREATE INDEX IF NOT EXISTS idx_posts_mod ON community_posts(moderation_status);

            CREATE TABLE IF NOT EXISTS community_files (
                id TEXT PRIMARY KEY,
                post_id TEXT NOT NULL,
                storage_provider TEXT,
                storage_file_id TEXT,
                storage_folder_id TEXT,
                filename TEXT,
                mime_type TEXT,
                size_bytes INTEGER,
                sha256 TEXT,
                provider_checksum TEXT,
                upload_job_id TEXT,
                upload_status TEXT NOT NULL,
                bytes_uploaded INTEGER DEFAULT 0,
                session_ref TEXT,
                verified_at REAL,
                deleted_at REAL,
                created_at REAL,
                updated_at REAL,
                FOREIGN KEY(post_id) REFERENCES community_posts(id)
            );
            CREATE INDEX IF NOT EXISTS idx_files_post ON community_files(post_id);
            CREATE INDEX IF NOT EXISTS idx_files_sha ON community_files(sha256);
            CREATE UNIQUE INDEX IF NOT EXISTS uq_files_post_sha ON community_files(post_id, sha256);

            CREATE TABLE IF NOT EXISTS community_events (
                id TEXT PRIMARY KEY,
                post_id TEXT,
                actor_id TEXT,
                event_type TEXT,
                metadata_json TEXT,
                created_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_events_post ON community_events(post_id);
            """
        )

    # ---- helpers ------------------------------------------------------------
    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return {k: row[k] for k in row.keys()} if row is not None else None

    def _rows(self, cursor) -> list[dict[str, Any]]:
        return [self._row(r) for r in cursor.fetchall()]  # type: ignore[misc]

    # ---- posts --------------------------------------------------------------
    def create_post(self, *, user_id: str, source_job_id: str = "", source_run_id: str = "",
                    series_title: str = "", series_slug: str = "", episode_number: str = "",
                    output_dir: str = "", title: str = "", description: str = "",
                    cover_reference: str = "", visibility: str = Visibility.PUBLIC) -> str:
        if visibility not in Visibility.ALL:
            raise ValueError(f"invalid visibility: {visibility}")
        post_id = uuid.uuid4().hex
        now = time.time()
        self._conn.execute(
            """INSERT INTO community_posts(id,user_id,source_job_id,source_run_id,series_title,
               series_slug,episode_number,output_dir,title,description,cover_reference,status,
               visibility,moderation_status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (post_id, user_id, source_job_id, source_run_id, series_title, series_slug,
             episode_number, output_dir, title, description, cover_reference, PostStatus.DRAFT,
             visibility, Moderation.PENDING, now, now))
        self.add_event(post_id, user_id, "post_created", {"series_slug": series_slug})
        return post_id

    def get_post(self, post_id: str) -> dict[str, Any] | None:
        return self._row(self._conn.execute(
            "SELECT * FROM community_posts WHERE id=?", (post_id,)).fetchone())

    def set_post_status(self, post_id: str, status: str, *, actor_id: str = "",
                        moderation_status: str | None = None) -> None:
        if status not in PostStatus.ALL:
            raise ValueError(f"invalid post status: {status}")
        fields: dict[str, Any] = {"status": status, "updated_at": time.time()}
        if status == PostStatus.PUBLISHED:
            fields["published_at"] = time.time()
        if status == PostStatus.UNPUBLISHED:
            fields["unpublished_at"] = time.time()
        if moderation_status is not None:
            fields["moderation_status"] = moderation_status
        cols = ", ".join(f"{k}=?" for k in fields)
        self._conn.execute(f"UPDATE community_posts SET {cols} WHERE id=?",
                           (*fields.values(), post_id))
        self.add_event(post_id, actor_id, f"status_{status}", {})

    def increment_views(self, post_id: str) -> None:
        self._conn.execute(
            "UPDATE community_posts SET views=views+1 WHERE id=?", (post_id,))

    def feed(self, *, series_slug: str = "", user_id: str = "", query: str = "",
             include_unlisted: bool = False, require_moderation: bool = True,
             limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """Published posts for the feed. Reads only this database - never the provider."""
        where = ["status=?"]
        params: list[Any] = [PostStatus.PUBLISHED]
        visibilities = [Visibility.PUBLIC] + ([Visibility.UNLISTED] if include_unlisted else [])
        where.append("visibility IN (%s)" % ",".join("?" for _ in visibilities))
        params += visibilities
        if require_moderation:
            where.append("moderation_status=?")
            params.append(Moderation.APPROVED)
        if series_slug:
            where.append("series_slug=?")
            params.append(series_slug)
        if user_id:
            where.append("user_id=?")
            params.append(user_id)
        if query:
            where.append("(title LIKE ? OR series_title LIKE ?)")
            params += [f"%{query}%", f"%{query}%"]
        params += [int(limit), int(offset)]
        cur = self._conn.execute(
            f"SELECT * FROM community_posts WHERE {' AND '.join(where)} "
            "ORDER BY published_at DESC LIMIT ? OFFSET ?", params)
        return self._rows(cur)

    def list_user_posts(self, user_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        return self._rows(self._conn.execute(
            "SELECT * FROM community_posts WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, int(limit))))

    def active_publish_exists(self, post_id: str) -> bool:
        row = self._conn.execute(
            "SELECT COUNT(*) c FROM community_files WHERE post_id=? AND upload_status IN (?,?,?)",
            (post_id, FileStatus.PENDING, FileStatus.UPLOADING, FileStatus.VERIFYING)).fetchone()
        return bool(row["c"])

    def published_sha_exists(self, sha256: str, *, exclude_post: str = "") -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT f.* FROM community_files f JOIN community_posts p ON f.post_id=p.id
               WHERE f.sha256=? AND f.upload_status=? AND p.status=? AND f.post_id != ?
               LIMIT 1""",
            (sha256, FileStatus.VERIFIED, PostStatus.PUBLISHED, exclude_post)).fetchone()
        return self._row(row)

    # ---- files --------------------------------------------------------------
    def create_file(self, *, post_id: str, filename: str, mime_type: str, size_bytes: int,
                    sha256: str, storage_provider: str, storage_folder_id: str = "") -> str:
        file_id = uuid.uuid4().hex
        now = time.time()
        self._conn.execute(
            """INSERT INTO community_files(id,post_id,storage_provider,storage_folder_id,filename,
               mime_type,size_bytes,sha256,upload_status,bytes_uploaded,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,0,?,?)""",
            (file_id, post_id, storage_provider, storage_folder_id, filename, mime_type,
             size_bytes, sha256, FileStatus.PENDING, now, now))
        return file_id

    def get_file(self, file_id: str) -> dict[str, Any] | None:
        return self._row(self._conn.execute(
            "SELECT * FROM community_files WHERE id=?", (file_id,)).fetchone())

    def file_for_post(self, post_id: str) -> dict[str, Any] | None:
        return self._row(self._conn.execute(
            "SELECT * FROM community_files WHERE post_id=? AND upload_status != ? "
            "ORDER BY created_at DESC LIMIT 1", (post_id, FileStatus.DELETED)).fetchone())

    def update_file(self, file_id: str, **fields: Any) -> None:
        allowed = {"storage_file_id", "storage_folder_id", "provider_checksum", "upload_job_id",
                   "upload_status", "bytes_uploaded", "session_ref", "verified_at", "deleted_at"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown file columns: {bad}")
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._conn.execute(f"UPDATE community_files SET {cols} WHERE id=?",
                           (*fields.values(), file_id))

    # ---- events -------------------------------------------------------------
    def add_event(self, post_id: str, actor_id: str, event_type: str, metadata: dict) -> None:
        self._conn.execute(
            "INSERT INTO community_events(id,post_id,actor_id,event_type,metadata_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (uuid.uuid4().hex, post_id, actor_id, event_type,
             json.dumps(metadata, ensure_ascii=False), time.time()))

    def events_for_post(self, post_id: str) -> list[dict[str, Any]]:
        return self._rows(self._conn.execute(
            "SELECT * FROM community_events WHERE post_id=? ORDER BY created_at", (post_id,)))
