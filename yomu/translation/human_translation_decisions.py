"""Human overrides of a provider translation (BLOCO 6C).

A provider answer is a candidate, not a verdict. When a human writes a better
line for a region, that line is *data of this revision*: owner-scoped, bound to
the exact provider execution and source text it answers, idempotent, updatable
and removable. It never becomes a rule — nothing here maps a phrase to another
phrase, so the same code serves any chapter.

Additive: one new table in the local jobs sqlite, created on demand. Historical
revisions, PDFs and publications are never touched.
"""
from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1"

# What the human did with the provider's answer.
DECISIONS = ("replace_provider_candidate", "accept_provider_candidate", "reject_both")
# Where that decision stands in the preview flow.
STATUSES = ("approved_for_preview", "draft_rendered", "visually_approved",
            "visually_rejected", "discarded")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def source_text_hash(text: str) -> str:
    """Bind a decision to the exact text it answers, so drift fails closed."""
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


class HumanTranslationDecisionStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, timeout=5.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS human_translation_decisions (
                human_translation_decision_id TEXT PRIMARY KEY,
                provider_execution_id TEXT NOT NULL,
                authorization_request_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                page_id TEXT NOT NULL,
                region_id TEXT NOT NULL,
                source_text TEXT NOT NULL,
                source_text_hash TEXT NOT NULL,
                provider_candidate TEXT NOT NULL,
                human_candidate TEXT NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL,
                taxonomy_version TEXT,
                gate_version TEXT,
                schema_version TEXT NOT NULL,
                UNIQUE(job_id, run_id, revision_id, region_id, created_by)
            )
        """)
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    # --- write ------------------------------------------------------------
    def upsert(self, *, provider_execution_id: str, authorization_request_id: str,
               job_id: str, run_id: str, revision_id: str, page_id: str, region_id: str,
               source_text: str, provider_candidate: str, human_candidate: str,
               created_by: str, decision: str = "replace_provider_candidate",
               reason: str = "", status: str = "approved_for_preview",
               taxonomy_version: str = "", gate_version: str = "") -> dict[str, Any]:
        """One decision per user per region per revision, updated in place."""
        if not str(created_by or "").strip():
            raise ValueError("authentication_required")
        if decision not in DECISIONS:
            raise ValueError("unknown_decision")
        if status not in STATUSES:
            raise ValueError("unknown_status")
        if not str(human_candidate or "").strip() and decision == "replace_provider_candidate":
            raise ValueError("empty_human_candidate")

        now = _utc_now()
        existing = self.get_for_region(job_id, run_id, revision_id, region_id,
                                       created_by=created_by)
        record = {
            "human_translation_decision_id": (existing or {}).get(
                "human_translation_decision_id") or uuid.uuid4().hex,
            "provider_execution_id": str(provider_execution_id),
            "authorization_request_id": str(authorization_request_id),
            "job_id": str(job_id), "run_id": str(run_id), "revision_id": str(revision_id),
            "page_id": str(page_id), "region_id": str(region_id),
            "source_text": str(source_text),
            "source_text_hash": source_text_hash(source_text),
            "provider_candidate": str(provider_candidate),
            "human_candidate": str(human_candidate),
            "decision": decision, "reason": str(reason or ""),
            "created_by": str(created_by),
            "created_at": (existing or {}).get("created_at") or now,
            "updated_at": now,
            "status": status,
            "taxonomy_version": str(taxonomy_version or ""),
            "gate_version": str(gate_version or ""),
            "schema_version": SCHEMA_VERSION,
        }
        columns = ", ".join(record)
        placeholders = ", ".join(f":{key}" for key in record)
        self._conn.execute(
            f"INSERT OR REPLACE INTO human_translation_decisions ({columns}) "
            f"VALUES ({placeholders})", record)
        self._conn.commit()
        return record

    def set_status(self, decision_id: str, status: str, *, created_by: str) -> dict[str, Any] | None:
        if status not in STATUSES:
            raise ValueError("unknown_status")
        row = self.get(decision_id)
        if not row:
            return None
        if str(row["created_by"]) != str(created_by):
            raise ValueError("not_decision_owner")
        self._conn.execute(
            "UPDATE human_translation_decisions SET status=?, updated_at=? "
            "WHERE human_translation_decision_id=?", (status, _utc_now(), str(decision_id)))
        self._conn.commit()
        return self.get(decision_id)

    def delete(self, decision_id: str, *, created_by: str) -> bool:
        row = self.get(decision_id)
        if not row:
            return False
        if str(row["created_by"]) != str(created_by):
            raise ValueError("not_decision_owner")
        self._conn.execute(
            "DELETE FROM human_translation_decisions WHERE human_translation_decision_id=?",
            (str(decision_id),))
        self._conn.commit()
        return True

    # --- read -------------------------------------------------------------
    def get(self, decision_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM human_translation_decisions WHERE human_translation_decision_id=?",
            (str(decision_id),)).fetchone()
        return dict(row) if row else None

    def get_for_region(self, job_id: str, run_id: str, revision_id: str, region_id: str,
                       *, created_by: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM human_translation_decisions WHERE job_id=? AND run_id=? "
            "AND revision_id=? AND region_id=? AND created_by=?",
            (str(job_id), str(run_id), str(revision_id), str(region_id), str(created_by))).fetchone()
        return dict(row) if row else None

    def list_for(self, job_id: str, run_id: str, revision_id: str, *,
                 created_by: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM human_translation_decisions WHERE job_id=? AND run_id=? "
            "AND revision_id=? AND created_by=? ORDER BY region_id",
            (str(job_id), str(run_id), str(revision_id), str(created_by))).fetchall()
        return [dict(row) for row in rows]


def validate_against_execution(decision: dict[str, Any], execution: dict[str, Any]) -> None:
    """Fail closed when a decision no longer answers what it was made against.

    A decision carries the provider execution and the exact source text it was
    written for. If either moved, rendering it would silently put a human line
    onto a region that now says something else.
    """
    if not decision:
        raise ValueError("human_decision_not_found")
    if not execution:
        raise ValueError("provider_execution_not_found")
    if str(decision.get("provider_execution_id")) != str(
            execution.get("authorization_request_id")):
        raise ValueError("provider_execution_mismatch")
    result = next((r for r in (execution.get("results") or [])
                   if str(r.get("region_id")) == str(decision.get("region_id"))), None)
    if result is None:
        raise ValueError("region_not_in_provider_execution")
    if source_text_hash(result.get("text") or "") != str(decision.get("source_text_hash")):
        raise ValueError("source_text_hash_mismatch")
    if str(result.get("translation") or "") != str(decision.get("provider_candidate") or ""):
        raise ValueError("provider_candidate_mismatch")
