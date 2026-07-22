"""Community database — the single source of truth for posts and their files.

Posts, files and an audit log live here, never in the storage backend: the feed and the
reader query this database, and Google Drive only ever holds PDF bytes. Kept in its own
SQLite file so it can be backed up and evolved independently of the job queue. No PDF is
ever stored as a BLOB - only a reference to the file in the storage provider.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 3
MAX_TAGS = 20
MAX_TAG_LENGTH = 40
_DISPLAY_NAME_PATTERN = re.compile(r"^[\w][\w .'-]{0,59}$", re.UNICODE)
_RESERVED_DISPLAY_NAMES = frozenset({"admin", "administrator", "moderator", "support", "system", "root", "official"})
_TAG_PATTERN = re.compile(r"^[\wÀ-ÿ][\wÀ-ÿ ._-]*$", re.UNICODE)


def normalize_tags(tags: Any) -> list[str]:
    """Normalize publication tags deterministically at the server boundary."""
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = tags.split(",")
    if not isinstance(tags, (list, tuple)):
        raise ValueError("invalid_tags")
    result: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        value = str(raw or "").strip()
        if not value:
            continue
        if len(value) > MAX_TAG_LENGTH or not _TAG_PATTERN.fullmatch(value):
            raise ValueError("invalid_tags")
        folded = value.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        result.append(value)
        if len(result) > MAX_TAGS:
            raise ValueError("too_many_tags")
    return result


def normalize_display_name(value: Any) -> tuple[str, str]:
    display_name = " ".join(str(value or "").split())
    if not _DISPLAY_NAME_PATTERN.fullmatch(display_name):
        raise ValueError("invalid_display_name")
    normalized = display_name.casefold()
    if normalized in _RESERVED_DISPLAY_NAMES:
        raise ValueError("display_name_reserved")
    return display_name, normalized


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
        columns = {str(item["name"]) for item in self._conn.execute("PRAGMA table_info(community_posts)")}
        if "tags_json" not in columns:
            self._conn.execute(
                "ALTER TABLE community_posts ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'"
            )
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS community_profiles (
                user_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                display_name_normalized TEXT NOT NULL,
                avatar_object_key TEXT NOT NULL DEFAULT '',
                banner_object_key TEXT NOT NULL DEFAULT '',
                public_role TEXT NOT NULL DEFAULT '',
                pronouns TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'online',
                status_message TEXT NOT NULL DEFAULT '',
                bio TEXT NOT NULL DEFAULT '',
                accent_color TEXT NOT NULL DEFAULT '#c5372c',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_community_profiles_display_name
                ON community_profiles(display_name_normalized);
            """
        )
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

    # ---- public profiles ----------------------------------------------------
    def upsert_profile(self, user_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        user_id = str(user_id or "").strip()
        if not user_id:
            raise ValueError("authentication_required")
        display_name, normalized = normalize_display_name(fields.get("display_name"))
        now = time.time()
        values = (
            user_id, display_name, normalized,
            str(fields.get("avatar_object_key") or ""),
            str(fields.get("banner_object_key") or ""),
            str(fields.get("public_role") or "")[:80],
            str(fields.get("pronouns") or "")[:30],
            str(fields.get("status") or "online")[:16],
            str(fields.get("status_message") or "")[:120],
            str(fields.get("bio") or "")[:500],
            str(fields.get("accent_color") or "#c5372c")[:16],
            now, now,
        )
        try:
            self._conn.execute(
                """INSERT INTO community_profiles(
                    user_id,display_name,display_name_normalized,avatar_object_key,banner_object_key,
                    public_role,pronouns,status,status_message,bio,accent_color,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    display_name_normalized=excluded.display_name_normalized,
                    avatar_object_key=excluded.avatar_object_key,
                    banner_object_key=excluded.banner_object_key,
                    public_role=excluded.public_role,
                    pronouns=excluded.pronouns,
                    status=excluded.status,
                    status_message=excluded.status_message,
                    bio=excluded.bio,
                    accent_color=excluded.accent_color,
                    updated_at=excluded.updated_at""", values)
        except sqlite3.IntegrityError as exc:
            if "display_name" in str(exc).casefold():
                raise ValueError("display_name_taken") from exc
            raise
        return self.profile_public(user_id)

    def profile_public(self, user_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT user_id,display_name,avatar_object_key,banner_object_key,public_role,pronouns,status,status_message,bio,accent_color "
            "FROM community_profiles WHERE user_id=?", (str(user_id),)).fetchone()
        if row is None:
            return {"user_id": str(user_id), "display_name": "Usuário", "avatar_object_key": "", "banner_object_key": "", "public_role": ""}
        return self._row(row) or {}

    def profile_public_for_users(self, user_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        ids = [str(value or "") for value in user_ids if str(value or "")]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self._rows(self._conn.execute(
            f"SELECT user_id,display_name,avatar_object_key,banner_object_key,public_role,pronouns,status,status_message,bio,accent_color "
            f"FROM community_profiles WHERE user_id IN ({placeholders})", ids))
        result = {str(row["user_id"]): row for row in rows}
        for user_id in ids:
            result.setdefault(user_id, {"user_id": user_id, "display_name": "Usuário", "avatar_object_key": "", "banner_object_key": "", "public_role": ""})
        return result

    # ---- posts --------------------------------------------------------------
    def create_post(self, *, user_id: str, source_job_id: str = "", source_run_id: str = "",
                    series_title: str = "", series_slug: str = "", episode_number: str = "",
                    output_dir: str = "", title: str = "", description: str = "",
                    cover_reference: str = "", visibility: str = Visibility.PUBLIC,
                    tags: Any = None) -> str:
        if visibility not in Visibility.ALL:
            raise ValueError(f"invalid visibility: {visibility}")
        post_id = uuid.uuid4().hex
        now = time.time()
        normalized_tags = normalize_tags(tags)
        self._conn.execute(
            """INSERT INTO community_posts(id,user_id,source_job_id,source_run_id,series_title,
               series_slug,episode_number,output_dir,title,description,cover_reference,tags_json,status,
               visibility,moderation_status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (post_id, user_id, source_job_id, source_run_id, series_title, series_slug,
             episode_number, output_dir, title, description, cover_reference,
             json.dumps(normalized_tags, ensure_ascii=False, separators=(',', ':')), PostStatus.DRAFT,
             visibility, Moderation.PENDING, now, now))
        self.add_event(post_id, user_id, "post_created", {"series_slug": series_slug})
        return post_id

    def create_or_get_source_post(self, **fields: Any) -> tuple[str, bool]:
        """Atomically claim one post per owner/source job for idempotent publishing."""
        user_id = str(fields.get("user_id") or "")
        source_job_id = str(fields.get("source_job_id") or "")
        if not source_job_id:
            return self.create_post(**fields), True
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            existing = self._conn.execute(
                "SELECT id FROM community_posts WHERE user_id=? AND source_job_id=? "
                "ORDER BY created_at ASC, rowid ASC LIMIT 1",
                (user_id, source_job_id),
            ).fetchone()
            if existing:
                self._conn.execute("COMMIT")
                return str(existing["id"]), False
            post_id = self.create_post(**fields)
            self._conn.execute("COMMIT")
            return post_id, True
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def get_post(self, post_id: str) -> dict[str, Any] | None:
        return self._with_decoded_tags(self._row(self._conn.execute(
            "SELECT * FROM community_posts WHERE id=?", (post_id,)).fetchone()))

    def post_for_owner_source(self, user_id: str, source_job_id: str) -> dict[str, Any] | None:
        if not source_job_id:
            return None
        return self._with_decoded_tags(self._row(self._conn.execute(
            "SELECT * FROM community_posts WHERE user_id=? AND source_job_id=? "
            "ORDER BY created_at ASC,rowid ASC LIMIT 1",
            (user_id, source_job_id),
        ).fetchone()))

    def post_for_any_source(self, source_job_id: str) -> dict[str, Any] | None:
        """Return the first publication linked to a source job, regardless of owner."""
        if not source_job_id:
            return None
        return self._with_decoded_tags(self._row(self._conn.execute(
            "SELECT * FROM community_posts WHERE source_job_id=? "
            "ORDER BY created_at ASC,rowid ASC LIMIT 1",
            (source_job_id,),
        ).fetchone()))

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
             require_verified_file: bool = False,
             limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """Published posts for the feed. Reads only this database - never the provider."""
        where = ["community_posts.status=?"]
        params: list[Any] = [PostStatus.PUBLISHED]
        visibilities = [Visibility.PUBLIC] + ([Visibility.UNLISTED] if include_unlisted else [])
        where.append("community_posts.visibility IN (%s)" % ",".join("?" for _ in visibilities))
        params += visibilities
        if require_moderation:
            where.append("community_posts.moderation_status=?")
            params.append(Moderation.APPROVED)
        else:
            # Publication and moderation are separate lifecycles. The normal
            # authenticated feed includes posts awaiting review, but never
            # posts explicitly rejected or blocked by moderation.
            where.append("community_posts.moderation_status IN (?, ?)")
            params.extend((Moderation.APPROVED, Moderation.PENDING))
        if require_verified_file:
            where.append(
                "EXISTS (SELECT 1 FROM community_files f WHERE f.id=(SELECT latest.id "
                "FROM community_files latest WHERE latest.post_id=community_posts.id "
                "ORDER BY latest.created_at DESC, latest.rowid DESC "
                "LIMIT 1) AND f.upload_status=? AND f.storage_file_id IS NOT NULL "
                "AND f.storage_file_id != '')"
            )
            params.append(FileStatus.VERIFIED)
        if series_slug:
            where.append("community_posts.series_slug=?")
            params.append(series_slug)
        if user_id:
            where.append("community_posts.user_id=?")
            params.append(user_id)
        if query:
            where.append("(community_posts.title LIKE ? OR community_posts.series_title LIKE ?)")
            params += [f"%{query}%", f"%{query}%"]
        params += [int(limit), int(offset)]
        cur = self._conn.execute(
            f"SELECT community_posts.*, cp.display_name AS author_display_name, "
            "cp.avatar_object_key AS author_avatar_object_key, cp.public_role AS author_public_role "
            "FROM community_posts LEFT JOIN community_profiles cp ON cp.user_id=community_posts.user_id "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY published_at DESC LIMIT ? OFFSET ?", params)
        return [self._with_decoded_tags(row) for row in self._rows(cur)]

    def list_user_posts(self, user_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        return [self._with_decoded_tags(row) for row in self._rows(self._conn.execute(
            "SELECT * FROM community_posts WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, int(limit))))]

    @staticmethod
    def _with_decoded_tags(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        raw = row.get("tags_json", "[]")
        try:
            decoded = json.loads(raw) if isinstance(raw, str) else raw
            row["tags"] = normalize_tags(decoded)
        except (TypeError, ValueError, json.JSONDecodeError):
            row["tags"] = []
        return row

    def active_publish_exists(self, post_id: str) -> bool:
        row = self._conn.execute(
            "SELECT COUNT(*) c FROM community_files f JOIN community_posts p ON p.id=f.post_id "
            "WHERE f.post_id=? AND p.status=? AND f.upload_status IN (?,?,?)",
            (
                post_id,
                PostStatus.PUBLISHING,
                FileStatus.PENDING,
                FileStatus.UPLOADING,
                FileStatus.VERIFYING,
            )).fetchone()
        return bool(row["c"])

    def active_publish_result(self, post_id: str) -> dict[str, Any] | None:
        return self._row(self._conn.execute(
            "SELECT f.id AS file_id,f.upload_job_id AS job_id FROM community_files f "
            "JOIN community_posts p ON p.id=f.post_id WHERE f.post_id=? AND p.status=? "
            "AND f.upload_status IN (?,?,?) ORDER BY f.created_at DESC,f.rowid DESC LIMIT 1",
            (
                post_id,
                PostStatus.PUBLISHING,
                FileStatus.PENDING,
                FileStatus.UPLOADING,
                FileStatus.VERIFYING,
            ),
        ).fetchone())

    def published_sha_exists(self, sha256: str, *, exclude_post: str = "") -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT f.* FROM community_files f JOIN community_posts p ON f.post_id=p.id
               WHERE f.sha256=? AND f.upload_status=? AND p.status=? AND f.post_id != ?
               LIMIT 1""",
            (sha256, FileStatus.VERIFIED, PostStatus.PUBLISHED, exclude_post)).fetchone()
        return self._row(row)

    def blocking_sha_exists(self, sha256: str, *, exclude_post: str = "") -> dict[str, Any] | None:
        """A live or completed upload that blocks an accidental duplicate."""
        row = self._conn.execute(
            "SELECT f.* FROM community_files f JOIN community_posts p ON f.post_id=p.id "
            "WHERE f.sha256=? AND f.post_id != ? AND ("
            "(f.upload_status=? AND p.status=?) OR "
            "(f.upload_status IN (?,?,?) AND p.status=?)) "
            "ORDER BY f.created_at ASC LIMIT 1",
            (
                sha256,
                exclude_post,
                FileStatus.VERIFIED,
                PostStatus.PUBLISHED,
                FileStatus.PENDING,
                FileStatus.UPLOADING,
                FileStatus.VERIFYING,
                PostStatus.PUBLISHING,
            ),
        ).fetchone()
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

    def prepare_publish_attempt(
        self,
        *,
        post_id: str,
        sha256: str,
        actor_id: str,
        allow_duplicate: bool = False,
    ) -> dict[str, Any]:
        """Classify a publish from one database snapshot before creating a job.

        Active and completed attempts are idempotent and need no new queue row.  A
        retryable/new attempt returns the exact file id snapshot that the later STAGING
        job will embed.  ``activate_publish_attempt`` repeats the classification under
        its own write transaction, so a change between these two phases remains safe.
        """
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            outcome, existing = self._classify_publish_attempt(
                post_id=post_id,
                sha256=sha256,
                actor_id=actor_id,
                allow_duplicate=allow_duplicate,
            )
            self._conn.execute("COMMIT")
            if outcome:
                return outcome
            return {
                "outcome": "needs_job",
                "file_id": existing["id"] if existing else "",
            }
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def activate_publish_attempt(
        self,
        *,
        post_id: str,
        file_id: str,
        upload_job_id: str,
        filename: str,
        mime_type: str,
        size_bytes: int,
        sha256: str,
        storage_provider: str,
        actor_id: str,
        allow_duplicate: bool = False,
    ) -> dict[str, Any]:
        """Atomically link file/job and move the post to PUBLISHING.

        The job already exists in non-claimable STAGING.  This transaction is therefore
        the single winner between publish, unpublish and runner completion; no observer
        can see PUBLISHING with an unlinked/nonexistent job created by this process.
        """
        now = time.time()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            outcome, existing = self._classify_publish_attempt(
                post_id=post_id,
                sha256=sha256,
                actor_id=actor_id,
                allow_duplicate=allow_duplicate,
            )
            if outcome:
                self._conn.execute("COMMIT")
                return outcome

            # ``file_id`` is embedded in the already-created STAGING job command.
            # If another process inserted a retryable row after the caller's lookup,
            # adopting that different id would make the job configuration and the
            # community link disagree.  Fail safely and let the caller retry from a
            # fresh snapshot.
            if existing and existing["id"] != file_id:
                self._conn.execute("ROLLBACK")
                return {"outcome": "conflict"}

            if existing:
                file_id = existing["id"]
                self._conn.execute(
                    "UPDATE community_files SET upload_job_id=?,upload_status=?,bytes_uploaded=0,"
                    "provider_checksum=NULL,verified_at=NULL,updated_at=? WHERE id=?",
                    (upload_job_id, FileStatus.PENDING, now, file_id),
                )
            else:
                self._conn.execute(
                    "INSERT INTO community_files(id,post_id,storage_provider,filename,mime_type,"
                    "size_bytes,sha256,upload_job_id,upload_status,bytes_uploaded,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,0,?,?)",
                    (
                        file_id, post_id, storage_provider, filename, mime_type,
                        int(size_bytes), sha256, upload_job_id, FileStatus.PENDING, now, now,
                    ),
                )
            self._conn.execute(
                "UPDATE community_posts SET status=?,updated_at=? WHERE id=?",
                (PostStatus.PUBLISHING, now, post_id),
            )
            self._insert_event(post_id, actor_id, "status_publishing", {}, now)
            self._insert_event(
                post_id, actor_id, "publish_requested",
                {"file_id": file_id, "job_id": upload_job_id}, now,
            )
            self._conn.execute("COMMIT")
            return {
                "outcome": "reserved",
                "file_id": file_id,
                "job_id": upload_job_id,
            }
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def _classify_publish_attempt(
        self,
        *,
        post_id: str,
        sha256: str,
        actor_id: str,
        allow_duplicate: bool,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Classify while the caller owns a community DB write transaction."""
        now = time.time()
        post = self._row(self._conn.execute(
            "SELECT * FROM community_posts WHERE id=?", (post_id,)
        ).fetchone())
        if not post:
            return {"outcome": "missing"}, None
        if post["status"] in {PostStatus.BLOCKED, PostStatus.DELETED}:
            return {"outcome": "not_publishable"}, None

        if not allow_duplicate:
            duplicate = self.blocking_sha_exists(sha256, exclude_post=post_id)
            if duplicate:
                return {"outcome": "duplicate"}, None

        active = self._row(self._conn.execute(
            "SELECT f.* FROM community_files f WHERE f.post_id=? "
            "AND f.upload_status IN (?,?,?) ORDER BY f.created_at DESC,f.rowid DESC LIMIT 1",
            (post_id, FileStatus.PENDING, FileStatus.UPLOADING, FileStatus.VERIFYING),
        ).fetchone())
        if active:
            if (
                post["status"] == PostStatus.PUBLISHING
                and active["sha256"] == sha256
                and active.get("upload_job_id")
            ):
                return {
                    "outcome": "active",
                    "file_id": active["id"],
                    "job_id": active["upload_job_id"],
                }, active
            return {"outcome": "conflict"}, active

        existing = self._row(self._conn.execute(
            "SELECT * FROM community_files WHERE post_id=? AND sha256=? LIMIT 1",
            (post_id, sha256),
        ).fetchone())
        latest = self._row(self._conn.execute(
            "SELECT * FROM community_files WHERE post_id=? "
            "ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (post_id,),
        ).fetchone())
        if existing and latest and existing["id"] != latest["id"]:
            return {"outcome": "unavailable"}, existing
        if existing and existing["upload_status"] == FileStatus.VERIFIED:
            if post["status"] == PostStatus.UNPUBLISHED:
                self._conn.execute(
                    "UPDATE community_posts SET status=?,published_at=?,updated_at=? WHERE id=?",
                    (PostStatus.PUBLISHED, now, now, post_id),
                )
                self._insert_event(post_id, actor_id, "status_published", {}, now)
                self._insert_event(
                    post_id,
                    actor_id,
                    "republished_existing_file",
                    {"file_id": existing["id"]},
                    now,
                )
                return {
                    "outcome": "completed",
                    "file_id": existing["id"],
                    "job_id": existing.get("upload_job_id") or "",
                }, existing
            if post["status"] == PostStatus.PUBLISHED:
                return {
                    "outcome": "completed",
                    "file_id": existing["id"],
                    "job_id": existing.get("upload_job_id") or "",
                }, existing
            return {"outcome": "conflict"}, existing
        if existing and existing["upload_status"] in {
            FileStatus.DELETING, FileStatus.DELETED,
        }:
            return {"outcome": "unavailable"}, existing
        return None, existing

    def get_file(self, file_id: str) -> dict[str, Any] | None:
        return self._row(self._conn.execute(
            "SELECT * FROM community_files WHERE id=?", (file_id,)).fetchone())

    def file_for_post(self, post_id: str) -> dict[str, Any] | None:
        return self._row(self._conn.execute(
            "SELECT * FROM community_files WHERE post_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (post_id,)).fetchone())

    def file_for_post_sha(self, post_id: str, sha256: str) -> dict[str, Any] | None:
        return self._row(self._conn.execute(
            "SELECT * FROM community_files WHERE post_id=? AND sha256=? LIMIT 1",
            (post_id, sha256),
        ).fetchone())

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

    def update_current_publish_file(
        self,
        post_id: str,
        file_id: str,
        upload_job_id: str,
        **fields: Any,
    ) -> bool:
        """Update upload metadata only while this remains the live publish attempt."""
        allowed = {
            "storage_file_id",
            "storage_folder_id",
            "provider_checksum",
            "upload_status",
            "bytes_uploaded",
            "session_ref",
            "verified_at",
        }
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown publish file columns: {bad}")
        if not fields:
            return True
        now = time.time()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._publish_attempt_row(post_id, file_id)
            valid = bool(
                row
                and row["post_status"] == PostStatus.PUBLISHING
                and row["file_id"] == row["latest_file_id"]
                and row["upload_job_id"] == upload_job_id
                and row["upload_status"] in {
                    FileStatus.PENDING,
                    FileStatus.UPLOADING,
                    FileStatus.VERIFYING,
                }
            )
            if not valid:
                self._conn.execute("ROLLBACK")
                return False
            fields["updated_at"] = now
            cols = ", ".join(f"{key}=?" for key in fields)
            self._conn.execute(
                f"UPDATE community_files SET {cols} WHERE id=?",
                (*fields.values(), file_id),
            )
            self._conn.execute("COMMIT")
            return True
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def record_publish_remote_reference(
        self,
        file_id: str,
        upload_job_id: str,
        storage_file_id: str,
    ) -> None:
        """Keep a remote id traceable without changing an invalidated file's status."""
        if not storage_file_id:
            return
        self._conn.execute(
            "UPDATE community_files SET storage_file_id=?,updated_at=? "
            "WHERE id=? AND upload_job_id=?",
            (storage_file_id, time.time(), file_id, upload_job_id),
        )

    def publish_attempt_is_current(
        self,
        post_id: str,
        file_id: str,
        upload_job_id: str,
    ) -> bool:
        return self.publish_attempt_state(post_id, file_id, upload_job_id) == "active"

    def publish_attempt_state(
        self,
        post_id: str,
        file_id: str,
        upload_job_id: str,
    ) -> str:
        """Classify the exact linked attempt for cross-database crash recovery."""
        row = self._publish_attempt_row(post_id, file_id)
        if not (
            row
            and row["file_id"] == row["latest_file_id"]
            and row["upload_job_id"] == upload_job_id
        ):
            return "invalid"
        if (
            row["post_status"] == PostStatus.PUBLISHING
            and row["upload_status"] in {
                FileStatus.PENDING,
                FileStatus.UPLOADING,
                FileStatus.VERIFYING,
            }
        ):
            return "active"
        if (
            row["post_status"] == PostStatus.PUBLISHED
            and row["upload_status"] == FileStatus.VERIFIED
            and row["storage_file_id"]
        ):
            return "completed"
        if (
            row["post_status"] == PostStatus.FAILED
            and row["upload_status"] == FileStatus.FAILED
        ):
            return "failed"
        return "invalid"

    def complete_publish_attempt(
        self,
        *,
        post_id: str,
        file_id: str,
        upload_job_id: str,
        provider_checksum: str,
        actor_id: str,
        size: int,
    ) -> bool:
        """Atomically verify the current file and publish only its still-live post."""
        now = time.time()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._publish_attempt_row(post_id, file_id)
            valid = bool(
                row
                and row["post_status"] == PostStatus.PUBLISHING
                and row["file_id"] == row["latest_file_id"]
                and row["upload_job_id"] == upload_job_id
                and row["upload_status"] == FileStatus.VERIFYING
            )
            if not valid:
                self._conn.execute("ROLLBACK")
                return False
            self._conn.execute(
                "UPDATE community_files SET upload_status=?,provider_checksum=?,"
                "verified_at=?,updated_at=? WHERE id=?",
                (FileStatus.VERIFIED, provider_checksum, now, now, file_id),
            )
            self._conn.execute(
                "UPDATE community_posts SET status=?,published_at=?,updated_at=? WHERE id=?",
                (PostStatus.PUBLISHED, now, now, post_id),
            )
            self._insert_event(post_id, actor_id, "status_published", {}, now)
            self._insert_event(post_id, actor_id, "published", {
                "file_id": file_id,
                "size": int(size),
            }, now)
            self._conn.execute("COMMIT")
            return True
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def fail_publish_attempt(
        self,
        *,
        post_id: str,
        file_id: str,
        upload_job_id: str,
        actor_id: str,
        reason: str,
    ) -> bool:
        """Fail only the still-current attempt; never overwrite a later unpublish."""
        now = time.time()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._publish_attempt_row(post_id, file_id)
            valid = bool(
                row
                and row["post_status"] == PostStatus.PUBLISHING
                and row["file_id"] == row["latest_file_id"]
                and row["upload_job_id"] == upload_job_id
                and row["upload_status"] in {
                    FileStatus.PENDING,
                    FileStatus.UPLOADING,
                    FileStatus.VERIFYING,
                }
            )
            if not valid:
                self._conn.execute("ROLLBACK")
                return False
            self._conn.execute(
                "UPDATE community_files SET upload_status=?,updated_at=? WHERE id=?",
                (FileStatus.FAILED, now, file_id),
            )
            self._conn.execute(
                "UPDATE community_posts SET status=?,updated_at=? WHERE id=?",
                (PostStatus.FAILED, now, post_id),
            )
            self._insert_event(post_id, actor_id, "status_failed", {}, now)
            self._insert_event(post_id, actor_id, "publish_failed", {
                "reason": str(reason),
            }, now)
            self._conn.execute("COMMIT")
            return True
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def invalidate_publish_file(self, file_id: str, upload_job_id: str) -> None:
        self._conn.execute(
            "UPDATE community_files SET upload_status=?,updated_at=? "
            "WHERE id=? AND upload_job_id=? AND upload_status IN (?,?,?)",
            (
                FileStatus.FAILED,
                time.time(),
                file_id,
                upload_job_id,
                FileStatus.PENDING,
                FileStatus.UPLOADING,
                FileStatus.VERIFYING,
            ),
        )

    def _publish_attempt_row(self, post_id: str, file_id: str):
        return self._conn.execute(
            "SELECT p.status AS post_status,f.id AS file_id,f.upload_job_id,"
            "f.upload_status,f.storage_file_id,(SELECT latest.id FROM community_files latest "
            "WHERE latest.post_id=p.id ORDER BY latest.created_at DESC,latest.rowid DESC "
            "LIMIT 1) AS latest_file_id FROM community_posts p "
            "JOIN community_files f ON f.post_id=p.id WHERE p.id=? AND f.id=?",
            (post_id, file_id),
        ).fetchone()

    def _insert_event(
        self,
        post_id: str,
        actor_id: str,
        event_type: str,
        metadata: dict,
        created_at: float,
    ) -> None:
        self._conn.execute(
            "INSERT INTO community_events(id,post_id,actor_id,event_type,metadata_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                post_id,
                actor_id,
                event_type,
                json.dumps(metadata, ensure_ascii=False),
                created_at,
            ),
        )

    # ---- events -------------------------------------------------------------
    def add_event(self, post_id: str, actor_id: str, event_type: str, metadata: dict) -> None:
        self._conn.execute(
            "INSERT INTO community_events(id,post_id,actor_id,event_type,metadata_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (uuid.uuid4().hex, post_id, actor_id, event_type,
             json.dumps(metadata, ensure_ascii=False), time.time()))

    def add_admin_audit_event(
        self,
        *,
        actor_id: str,
        event_type: str,
        metadata: dict[str, Any],
    ) -> None:
        """Persist an administrative audit event without tying it to a post.

        The event table is append-only from the service perspective.  Sensitive
        credentials are never accepted by this helper; callers provide only the
        sanitized identifiers and outcome metadata needed for reconciliation.
        """
        self._conn.execute(
            "INSERT INTO community_events(id,post_id,actor_id,event_type,metadata_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (uuid.uuid4().hex, None, str(actor_id or ""), str(event_type or ""),
             json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), time.time()),
        )

    def events_for_post(self, post_id: str) -> list[dict[str, Any]]:
        return self._rows(self._conn.execute(
            "SELECT * FROM community_events WHERE post_id=? ORDER BY created_at", (post_id,)))
