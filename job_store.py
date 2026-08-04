"""Persistent SQLite job store — the single source of truth for job state.

The UI, the worker and each runner are separate OS processes. They coordinate only
through this database, so a job survives the UI (or the shell that launched it) being
closed. Output folders remain the source of truth for the chapter artifacts; this store
owns queue position, status, progress, heartbeat, cancellation and recovery.

Pure standard library (``sqlite3``). WAL mode lets the readers (UI) and the single
writer (worker/runner) work concurrently without a global lock.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 9
_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")


class JobStatus:
    STAGING = "staging"
    QUEUED = "queued"
    CLAIMING = "claiming"
    STARTING = "starting"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    RESUMABLE = "resumable"
    FAILED = "failed"
    FINISHED = "finished"
    REVIEW_REQUIRED = "review_required"
    AWAITING_SOURCE_REVIEW = "awaiting_source_review"
    SOURCE_ANALYSIS_READY = "source_analysis_ready"

    ALL = frozenset(
        {
            STAGING, QUEUED, CLAIMING, STARTING, RUNNING, CANCELLING, CANCELLED,
            INTERRUPTED, RESUMABLE, FAILED, FINISHED, REVIEW_REQUIRED,
            AWAITING_SOURCE_REVIEW, SOURCE_ANALYSIS_READY,
        }
    )
    TERMINAL = frozenset({CANCELLED, FAILED, FINISHED, REVIEW_REQUIRED})
    # A job the worker considers "in flight" and must heartbeat.
    IN_FLIGHT = frozenset({CLAIMING, STARTING, RUNNING, CANCELLING})


_DEFAULT_TERMINAL_REASONS = {
    JobStatus.CANCELLED: "cancelled",
    JobStatus.FAILED: "pipeline_failed",
    JobStatus.FINISHED: "completed",
    JobStatus.REVIEW_REQUIRED: "quality_review_required",
    JobStatus.INTERRUPTED: "interrupted",
}


# Allowed transitions. A transition not listed here is rejected (fail-closed), so a
# stray write can never move a job into a nonsensical state from another module.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    JobStatus.STAGING: frozenset({
        JobStatus.QUEUED, JobStatus.AWAITING_SOURCE_REVIEW, JobStatus.CANCELLED, JobStatus.FAILED,
    }),
    JobStatus.QUEUED: frozenset({JobStatus.CLAIMING, JobStatus.CANCELLED}),
    JobStatus.CLAIMING: frozenset(
        # The worker analyses a URL source while holding the claim, so a medium-confidence
        # result has to reach review from here. Additive: nothing previously allowed was
        # removed, and a claimed job still cannot jump straight to a running state.
        {JobStatus.STARTING, JobStatus.QUEUED, JobStatus.FAILED, JobStatus.INTERRUPTED,
         JobStatus.AWAITING_SOURCE_REVIEW, JobStatus.CANCELLING}
         | {JobStatus.SOURCE_ANALYSIS_READY}
    ),
    JobStatus.STARTING: frozenset(
        {JobStatus.RUNNING, JobStatus.INTERRUPTED, JobStatus.FAILED, JobStatus.CANCELLING}
    ),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.FINISHED, JobStatus.REVIEW_REQUIRED, JobStatus.CANCELLING,
            JobStatus.INTERRUPTED, JobStatus.FAILED,
        }
    ),
    JobStatus.AWAITING_SOURCE_REVIEW: frozenset(
        {JobStatus.QUEUED, JobStatus.CANCELLED, JobStatus.FAILED}
    ),
    JobStatus.SOURCE_ANALYSIS_READY: frozenset(
        {JobStatus.QUEUED, JobStatus.CANCELLED, JobStatus.FAILED}
    ),
    JobStatus.CANCELLING: frozenset({JobStatus.CANCELLED, JobStatus.INTERRUPTED, JobStatus.FAILED}),
    JobStatus.INTERRUPTED: frozenset({JobStatus.RESUMABLE, JobStatus.FAILED, JobStatus.CANCELLED}),
    JobStatus.RESUMABLE: frozenset({JobStatus.QUEUED, JobStatus.CANCELLED}),
    # Terminal states do not transition.
    JobStatus.CANCELLED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.FINISHED: frozenset(),
    JobStatus.REVIEW_REQUIRED: frozenset(),
}


def transition_allowed(current: str, target: str) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


class TransitionError(RuntimeError):
    """Raised when a status change is not a permitted transition."""


# Columns that make up a job row. Kept explicit so the schema and the row->dict mapping
# never drift. Timestamps are epoch seconds (REAL); NULL means "not yet".
_JOB_COLUMNS = (
    "id", "owner_id", "run_id", "operation_kind", "parent_job_id", "source_url",
    "series_title", "series_slug", "episode_number",
    "output_dir", "configuration_json", "command_json", "status", "stage",
    "progress_current", "progress_total", "progress_message", "progress_counter_stage",
    "source_type", "adapter_name", "adapter_version", "transport_name", "source_score",
    "candidate_count", "input_root_fingerprint",
    "snapshot_ref", "logical_pages", "input_count", "accepted_count",
    "rejected_count", "duplicate_count", "total_size_bytes",
    "created_at", "queued_at", "claimed_at", "started_at", "heartbeat_at", "finished_at",
    "worker_id", "worker_pid", "worker_create_time", "runner_pid", "runner_create_time",
    "exit_code",
    "cancel_requested", "interrupted_reason", "recoverable", "resume_from_stage",
    "cancellation_requested_at", "cancellation_completed_at",
    "attempt", "previous_job_id", "commit_hash", "branch",
    "manifest_path", "progress_path", "quality_report_path", "pdf_path", "log_path",
    "error_type", "error_message", "error_trace_path", "updated_at",
    "reason_code", "source_analysis_json", "source_selection_json",
    "review_actions_json", "review_confirmed_at", "stage_started_at",
)


class JobStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path), timeout=5.0, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    # ---- schema -------------------------------------------------------------
    def _migrate(self) -> None:
        cur = self._conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        cur.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        )
        row = cur.fetchone()
        version = int(row["value"]) if row else 0
        if version < 1:
            self._create_v1()
        if version < 2:
            self._migrate_v2()
        if version < 3:
            self._migrate_v3()
        if version < 4:
            self._migrate_v4()
        if version < 5:
            self._migrate_v5()
        if version < 6:
            self._migrate_v6()
        if version < 7:
            self._migrate_v7()
        if version < 8:
            self._migrate_v8()
        if version < 9:
            self._migrate_v9()
        self._backfill_additive_columns()
        # Idempotent: record the current version.
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )

    def _backfill_additive_columns(self) -> None:
        """Repair databases whose schema_version advanced before all columns existed.

        Some development branches introduced independent additive migrations with the
        same intermediate schema version.  A database may therefore already report the
        current version while still lacking one of those physical columns.  Re-running
        the idempotent additive migrations keeps opened stores usable without lowering
        or rewriting their version marker.
        """
        self._migrate_v2()
        self._migrate_v3()
        self._migrate_v4()
        self._migrate_v5()
        self._migrate_v6()
        self._migrate_v7()
        self._migrate_v8()
        self._migrate_v9()

    def _migrate_v9(self) -> None:
        """Persist child operations without turning them into chapter attempts."""
        job_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(jobs)")}
        if "operation_kind" not in job_cols:
            self._conn.execute(
                "ALTER TABLE jobs ADD COLUMN operation_kind TEXT NOT NULL DEFAULT 'chapter'"
            )
        if "parent_job_id" not in job_cols:
            self._conn.execute(
                "ALTER TABLE jobs ADD COLUMN parent_job_id TEXT NOT NULL DEFAULT ''"
            )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_parent_operation_created "
            "ON jobs(parent_job_id,operation_kind,created_at DESC)"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_active_review_rerun_parent "
            "ON jobs(parent_job_id) WHERE operation_kind='review_rerun' "
            "AND status IN ('queued','claiming','starting','running','cancelling')"
        )

    def _migrate_v8(self) -> None:
        """Append-only, optimistic review-item revisions."""
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS quality_review_item_revisions (
                revision_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                item_key TEXT NOT NULL,
                version INTEGER NOT NULL,
                action TEXT NOT NULL,
                translation TEXT NOT NULL DEFAULT '',
                reason_code TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                actor_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(job_id,item_key,version)
            );
            CREATE INDEX IF NOT EXISTS idx_quality_review_item_latest
                ON quality_review_item_revisions(job_id,item_key,version DESC);
            """
        )

    def _migrate_v7(self) -> None:
        """Materialize private ownership for fail-closed SQL-scoped reads."""
        job_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(jobs)")}
        if "owner_id" not in job_cols:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''")
        rows = self._conn.execute(
            "SELECT id,configuration_json FROM jobs WHERE owner_id=''"
        ).fetchall()
        for row in rows:
            try:
                config = json.loads(row["configuration_json"] or "{}")
            except (TypeError, ValueError):
                config = {}
            owner_id = str(
                config.get("community_owner_id") or ""
            ).strip() if isinstance(config, dict) else ""
            if owner_id:
                self._conn.execute(
                    "UPDATE jobs SET owner_id=? WHERE id=? AND owner_id=''",
                    (owner_id, row["id"]),
                )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_owner_created "
            "ON jobs(owner_id,created_at DESC)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_owner_status_created "
            "ON jobs(owner_id,status,created_at DESC)"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_retry_parent "
            "ON jobs(previous_job_id) WHERE previous_job_id IS NOT NULL "
            "AND previous_job_id != ''"
        )

    def _migrate_v2(self) -> None:
        # Additive: process start times let recovery tell a live runner from a reused PID,
        # and a stop flag lets a detached worker be stopped without a shared console.
        job_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(jobs)")}
        for column in ("worker_create_time", "runner_create_time"):
            if column not in job_cols:
                self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} REAL")
        # Additive: the stage a counter came from, so a download's 99/99 is never rendered
        # under a later stage's label.
        if "progress_counter_stage" not in job_cols:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN progress_counter_stage TEXT")
        worker_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(workers)")}
        if "create_time" not in worker_cols:
            self._conn.execute("ALTER TABLE workers ADD COLUMN create_time REAL")
        if "stop_requested" not in worker_cols:
            self._conn.execute("ALTER TABLE workers ADD COLUMN stop_requested INTEGER DEFAULT 0")

    def _migrate_v3(self) -> None:
        """Persist a sanitised source diagnosis without turning a review hold into a job run."""
        job_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(jobs)")}
        for column in ("reason_code", "source_analysis_json", "source_selection_json"):
            if column not in job_cols:
                self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")

    def _migrate_v4(self) -> None:
        """Additive: local-folder provenance on the job row.

        Text/integer only — never a filesystem path. The original folder and the snapshot
        location stay server-side; the row carries a fingerprint and an opaque reference.
        This is v4 rather than folded into v3: deployed v3 databases may contain either
        predecessor's additive columns, and must receive the other set on upgrade.
        """
        # Both merge parents used schema v3 for different additive columns.  A database
        # already marked v3 therefore still needs this idempotent backfill of the source
        # diagnosis columns before its local provenance columns are checked.
        self._migrate_v3()
        job_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(jobs)")}
        for column, kind in (
            ("source_type", "TEXT"), ("adapter_name", "TEXT"), ("adapter_version", "TEXT"),
            ("input_root_fingerprint", "TEXT"), ("snapshot_ref", "TEXT"),
            ("logical_pages", "INTEGER"), ("input_count", "INTEGER"),
            ("accepted_count", "INTEGER"), ("rejected_count", "INTEGER"),
            ("duplicate_count", "INTEGER"), ("total_size_bytes", "INTEGER"),
        ):
            if column not in job_cols:
                self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {kind}")

    def _migrate_v5(self) -> None:
        """Add scalar source-run provenance needed by queue/history views.

        The JSON source diagnosis remains the detailed audit record; these columns are the
        bounded values needed to filter/display a job without parsing or exposing it.
        """
        job_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(jobs)")}
        for column, kind in (
            ("transport_name", "TEXT"),
            ("source_score", "REAL"),
            ("candidate_count", "INTEGER"),
        ):
            if column not in job_cols:
                self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {kind}")

    def _migrate_v6(self) -> None:
        """Persist cancellation/review lifecycle and the active-stage clock."""
        job_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(jobs)")}
        for column, kind in (
            ("cancellation_requested_at", "REAL"),
            ("cancellation_completed_at", "REAL"),
            ("review_actions_json", "TEXT"),
            ("review_confirmed_at", "REAL"),
            ("stage_started_at", "REAL"),
        ):
            if column not in job_cols:
                self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {kind}")

    def _create_v1(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL DEFAULT '',
                run_id TEXT,
                operation_kind TEXT NOT NULL DEFAULT 'chapter',
                parent_job_id TEXT NOT NULL DEFAULT '',
                source_url TEXT,
                series_title TEXT,
                series_slug TEXT,
                episode_number TEXT,
                output_dir TEXT,
                configuration_json TEXT,
                command_json TEXT,
                status TEXT NOT NULL,
                stage TEXT,
                progress_current INTEGER DEFAULT 0,
                progress_total INTEGER DEFAULT 0,
                progress_message TEXT,
                progress_counter_stage TEXT,
                created_at REAL,
                queued_at REAL,
                claimed_at REAL,
                started_at REAL,
                heartbeat_at REAL,
                finished_at REAL,
                worker_id TEXT,
                worker_pid INTEGER,
                runner_pid INTEGER,
                exit_code INTEGER,
                cancel_requested INTEGER DEFAULT 0,
                cancellation_requested_at REAL,
                cancellation_completed_at REAL,
                interrupted_reason TEXT,
                recoverable INTEGER DEFAULT 0,
                resume_from_stage TEXT,
                attempt INTEGER DEFAULT 1,
                previous_job_id TEXT,
                commit_hash TEXT,
                branch TEXT,
                manifest_path TEXT,
                progress_path TEXT,
                quality_report_path TEXT,
                pdf_path TEXT,
                log_path TEXT,
                error_type TEXT,
                error_message TEXT,
                error_trace_path TEXT,
                reason_code TEXT,
                source_analysis_json TEXT,
                source_selection_json TEXT,
                review_actions_json TEXT,
                review_confirmed_at REAL,
                stage_started_at REAL,
                updated_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
            CREATE TABLE IF NOT EXISTS workers (
                worker_id TEXT PRIMARY KEY,
                pid INTEGER,
                started_at REAL,
                heartbeat_at REAL
            );
            """
        )

    # ---- helpers ------------------------------------------------------------
    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = {key: row[key] for key in row.keys()}
        if data.get("configuration_json"):
            try:
                data["configuration"] = json.loads(data["configuration_json"])
            except (ValueError, TypeError):
                data["configuration"] = {}
        else:
            data["configuration"] = {}
        if data.get("command_json"):
            try:
                data["command"] = json.loads(data["command_json"])
            except (ValueError, TypeError):
                data["command"] = []
        else:
            data["command"] = []
        for column, key in (("source_analysis_json", "source_analysis"),
                            ("source_selection_json", "source_selection")):
            if data.get(column):
                try:
                    data[key] = json.loads(data[column])
                except (ValueError, TypeError):
                    data[key] = {}
            else:
                data[key] = {}
        return data

    # ---- job CRUD -----------------------------------------------------------
    def create_job(
        self,
        *,
        job_id: str | None = None,
        source_url: str,
        output_dir: str,
        command: Iterable[str],
        run_id: str | None = None,
        configuration: dict[str, Any] | None = None,
        series_title: str = "",
        series_slug: str = "",
        episode_number: str = "",
        commit_hash: str = "",
        branch: str = "",
        previous_job_id: str = "",
        attempt: int = 1,
        resume_from_stage: str = "",
        initial_status: str = JobStatus.QUEUED,
        staging_owner_pid: int | None = None,
        staging_owner_create_time: float | None = None,
        operation_kind: str = "chapter",
        parent_job_id: str = "",
    ) -> str:
        job_id = str(job_id or uuid.uuid4().hex)
        if len(job_id) != 32 or any(char not in "0123456789abcdef" for char in job_id):
            raise ValueError("invalid_job_id")
        if initial_status not in {JobStatus.QUEUED, JobStatus.STAGING,
                                  JobStatus.AWAITING_SOURCE_REVIEW}:
            raise ValueError("invalid_initial_job_status")
        now = time.time()
        configuration = dict(configuration or {})
        owner_id = str(configuration.get("community_owner_id") or "").strip()
        operation_kind = str(operation_kind or "chapter").strip().casefold()
        if operation_kind not in {"chapter", "review_rerun", "community_publish"}:
            raise ValueError("invalid_operation_kind")
        parent_job_id = str(parent_job_id or "").strip()
        if operation_kind == "review_rerun" and not parent_job_id:
            raise ValueError("parent_job_required")
        self._conn.execute(
            """
            INSERT INTO jobs (
                id, owner_id, run_id, operation_kind, parent_job_id, source_url,
                series_title, series_slug, episode_number,
                output_dir, configuration_json, command_json, status, stage,
                progress_current, progress_total, created_at, queued_at, updated_at,
                worker_pid, worker_create_time,
                cancel_requested, recoverable, attempt, previous_job_id,
                resume_from_stage, commit_hash, branch
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,?,?,?,?,?,0,0,?,?,?,?,?)
            """,
            (
                job_id,
                owner_id,
                run_id or uuid.uuid4().hex,
                operation_kind,
                parent_job_id,
                source_url,
                series_title,
                series_slug,
                episode_number,
                output_dir,
                json.dumps(configuration, ensure_ascii=False),
                json.dumps(list(command), ensure_ascii=False),
                initial_status,
                "created",
                now,
                now if initial_status == JobStatus.QUEUED else None,
                now,
                int(staging_owner_pid) if staging_owner_pid is not None else None,
                staging_owner_create_time,
                int(attempt),
                previous_job_id,
                resume_from_stage,
                commit_hash,
                branch,
            ),
        )
        return job_id

    def active_review_rerun(self, parent_job_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE parent_job_id=? AND operation_kind='review_rerun' "
            "AND status IN ('queued','claiming','starting','running','cancelling') "
            "ORDER BY created_at DESC LIMIT 1",
            (str(parent_job_id or ""),),
        ).fetchone()
        return self._row_to_dict(row)

    def latest_review_rerun(self, parent_job_id: str) -> dict[str, Any] | None:
        """Return the newest child lifecycle, including a terminal child."""
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE parent_job_id=? AND operation_kind='review_rerun' "
            "ORDER BY created_at DESC LIMIT 1",
            (str(parent_job_id or ""),),
        ).fetchone()
        return self._row_to_dict(row)

    def queue_position(self, job_id: str) -> int | None:
        """One-based position for a queued job, or ``None`` once it leaves the queue."""
        row = self._conn.execute(
            "SELECT created_at FROM jobs WHERE id=? AND status=?",
            (str(job_id or ""), JobStatus.QUEUED),
        ).fetchone()
        if row is None:
            return None
        earlier = self._conn.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE status=? "
            "AND (created_at < ? OR (created_at = ? AND id <= ?))",
            (JobStatus.QUEUED, row["created_at"], row["created_at"], str(job_id)),
        ).fetchone()
        return int(earlier["count"] or 0) if earlier else 1

    def create_review_rerun(
        self,
        parent_job_id: str,
        *,
        targets: list[dict[str, Any]],
        allow_provider: bool,
        modes: list[str] | None = None,
        commit_hash: str = "",
        branch: str = "",
    ) -> dict[str, Any]:
        """Queue one idempotent child run for a chapter's explicit region targets."""
        parent = self.get_job(str(parent_job_id or ""))
        if not parent or not str(parent.get("output_dir") or ""):
            raise ValueError("parent_job_not_found")
        owner_id = str(parent.get("owner_id") or "").strip()
        if not owner_id:
            raise ValueError("parent_job_owner_required")
        normalized_targets: list[dict[str, Any]] = []
        for target in targets or []:
            region_id = str((target or {}).get("region_id") or "").strip()
            try:
                page = int((target or {}).get("page") or 0)
            except (TypeError, ValueError):
                page = 0
            if not region_id or page <= 0:
                raise ValueError("invalid_rerun_target")
            requires_provider = (target or {}).get("requires_provider") is True
            if requires_provider and allow_provider is not True:
                raise ValueError("provider_authorization_required")
            normalized_targets.append({
                "region_id": region_id,
                "page": page,
                "requires_provider": requires_provider,
                "work_kind": str((target or {}).get("work_kind") or "reconstruction_only")[:64],
                "reason_code": str((target or {}).get("reason_code") or "review_required")[:80],
                "translation_to_reuse": str(
                    (target or {}).get("translation_to_reuse") or ""
                )[:2000],
            })
        if not normalized_targets:
            raise ValueError("rerun_targets_required")
        existing = self.active_review_rerun(str(parent_job_id))
        if existing:
            return existing
        config = {
            "job_type": "review_rerun",
            "community_owner_id": owner_id,
            "parent_job_id": str(parent_job_id),
            "parent_run_id": str(parent.get("run_id") or ""),
            "targets": normalized_targets,
            "allow_provider": allow_provider is True,
            "modes": [str(value)[:48] for value in (modes or ["all_pending"])],
            "chapter_name": str(
                (parent.get("configuration") or {}).get("chapter_name")
                or parent.get("series_title") or ""
            )[:160],
            "full": False,
            "use_cache": True,
        }
        try:
            child_id = self.create_job(
                source_url="",
                output_dir=str(parent["output_dir"]),
                command=[],
                configuration=config,
                series_title=str(parent.get("series_title") or ""),
                series_slug=str(parent.get("series_slug") or ""),
                episode_number=str(parent.get("episode_number") or ""),
                commit_hash=commit_hash,
                branch=branch,
                operation_kind="review_rerun",
                parent_job_id=str(parent_job_id),
            )
        except sqlite3.IntegrityError:
            existing = self.active_review_rerun(str(parent_job_id))
            if existing:
                return existing
            raise
        return self.get_job(child_id) or {}

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row_to_dict(row)

    @staticmethod
    def _required_owner(owner_id: str) -> str:
        owner = str(owner_id or "").strip()
        if not owner or len(owner) > 128:
            raise ValueError("owner_required")
        return owner

    def get_job_for_owner(self, owner_id: str, job_id: str) -> dict[str, Any] | None:
        owner = self._required_owner(owner_id)
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE owner_id=? AND id=?",
            (owner, str(job_id or "")),
        ).fetchone()
        return self._row_to_dict(row)

    def retry_for_job(self, job_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE previous_job_id=? ORDER BY created_at ASC LIMIT 1",
            (str(job_id or ""),),
        ).fetchone()
        return self._row_to_dict(row)

    def list_jobs_for_owner(
        self,
        owner_id: str,
        *,
        statuses: Iterable[str] | None = None,
        limit: int | None = 200,
    ) -> list[dict[str, Any]]:
        owner = self._required_owner(owner_id)
        params: tuple[Any, ...] = (owner,)
        sql = "SELECT * FROM jobs WHERE owner_id=?"
        if statuses:
            values = tuple(statuses)
            placeholders = ",".join("?" for _ in values)
            sql += f" AND status IN ({placeholders})"
            params += values
        sql += " ORDER BY created_at DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params += (int(limit),)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_dict(row) for row in rows]  # type: ignore[misc]

    def list_jobs(
        self,
        *,
        statuses: Iterable[str] | None = None,
        limit: int | None = 200,
    ) -> list[dict[str, Any]]:
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            sql = (
                f"SELECT * FROM jobs WHERE status IN ({placeholders}) "
                "ORDER BY created_at DESC"
            )
            params: tuple[Any, ...] = tuple(statuses)
            if limit is not None:
                sql += " LIMIT ?"
                params += (int(limit),)
            rows = self._conn.execute(sql, params).fetchall()
        else:
            sql = "SELECT * FROM jobs ORDER BY created_at DESC"
            params = ()
            if limit is not None:
                sql += " LIMIT ?"
                params = (int(limit),)
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_dict(row) for row in rows]  # type: ignore[misc]

    def active_job(self) -> dict[str, Any] | None:
        placeholders = ",".join("?" for _ in JobStatus.IN_FLIGHT)
        row = self._conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({placeholders}) "
            "ORDER BY claimed_at DESC LIMIT 1",
            tuple(JobStatus.IN_FLIGHT),
        ).fetchone()
        return self._row_to_dict(row)

    def active_job_for_owner(self, owner_id: str) -> dict[str, Any] | None:
        owner = self._required_owner(owner_id)
        placeholders = ",".join("?" for _ in JobStatus.IN_FLIGHT)
        row = self._conn.execute(
            f"SELECT * FROM jobs WHERE owner_id=? AND status IN ({placeholders}) "
            "ORDER BY claimed_at DESC LIMIT 1",
            (owner, *tuple(JobStatus.IN_FLIGHT)),
        ).fetchone()
        return self._row_to_dict(row)

    # ---- atomic claim -------------------------------------------------------
    def claim_next_job(
        self,
        worker_id: str,
        worker_pid: int,
        worker_create_time: float | None = None,
    ) -> dict[str, Any] | None:
        """Atomically move the oldest queued job to ``claiming`` for this worker.

        The UPDATE is guarded by ``status='queued'`` so two workers racing for the same
        row cannot both win: SQLite serialises writers, and only the first UPDATE sees
        the row still queued.
        """
        now = time.time()
        cur = self._conn.execute(
            """
            UPDATE jobs SET status=?, worker_id=?, worker_pid=?, worker_create_time=?,
                   claimed_at=?, updated_at=?
            WHERE id = (
                SELECT id FROM jobs WHERE status=?
                AND (queued_at IS NULL OR queued_at<=?)
                ORDER BY created_at ASC LIMIT 1
            ) AND status=?
            """,
            (
                JobStatus.CLAIMING, worker_id, int(worker_pid), worker_create_time, now, now,
                JobStatus.QUEUED, now, JobStatus.QUEUED,
            ),
        )
        if cur.rowcount != 1:
            return None
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE worker_id=? AND status=? ORDER BY claimed_at DESC LIMIT 1",
            (worker_id, JobStatus.CLAIMING),
        ).fetchone()
        return self._row_to_dict(row)

    # ---- transitions --------------------------------------------------------
    def transition(
        self,
        job_id: str,
        target: str,
        *,
        expected_worker: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        if target not in JobStatus.ALL:
            raise TransitionError(f"unknown status: {target}")
        row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise TransitionError(f"unknown job: {job_id}")
        current = row["status"]
        if not transition_allowed(current, target):
            raise TransitionError(f"illegal transition {current} -> {target}")
        if expected_worker is not None and row["worker_id"] != expected_worker:
            raise TransitionError(
                f"job {job_id} owned by {row['worker_id']!r}, not {expected_worker!r}"
            )
        assignments = {"status": target, "updated_at": time.time()}
        if target == JobStatus.QUEUED and row["queued_at"] is None:
            assignments["queued_at"] = time.time()
        if target == JobStatus.RUNNING and row["started_at"] is None:
            assignments["started_at"] = time.time()
            assignments["stage_started_at"] = time.time()
        if target in JobStatus.TERMINAL:
            assignments["finished_at"] = time.time()
        if target == JobStatus.CANCELLED:
            assignments["cancellation_completed_at"] = time.time()
        if target in _DEFAULT_TERMINAL_REASONS and "reason_code" not in fields:
            fields["reason_code"] = _DEFAULT_TERMINAL_REASONS[target]
        if "reason_code" in fields:
            candidate = str(fields["reason_code"] or "").strip().casefold()
            fields["reason_code"] = (
                candidate if _REASON_CODE_RE.fullmatch(candidate)
                else _DEFAULT_TERMINAL_REASONS.get(target, "invalid_reason_code")
            )
        for key, value in fields.items():
            if key not in _JOB_COLUMNS:
                raise TransitionError(f"unknown job column: {key}")
            assignments[key] = value
        columns = ", ".join(f"{key}=?" for key in assignments)
        cur = self._conn.execute(
            f"UPDATE jobs SET {columns} WHERE id=? AND status=?",
            (*assignments.values(), job_id, current),
        )
        if cur.rowcount != 1:
            # Someone changed the row between the read and the write.
            raise TransitionError(f"job {job_id} changed concurrently during {current}->{target}")
        return self.get_job(job_id)  # type: ignore[return-value]

    def update_fields(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        for key in fields:
            if key not in _JOB_COLUMNS:
                raise TransitionError(f"unknown job column: {key}")
        fields["updated_at"] = time.time()
        columns = ", ".join(f"{key}=?" for key in fields)
        self._conn.execute(
            f"UPDATE jobs SET {columns} WHERE id=?", (*fields.values(), job_id)
        )

    def bind_community_owner(
        self,
        job_id: str,
        target_user_id: str,
        *,
        bound_by: str,
        bound_at: float | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        """Atomically claim an unowned translation job for community publication.

        Ownership is stored in the job configuration because it is optional metadata
        for older rows.  The transaction prevents two administrative requests from
        claiming the same legacy artifact concurrently; an existing owner is never
        overwritten.
        """
        now = time.time() if bound_at is None else float(bound_at)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id=?", (str(job_id),)
            ).fetchone()
            if row is None:
                self._conn.execute("ROLLBACK")
                return "not_found", None
            record = self._row_to_dict(row)
            config = record.get("configuration") or {}
            if not isinstance(config, dict):
                self._conn.execute("ROLLBACK")
                return "invalid_configuration", record
            current = str(config.get("community_owner_id") or "")
            if current:
                self._conn.execute("ROLLBACK")
                return (
                    "already_bound_to_target" if current == str(target_user_id)
                    else "owner_already_assigned",
                    record,
                )
            config = dict(config)
            config.update({
                "community_owner_id": str(target_user_id),
                "owner_bound_at": now,
                "owner_bound_by": str(bound_by),
            })
            self._conn.execute(
                "UPDATE jobs SET configuration_json=?,owner_id=?,updated_at=? WHERE id=?",
                (json.dumps(config, ensure_ascii=False), str(target_user_id), now, str(job_id)),
            )
            self._conn.execute("COMMIT")
            record["configuration"] = config
            record["configuration_json"] = json.dumps(config, ensure_ascii=False)
            record["owner_id"] = str(target_user_id)
            record["updated_at"] = now
            return "owner_bound", record
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def heartbeat(self, job_id: str, **fields: Any) -> None:
        self.update_fields(job_id, heartbeat_at=time.time(), **fields)

    def update_progress(
        self,
        job_id: str,
        *,
        stage: str | None = None,
        current: int | None = None,
        total: int | None = None,
        message: str | None = None,
        counter_stage: str | None = None,
    ) -> None:
        now = time.time()
        fields: dict[str, Any] = {"heartbeat_at": now}
        if stage is not None:
            current_row = self._conn.execute(
                "SELECT stage, stage_started_at FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if current_row is not None and current_row["stage"] != stage:
                fields["stage_started_at"] = now
            elif current_row is not None and current_row["stage_started_at"] is None:
                fields["stage_started_at"] = now
        if stage is not None:
            fields["stage"] = stage
        if current is not None:
            fields["progress_current"] = int(current)
        if total is not None:
            fields["progress_total"] = int(total)
        if message is not None:
            fields["progress_message"] = message
        if counter_stage is not None:
            fields["progress_counter_stage"] = counter_stage
        self.update_fields(job_id, **fields)

    def request_cancel(self, job_id: str) -> bool:
        row = self._conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or row["status"] in JobStatus.TERMINAL:
            return False
        self._conn.execute(
            "UPDATE jobs SET cancel_requested=1, cancellation_requested_at=?, updated_at=? WHERE id=?",
            (time.time(), time.time(), job_id),
        )
        return True

    def review_actions(self, job_id: str) -> dict[str, str]:
        row = self._conn.execute(
            "SELECT review_actions_json FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if not row or not row["review_actions_json"]:
            return {}
        try:
            value = json.loads(row["review_actions_json"])
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def review_item_revisions(self, job_id: str, item_key: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM quality_review_item_revisions "
            "WHERE job_id=? AND item_key=? ORDER BY version",
            (str(job_id), str(item_key)),
        ).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    def review_item_latest(self, job_id: str, item_key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM quality_review_item_revisions "
            "WHERE job_id=? AND item_key=? ORDER BY version DESC LIMIT 1",
            (str(job_id), str(item_key)),
        ).fetchone()
        return {key: row[key] for key in row.keys()} if row else None

    def record_review_item_revision(
        self, job_id: str, item_key: str, *, expected_version: int,
        action: str, translation: str, reason_code: str,
        reason: str, actor_id: str,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,180}", str(item_key or "")):
            raise TransitionError("invalid_review_item")
        allowed = {
            "edited", "reviewed", "rejected", "preserved_original", "manual_review",
        }
        if action not in allowed:
            raise TransitionError("invalid_review_action")
        with self._conn:
            latest = self.review_item_latest(job_id, item_key)
            current = int((latest or {}).get("version") or 0)
            if int(expected_version) != current:
                raise TransitionError("review_version_conflict")
            normalized_reason = str(reason)[:500]
            if latest and all((
                str(latest.get("action") or "") == str(action),
                str(latest.get("translation") or "") == str(translation),
                str(latest.get("reason_code") or "") == str(reason_code),
                str(latest.get("reason") or "") == normalized_reason,
                str(latest.get("actor_id") or "") == str(actor_id),
            )):
                return latest
            version = current + 1
            revision_id = uuid.uuid4().hex
            self._conn.execute(
                "INSERT INTO quality_review_item_revisions("
                "revision_id,job_id,item_key,version,action,translation,"
                "reason_code,reason,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    revision_id, str(job_id), str(item_key), version, action,
                    str(translation), str(reason_code), normalized_reason,
                    str(actor_id), time.time(),
                ),
            )
        return self.review_item_latest(job_id, item_key) or {}

    def record_review_action(self, job_id: str, item_key: str, action: str) -> dict[str, str]:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,180}", str(item_key or "")):
            raise TransitionError("invalid_review_item")
        if action not in {"reviewed", "preserved_original"}:
            raise TransitionError("invalid_review_action")
        actions = self.review_actions(job_id)
        actions[str(item_key)] = action
        self.update_fields(job_id, review_actions_json=json.dumps(actions, ensure_ascii=False))
        return actions

    def record_review_actions_bulk(self, job_id: str, updates: dict[str, str]) -> dict[str, str]:
        if not updates:
            return self.review_actions(job_id)
        for item_key, action in updates.items():
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,180}", str(item_key or "")):
                raise TransitionError("invalid_review_item")
            if action not in {"reviewed", "preserved_original", "pending"}:
                raise TransitionError("invalid_review_action")
        actions = self.review_actions(job_id)
        for item_key, action in updates.items():
            if action == "pending":
                actions.pop(str(item_key), None)
            else:
                actions[str(item_key)] = action
        now = time.time()
        payload = json.dumps(actions, ensure_ascii=False)
        with self._conn:
            row = self._conn.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise TransitionError("unknown job")
            self._conn.execute(
                "UPDATE jobs SET review_actions_json=?, updated_at=? WHERE id=?",
                (payload, now, job_id),
            )
        return actions

    def confirm_review(self, job_id: str) -> dict[str, str]:
        self.complete_review(job_id)
        return self.review_actions(job_id)

    def complete_review(self, job_id: str) -> dict[str, Any]:
        """Commit the human review decision as an operationally terminal outcome.

        The quality report remains the immutable record of the automated gate.  The job
        status, however, must stop looking active/review-blocked after the user has
        explicitly confirmed every item.  Repeating the operation is intentionally
        idempotent and never rewrites the original completion timestamp.
        """
        row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise TransitionError(f"unknown job: {job_id}")
        now = time.time()
        if row["status"] == JobStatus.REVIEW_REQUIRED:
            fields = {
                "status": JobStatus.FINISHED,
                "stage": "review_completed",
                "reason_code": "quality_review_completed",
                "review_confirmed_at": row["review_confirmed_at"] or now,
                "finished_at": row["finished_at"] or now,
                "worker_id": None,
                "worker_pid": None,
                "worker_create_time": None,
                "runner_pid": None,
                "runner_create_time": None,
                "cancel_requested": 0,
                "updated_at": now,
            }
            columns = ", ".join(f"{key}=?" for key in fields)
            self._conn.execute(
                f"UPDATE jobs SET {columns} WHERE id=? AND status=?",
                (*fields.values(), job_id, JobStatus.REVIEW_REQUIRED),
            )
        elif row["status"] == JobStatus.FINISHED and row["review_confirmed_at"]:
            # Already completed: do not alter timestamps or reopen the quality review.
            return self.get_job(job_id) or {}
        else:
            raise TransitionError("quality_review_not_confirmable")
        return self.get_job(job_id) or {}

    def reconcile_confirmed_reviews(self) -> list[str]:
        """Repair rows from the old timestamp-only confirmation implementation."""
        rows = self._conn.execute(
            "SELECT id FROM jobs WHERE status=? AND review_confirmed_at IS NOT NULL",
            (JobStatus.REVIEW_REQUIRED,),
        ).fetchall()
        repaired: list[str] = []
        for row in rows:
            try:
                self.complete_review(str(row["id"]))
            except TransitionError:
                continue
            repaired.append(str(row["id"]))
        return repaired

    def cancel_requested(self, job_id: str) -> bool:
        row = self._conn.execute(
            "SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        return bool(row and row["cancel_requested"])

    # ---- recovery -----------------------------------------------------------
    def recover_stale(self, *, stale_seconds: float = 30.0) -> list[str]:
        """Mark in-flight jobs whose heartbeat went stale as interrupted.

        Called on worker startup and periodically. A job is stale when it claims to be
        in flight but has not been heartbeated within ``stale_seconds`` — its runner (or
        the worker that owned it) died without a terminal transition.
        """
        cutoff = time.time() - stale_seconds
        placeholders = ",".join("?" for _ in JobStatus.IN_FLIGHT)
        rows = self._conn.execute(
            f"SELECT id, status, heartbeat_at, created_at FROM jobs "
            f"WHERE status IN ({placeholders})",
            tuple(JobStatus.IN_FLIGHT),
        ).fetchall()
        recovered: list[str] = []
        for row in rows:
            reference = row["heartbeat_at"]
            if reference is None:
                reference = row["created_at"]
            if reference is None:
                reference = 0
            if reference > cutoff:
                continue
            self.transition(
                row["id"],
                JobStatus.INTERRUPTED,
                interrupted_reason="stale_heartbeat",
                recoverable=1,
            )
            recovered.append(row["id"])
        return recovered

    def orphaned_in_flight_jobs(
        self, *, exclude_worker: str = "", worker_stale_seconds: float = 15.0
    ) -> list[dict[str, Any]]:
        """In-flight jobs whose owning worker is gone, for a new worker to reconcile.

        The job heartbeat is written by the runner, which can outlive a crashed worker
        (a stuck pipeline keeps heartbeating), so staleness of the job heartbeat is the
        wrong signal. A job is orphaned when the worker that owns it is no longer a live,
        heartbeating worker - then its runner, however lively, has nobody supervising it.
        This does not transition anything; the caller checks the runner process first.
        """
        cutoff = time.time() - worker_stale_seconds
        placeholders = ",".join("?" for _ in JobStatus.IN_FLIGHT)
        rows = self._conn.execute(
            f"""
            SELECT j.* FROM jobs j
            LEFT JOIN workers w ON j.worker_id = w.worker_id
            WHERE j.status IN ({placeholders})
              AND (j.worker_id IS NULL OR j.worker_id != ?
                   AND (w.worker_id IS NULL OR w.heartbeat_at IS NULL OR w.heartbeat_at < ?))
            """,
            (*JobStatus.IN_FLIGHT, exclude_worker or "", cutoff),
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]  # type: ignore[misc]

    def mark_resumable(self, job_id: str, *, resume_from_stage: str = "") -> dict[str, Any]:
        return self.transition(
            job_id,
            JobStatus.RESUMABLE,
            recoverable=1,
            resume_from_stage=resume_from_stage,
        )

    def reconcile_community_publish_terminal(self, job_id: str, target: str) -> dict[str, Any]:
        """Close a publish job after its other database already reached a terminal state."""
        if target not in {JobStatus.FINISHED, JobStatus.FAILED}:
            raise TransitionError("invalid community publish recovery target")
        row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise TransitionError(f"unknown job: {job_id}")
        try:
            config = json.loads(row["configuration_json"] or "{}")
        except (TypeError, ValueError):
            config = {}
        if not isinstance(config, dict) or config.get("job_type") != "community_publish":
            raise TransitionError("job is not a community publish")
        eligible = {
            JobStatus.CLAIMING,
            JobStatus.STARTING,
            JobStatus.RUNNING,
            JobStatus.CANCELLING,
            JobStatus.STAGING,
            JobStatus.INTERRUPTED,
            JobStatus.RESUMABLE,
        }
        if row["status"] not in eligible:
            raise TransitionError(
                f"cannot reconcile {row['status']} community publish to {target}"
            )
        now = time.time()
        cur = self._conn.execute(
            "UPDATE jobs SET status=?,stage=?,exit_code=?,finished_at=?,updated_at=?,"
            "error_type=?,error_message=? WHERE id=? AND status=?",
            (
                target,
                "finished" if target == JobStatus.FINISHED else "failed",
                0 if target == JobStatus.FINISHED else 1,
                now,
                now,
                "" if target == JobStatus.FINISHED else "community_publish",
                "" if target == JobStatus.FINISHED else "publish_failed",
                job_id,
                row["status"],
            ),
        )
        if cur.rowcount != 1:
            raise TransitionError("community publish changed during terminal reconciliation")
        return self.get_job(job_id)  # type: ignore[return-value]

    # ---- worker registry ----------------------------------------------------
    def register_worker(self, worker_id: str, pid: int, create_time: float | None = None) -> None:
        now = time.time()
        self._conn.execute(
            "INSERT INTO workers(worker_id, pid, started_at, heartbeat_at, create_time, stop_requested) "
            "VALUES(?,?,?,?,?,0) "
            "ON CONFLICT(worker_id) DO UPDATE SET pid=excluded.pid, "
            "heartbeat_at=excluded.heartbeat_at, create_time=excluded.create_time, stop_requested=0",
            (worker_id, int(pid), now, now, create_time),
        )

    def request_worker_stop(self, worker_id: str) -> None:
        self._conn.execute(
            "UPDATE workers SET stop_requested=1 WHERE worker_id=?", (worker_id,)
        )

    def worker_stop_requested(self, worker_id: str) -> bool:
        row = self._conn.execute(
            "SELECT stop_requested FROM workers WHERE worker_id=?", (worker_id,)
        ).fetchone()
        return bool(row and row["stop_requested"])

    def worker_heartbeat(self, worker_id: str) -> None:
        self._conn.execute(
            "UPDATE workers SET heartbeat_at=? WHERE worker_id=?", (time.time(), worker_id)
        )

    def unregister_worker(self, worker_id: str) -> None:
        self._conn.execute("DELETE FROM workers WHERE worker_id=?", (worker_id,))

    def healthy_worker(self, *, stale_seconds: float = 15.0) -> dict[str, Any] | None:
        cutoff = time.time() - stale_seconds
        row = self._conn.execute(
            "SELECT * FROM workers WHERE heartbeat_at >= ? ORDER BY heartbeat_at DESC LIMIT 1",
            (cutoff,),
        ).fetchone()
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    def get_worker(self, worker_id: str) -> dict[str, Any] | None:
        """Return one worker lease without exposing it outside the trusted bridge."""
        normalized = str(worker_id or "").strip()
        if not normalized:
            return None
        row = self._conn.execute(
            "SELECT * FROM workers WHERE worker_id=?", (normalized,)
        ).fetchone()
        return {key: row[key] for key in row.keys()} if row is not None else None
