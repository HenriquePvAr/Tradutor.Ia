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

import json
import sqlite3
import time
import uuid
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
        # Retention: a replaced/unlinked asset is never deleted — it is retained (with a
        # deadline) so the owner can restore it, and only a later sweep may move it to the
        # Drive trash. Additive tables; timestamps are epoch seconds (UTC).
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS social_asset_retention (
                id TEXT PRIMARY KEY,
                chapter_id TEXT NOT NULL,
                publication_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                state TEXT NOT NULL,
                previous_state TEXT,
                retained_at REAL NOT NULL,
                retain_until REAL NOT NULL,
                trashed_at REAL,
                restored_at REAL,
                last_attempt_at REAL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error_code TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                version INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        # At most one *live* retention per (chapter, publication): repeated replace/unlink
        # must not pile up duplicate retentions for the same asset.
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sar_live "
            "ON social_asset_retention(chapter_id, publication_id) "
            "WHERE state IN ('retained','pending_trash','reconcile_required')"
        )
        for col in ("state", "retain_until", "publication_id", "chapter_id", "owner_id"):
            self._conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_sar_{col} ON social_asset_retention({col})")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS social_asset_audit_log (
                id TEXT PRIMARY KEY,
                retention_id TEXT,
                chapter_id TEXT,
                publication_id TEXT,
                owner_id TEXT,
                actor_id TEXT,
                action TEXT NOT NULL,
                from_state TEXT,
                to_state TEXT,
                reason TEXT,
                metadata_json TEXT,
                created_at REAL NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_saal_retention ON social_asset_audit_log(retention_id)")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS social_asset_reconcile_runs (
                id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                started_at REAL NOT NULL,
                finished_at REAL,
                status TEXT NOT NULL,
                scanned_count INTEGER NOT NULL DEFAULT 0,
                safe_count INTEGER NOT NULL DEFAULT 0,
                ambiguous_count INTEGER NOT NULL DEFAULT 0,
                changed_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                summary_json TEXT
            )
            """
        )
        # Publish intents make the async upload idempotent: a duplicate click/retry finds
        # the same in-flight publication instead of starting a second upload.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS social_publish_intents (
                chapter_id TEXT PRIMARY KEY,
                publication_id TEXT NOT NULL,
                target_status TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                source_job_id TEXT NOT NULL,
                idempotency_key TEXT,
                created_at REAL NOT NULL
            )
            """
        )

    # ---- publish intents (idempotency for the async upload) ------------------
    def get_intent(self, chapter_id: str, owner_id: str) -> dict[str, Any] | None:
        r = self._conn.execute(
            "SELECT * FROM social_publish_intents WHERE chapter_id=? AND owner_id=?",
            (chapter_id, owner_id)).fetchone()
        return dict(r) if r else None

    def set_intent(self, chapter_id: str, publication_id: str, target_status: str,
                   owner_id: str, source_job_id: str, idempotency_key: str = "") -> None:
        self._conn.execute(
            "INSERT INTO social_publish_intents(chapter_id,publication_id,target_status,"
            "owner_id,source_job_id,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(chapter_id) DO UPDATE SET publication_id=excluded.publication_id,"
            "target_status=excluded.target_status,source_job_id=excluded.source_job_id,"
            "idempotency_key=excluded.idempotency_key,created_at=excluded.created_at",
            (chapter_id, publication_id, target_status, owner_id, source_job_id,
             idempotency_key, time.time()))

    def clear_intent(self, chapter_id: str, owner_id: str) -> None:
        self._conn.execute(
            "DELETE FROM social_publish_intents WHERE chapter_id=? AND owner_id=?",
            (chapter_id, owner_id))

    # ---- retention lifecycle -------------------------------------------------
    LIVE_STATES = ("retained", "pending_trash", "reconcile_required")

    def create_retention(self, *, chapter_id: str, publication_id: str, owner_id: str,
                         reason: str, retain_until: float, previous_state: str = "active") -> str:
        """Idempotent: a live retention for the same (chapter, publication) is reused."""
        existing = self.get_live_retention(chapter_id, publication_id)
        if existing:
            return existing["id"]
        now = time.time()
        rid = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO social_asset_retention(id,chapter_id,publication_id,owner_id,reason,"
            "state,previous_state,retained_at,retain_until,created_at,updated_at,version) "
            "VALUES(?,?,?,?,?,'retained',?,?,?,?,?,1)",
            (rid, chapter_id, publication_id, owner_id, reason, previous_state,
             now, retain_until, now, now))
        self.audit(action="retain", retention_id=rid, chapter_id=chapter_id,
                   publication_id=publication_id, owner_id=owner_id, actor_id=owner_id,
                   from_state=previous_state, to_state="retained", reason=reason)
        return rid

    def get_live_retention(self, chapter_id: str, publication_id: str) -> dict[str, Any] | None:
        marks = ",".join("?" for _ in self.LIVE_STATES)
        r = self._conn.execute(
            f"SELECT * FROM social_asset_retention WHERE chapter_id=? AND publication_id=? "
            f"AND state IN ({marks})", (chapter_id, publication_id, *self.LIVE_STATES)).fetchone()
        return dict(r) if r else None

    def get_retention(self, retention_id: str) -> dict[str, Any] | None:
        r = self._conn.execute(
            "SELECT * FROM social_asset_retention WHERE id=?", (retention_id,)).fetchone()
        return dict(r) if r else None

    def latest_retention_for_chapter(self, chapter_id: str, owner_id: str) -> dict[str, Any] | None:
        r = self._conn.execute(
            "SELECT * FROM social_asset_retention WHERE chapter_id=? AND owner_id=? "
            "ORDER BY retained_at DESC LIMIT 1", (chapter_id, owner_id)).fetchone()
        return dict(r) if r else None

    def list_owner_retentions(self, owner_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        marks = ",".join("?" for _ in self.LIVE_STATES)
        rows = self._conn.execute(
            f"SELECT * FROM social_asset_retention WHERE owner_id=? AND state IN ({marks}) "
            f"ORDER BY retained_at DESC LIMIT ?",
            (owner_id, *self.LIVE_STATES, int(limit))).fetchall()
        return [dict(r) for r in rows]

    # Allowed retention transitions; anything else is rejected (fail-closed) even when a
    # caller bypasses the service layer.
    TRANSITIONS = {
        "retained": {"pending_trash", "restored", "reconcile_required", "failed"},
        "pending_trash": {"trashed", "failed", "retained", "reconcile_required"},
        "failed": {"pending_trash", "trashed", "reconcile_required", "retained"},
        "reconcile_required": {"retained", "ignored", "pending_trash"},
        "trashed": {"restored"},
        "restored": set(),
        "ignored": set(),
    }

    def list_retentions_for_reconcile(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Every non-terminal retention, all owners — reconciliation scans app records only."""
        rows = self._conn.execute(
            "SELECT * FROM social_asset_retention WHERE state NOT IN ('restored','ignored') "
            "ORDER BY retained_at ASC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def list_due_retentions(self, now: float, *, limit: int = 100) -> list[dict[str, Any]]:
        """Retentions whose deadline has passed and that are still awaiting a decision."""
        rows = self._conn.execute(
            "SELECT * FROM social_asset_retention WHERE state IN ('retained','pending_trash','failed') "
            "AND retain_until <= ? ORDER BY retain_until ASC LIMIT ?", (now, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def transition_retention(self, retention_id: str, *, to_state: str, expected_version: int,
                             actor_id: str = "system", reason: str = "", **fields: Any) -> bool:
        """Optimistic-locked state change; a stale writer loses and must re-read."""
        current = self.get_retention(retention_id)
        if current and to_state not in self.TRANSITIONS.get(current["state"], set()):
            return False
        if not current or current["version"] != expected_version:
            return False
        allowed = {"trashed_at", "restored_at", "last_attempt_at", "attempt_count", "last_error_code"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown retention columns: {bad}")
        sets = ["state=?", "previous_state=?", "updated_at=?", "version=version+1"]
        params: list[Any] = [to_state, current["state"], time.time()]
        for k, v in fields.items():
            sets.append(f"{k}=?"); params.append(v)
        params += [retention_id, expected_version]
        cur = self._conn.execute(
            f"UPDATE social_asset_retention SET {', '.join(sets)} WHERE id=? AND version=?", params)
        if cur.rowcount != 1:
            return False
        self.audit(action=f"transition:{to_state}", retention_id=retention_id,
                   chapter_id=current["chapter_id"], publication_id=current["publication_id"],
                   owner_id=current["owner_id"], actor_id=actor_id,
                   from_state=current["state"], to_state=to_state, reason=reason)
        return True

    def current_publication_id(self, chapter_id: str, owner_id: str) -> str:
        """Server-side only: the publication currently linked (for retention bookkeeping)."""
        row = self._row(chapter_id)
        if not row or row["owner_id"] != owner_id or row.get("deleted_at") is not None:
            return ""
        return str(row["publication_id"])

    def publication_storage_id_for_retention(self, retention: dict[str, Any]) -> str:
        """Resolve the Drive id server-side from the retained publication. Never exposed."""
        return self._publication_storage_id(retention["publication_id"], retention["owner_id"])

    def publication_reference_count(self, publication_id: str) -> int:
        """Every known live reference to a publication: active links and in-flight uploads."""
        links = self._conn.execute(
            "SELECT COUNT(*) c FROM social_chapter_assets WHERE publication_id=? "
            "AND deleted_at IS NULL", (publication_id,)).fetchone()["c"]
        intents = self._conn.execute(
            "SELECT COUNT(*) c FROM social_publish_intents WHERE publication_id=?",
            (publication_id,)).fetchone()["c"]
        return int(links) + int(intents)

    def other_live_retentions(self, publication_id: str, exclude_id: str) -> int:
        marks = ",".join("?" for _ in self.LIVE_STATES)
        return int(self._conn.execute(
            f"SELECT COUNT(*) c FROM social_asset_retention WHERE publication_id=? AND id<>? "
            f"AND state IN ({marks})",
            (publication_id, exclude_id, *self.LIVE_STATES)).fetchone()["c"])

    def audit(self, *, action: str, retention_id: str = "", chapter_id: str = "",
              publication_id: str = "", owner_id: str = "", actor_id: str = "",
              from_state: str = "", to_state: str = "", reason: str = "",
              metadata: dict | None = None) -> None:
        """Sanitized audit trail — never stores tokens, Drive ids, paths or file contents."""
        self._conn.execute(
            "INSERT INTO social_asset_audit_log(id,retention_id,chapter_id,publication_id,"
            "owner_id,actor_id,action,from_state,to_state,reason,metadata_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, retention_id, chapter_id, publication_id, owner_id, actor_id,
             action, from_state, to_state, reason,
             json.dumps(metadata or {}, ensure_ascii=False), time.time()))

    def list_audit(self, retention_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM social_asset_audit_log WHERE retention_id=? ORDER BY created_at",
            (retention_id,)).fetchall()
        return [dict(r) for r in rows]

    def start_reconcile_run(self, mode: str) -> str:
        rid = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO social_asset_reconcile_runs(id,mode,started_at,status) VALUES(?,?,?,?)",
            (rid, mode, time.time(), "running"))
        return rid

    def finish_reconcile_run(self, run_id: str, *, status: str, counts: dict[str, int],
                             summary: dict | None = None) -> None:
        self._conn.execute(
            "UPDATE social_asset_reconcile_runs SET finished_at=?,status=?,scanned_count=?,"
            "safe_count=?,ambiguous_count=?,changed_count=?,failed_count=?,summary_json=? WHERE id=?",
            (time.time(), status, counts.get("scanned", 0), counts.get("safe", 0),
             counts.get("ambiguous", 0), counts.get("changed", 0), counts.get("failed", 0),
             json.dumps(summary or {}, ensure_ascii=False), run_id))

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
