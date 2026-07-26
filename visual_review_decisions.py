"""Owner-scoped delegated visual decisions for isolated preview drafts.

These decisions are audit records about a preview asset.  They never apply a
page permanently, promote a chapter revision, create a PDF, or publish
anything.  Reuse fails closed when the preview asset hash or lineage changes.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1"
DECISIONS = ("approved", "needs_adjustment", "rejected", "revoked")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class VisualReviewDecisionStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, timeout=5.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS visual_review_decisions (
                visual_review_decision_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                job_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                page_id TEXT NOT NULL,
                region_id TEXT NOT NULL,
                page_revision_id TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                preview_asset_hash TEXT NOT NULL,
                decision TEXT NOT NULL,
                authorization TEXT NOT NULL,
                reviewer TEXT NOT NULL,
                reason_codes_json TEXT NOT NULL,
                visual_evidence_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                UNIQUE(owner, job_id, run_id, page_revision_id, region_id, preview_asset_hash)
            )
        """)
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def upsert(self, *, owner: str, job_id: str, run_id: str, revision_id: str,
               page_id: str, region_id: str, page_revision_id: str, source_hash: str,
               preview_asset_hash: str, decision: str, authorization: str,
               reviewer: str, reason_codes: list[str] | None = None,
               visual_evidence: dict[str, Any] | None = None,
               status: str = "active") -> dict[str, Any]:
        if not str(owner or "").strip():
            raise ValueError("authentication_required")
        if decision not in DECISIONS:
            raise ValueError("unknown_visual_review_decision")
        if str(authorization or "") != "delegated_by_user":
            raise ValueError("visual_review_authorization_required")
        if not str(preview_asset_hash or "").strip():
            raise ValueError("preview_asset_hash_required")
        now = _utc_now()
        existing = self.latest_for_preview(
            owner=owner, job_id=job_id, run_id=run_id, page_revision_id=page_revision_id,
            region_id=region_id, preview_asset_hash=preview_asset_hash)
        record = {
            "visual_review_decision_id": (existing or {}).get("visual_review_decision_id") or uuid.uuid4().hex,
            "owner": str(owner),
            "job_id": str(job_id),
            "run_id": str(run_id),
            "revision_id": str(revision_id),
            "page_id": str(page_id),
            "region_id": str(region_id),
            "page_revision_id": str(page_revision_id),
            "source_hash": str(source_hash),
            "preview_asset_hash": str(preview_asset_hash),
            "decision": str(decision),
            "authorization": str(authorization),
            "reviewer": str(reviewer or "codex"),
            "reason_codes_json": json.dumps([str(v) for v in (reason_codes or [])], ensure_ascii=False),
            "visual_evidence_json": json.dumps(visual_evidence or {}, ensure_ascii=False, sort_keys=True),
            "status": str(status or "active"),
            "created_at": (existing or {}).get("created_at") or now,
            "updated_at": now,
            "schema_version": SCHEMA_VERSION,
        }
        columns = ", ".join(record)
        placeholders = ", ".join(f":{key}" for key in record)
        self._conn.execute(
            f"INSERT OR REPLACE INTO visual_review_decisions ({columns}) VALUES ({placeholders})",
            record,
        )
        self._conn.commit()
        return self._row_payload(record)

    def latest_for_preview(self, *, owner: str, job_id: str, run_id: str,
                           page_revision_id: str, region_id: str,
                           preview_asset_hash: str = "") -> dict[str, Any] | None:
        params: list[Any] = [str(owner), str(job_id), str(run_id), str(page_revision_id), str(region_id)]
        query = ("SELECT * FROM visual_review_decisions WHERE owner=? AND job_id=? AND run_id=? "
                 "AND page_revision_id=? AND region_id=?")
        if str(preview_asset_hash or ""):
            query += " AND preview_asset_hash=?"
            params.append(str(preview_asset_hash))
        query += " ORDER BY updated_at DESC LIMIT 1"
        row = self._conn.execute(query, tuple(params)).fetchone()
        return self._row_payload(dict(row)) if row else None

    def _row_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row)
        for column, output_name, fallback in (
            ("reason_codes_json", "reason_codes", []),
            ("visual_evidence_json", "visual_evidence", {}),
        ):
            try:
                payload[output_name] = json.loads(str(payload.pop(column, "") or "null"))
            except ValueError:
                payload[output_name] = fallback
            if payload[output_name] is None:
                payload[output_name] = fallback
        return payload
