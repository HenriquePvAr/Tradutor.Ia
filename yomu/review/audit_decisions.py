"""Human audit decisions store (BLOCO 3).

A derived-view decision layer that never touches historical revisions, PDFs or
publications. Decisions are owned by the authenticated user, validated against
the chapter/revision lineage and the audit artifact they were made against, and
are idempotent (one decision per user per region per revision, updatable and
removable). Additive: a single new table, created on demand.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# A human who looked at the crop may record what the region really says. It is
# audit metadata: it never rewrites a page, and it is what gets sent to the
# provider in place of a read nobody believes.
CORRECTED_READING_PREFIX = "leitura_correta: "


def corrected_reading(decision: dict) -> str:
    """The corrected reading a human attached to a decision, or ""."""
    notes = str((decision or {}).get("notes") or "")
    if not notes.startswith(CORRECTED_READING_PREFIX):
        return ""
    return notes[len(CORRECTED_READING_PREFIX):].strip()


DECISIONS = ("translate", "preserve", "ocr_invalid", "needs_review", "dismissed",
             "classify_credit", "classify_title_name", "classify_editorial",
             "classify_sfx", "classify_watermark")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditDecisionStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        # The UI serves requests from a thread pool, so the connection must be
        # usable across threads (WAL + busy_timeout keep concurrent access safe).
        self._conn = sqlite3.connect(self.db_path, timeout=5.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_decisions (
                audit_decision_id TEXT PRIMARY KEY,
                audit_artifact_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                page_id TEXT NOT NULL,
                region_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT,
                notes TEXT,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                taxonomy_version TEXT,
                source_audit_hash TEXT,
                UNIQUE(job_id, run_id, revision_id, region_id, created_by)
            )
        """)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return {k: row[k] for k in row.keys()}

    def upsert(self, *, job_id: str, run_id: str, revision_id: str, audit_artifact_id: str,
               page_id: str, region_id: str, decision: str, created_by: str,
               reason: str = "", notes: str = "", taxonomy_version: str = "",
               source_audit_hash: str = "") -> dict[str, Any]:
        if decision not in DECISIONS:
            raise ValueError("invalid_decision")
        for name, value in (("job_id", job_id), ("run_id", run_id), ("revision_id", revision_id),
                            ("region_id", region_id), ("created_by", created_by),
                            ("audit_artifact_id", audit_artifact_id)):
            if not str(value or "").strip():
                raise ValueError(f"missing_{name}")
        now = _utc_now()
        existing = self._conn.execute(
            "SELECT * FROM audit_decisions WHERE job_id=? AND run_id=? AND revision_id=? "
            "AND region_id=? AND created_by=?",
            (job_id, run_id, revision_id, region_id, created_by)).fetchone()
        if existing:
            # Idempotent update: same identity, new decision/reason/notes, bumped ts.
            self._conn.execute(
                "UPDATE audit_decisions SET decision=?, reason=?, notes=?, updated_at=?, "
                "audit_artifact_id=?, taxonomy_version=?, source_audit_hash=?, page_id=? "
                "WHERE audit_decision_id=?",
                (decision, reason, notes, now, audit_artifact_id, taxonomy_version,
                 source_audit_hash, page_id, existing["audit_decision_id"]))
            self._conn.commit()
            return self.get(existing["audit_decision_id"])
        decision_id = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO audit_decisions (audit_decision_id, audit_artifact_id, job_id, run_id, "
            "revision_id, page_id, region_id, decision, reason, notes, created_by, created_at, "
            "updated_at, taxonomy_version, source_audit_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (decision_id, audit_artifact_id, job_id, run_id, revision_id, page_id, region_id,
             decision, reason, notes, created_by, now, now, taxonomy_version, source_audit_hash))
        self._conn.commit()
        return self.get(decision_id)

    def get(self, decision_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM audit_decisions WHERE audit_decision_id=?", (str(decision_id),)).fetchone()
        return self._row(row) if row else None

    def list_for(self, job_id: str, run_id: str, revision_id: str, *,
                 created_by: str | None = None) -> list[dict[str, Any]]:
        query = ("SELECT * FROM audit_decisions WHERE job_id=? AND run_id=? AND revision_id=?")
        params: list[Any] = [job_id, run_id, revision_id]
        if created_by is not None:
            query += " AND created_by=?"
            params.append(created_by)
        query += " ORDER BY updated_at DESC"
        return [self._row(r) for r in self._conn.execute(query, params).fetchall()]

    def delete(self, decision_id: str, *, created_by: str) -> bool:
        row = self._conn.execute(
            "SELECT created_by FROM audit_decisions WHERE audit_decision_id=?", (str(decision_id),)).fetchone()
        if not row:
            return False
        if str(row["created_by"]) != str(created_by):
            raise ValueError("not_decision_owner")
        self._conn.execute("DELETE FROM audit_decisions WHERE audit_decision_id=?", (str(decision_id),))
        self._conn.commit()
        return True
