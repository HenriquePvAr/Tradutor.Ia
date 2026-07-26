"""Owner-scoped typography choices for human visual previews.

A font choice is a human decision about one already approved human translation.
It is not a runtime rule: the selected candidate is bound to the job/run/revision,
region, source hash, human translation decision and font file hash that produced
the preview.  Reusing it after drift fails closed.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "2"
STATUSES = ("selected", "selected_for_preview_generation", "draft_rendered", "discarded")
OPTIONAL_COLUMNS = {
    "requested_font": "TEXT NOT NULL DEFAULT ''",
    "visual_evidence_json": "TEXT NOT NULL DEFAULT '{}'",
    "selection_reason": "TEXT NOT NULL DEFAULT ''",
    "runner_authorization": "TEXT NOT NULL DEFAULT ''",
    "confidence": "REAL NOT NULL DEFAULT 0",
    "runner_up_candidate_json": "TEXT NOT NULL DEFAULT '{}'",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class HumanTypographyDecisionStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, timeout=5.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS human_typography_decisions (
                font_choice_decision_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                job_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                page_id TEXT NOT NULL,
                region_id TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                human_translation_decision_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                font_identity TEXT NOT NULL,
                font_file_hash TEXT NOT NULL,
                render_parameters_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                UNIQUE(owner, job_id, run_id, revision_id, region_id,
                       human_translation_decision_id, candidate_id, font_file_hash)
            )
        """)
        self._ensure_optional_columns()
        self._conn.commit()

    def _ensure_optional_columns(self) -> None:
        columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(human_typography_decisions)")
        }
        for name, ddl in OPTIONAL_COLUMNS.items():
            if name not in columns:
                self._conn.execute(
                    f"ALTER TABLE human_typography_decisions ADD COLUMN {name} {ddl}"
                )

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def upsert(self, *, owner: str, job_id: str, run_id: str, revision_id: str,
               page_id: str, region_id: str, source_hash: str,
               human_translation_decision_id: str, candidate: dict[str, Any],
               status: str = "selected", visual_evidence: dict[str, Any] | None = None,
               selection_reason: str = "", runner_authorization: str = "",
               confidence: float = 0.0,
               runner_up_candidate: dict[str, Any] | None = None) -> dict[str, Any]:
        if not str(owner or "").strip():
            raise ValueError("authentication_required")
        if status not in STATUSES:
            raise ValueError("unknown_typography_status")
        candidate_id = str(candidate.get("candidate_id") or "").strip()
        font_hash = str(candidate.get("font_path_hash") or candidate.get("font_file_hash") or "").strip()
        if not candidate_id or not font_hash:
            raise ValueError("font_candidate_not_auditable")
        now = _utc_now()
        existing = self.get_for_candidate(
            owner=owner, job_id=job_id, run_id=run_id, revision_id=revision_id,
            region_id=region_id, human_translation_decision_id=human_translation_decision_id,
            candidate_id=candidate_id, font_file_hash=font_hash,
        )
        record = {
            "font_choice_decision_id": (existing or {}).get("font_choice_decision_id") or uuid.uuid4().hex,
            "owner": str(owner),
            "job_id": str(job_id),
            "run_id": str(run_id),
            "revision_id": str(revision_id),
            "page_id": str(page_id),
            "region_id": str(region_id),
            "source_hash": str(source_hash),
            "human_translation_decision_id": str(human_translation_decision_id),
            "candidate_id": candidate_id,
            "font_identity": str(candidate.get("actual_font") or candidate.get("font_identity") or ""),
            "font_file_hash": font_hash,
            "requested_font": str(candidate.get("requested_font") or ""),
            "render_parameters_json": json.dumps({
                key: candidate.get(key)
                for key in ("requested_font", "font_size", "tracking", "slant", "resolved_font_path")
                if key in candidate
            }, ensure_ascii=False, sort_keys=True),
            "visual_evidence_json": json.dumps(visual_evidence or {}, ensure_ascii=False, sort_keys=True),
            "selection_reason": str(selection_reason or ""),
            "runner_authorization": str(runner_authorization or ""),
            "confidence": float(confidence or 0.0),
            "runner_up_candidate_json": json.dumps(runner_up_candidate or {}, ensure_ascii=False, sort_keys=True),
            "created_at": (existing or {}).get("created_at") or now,
            "updated_at": now,
            "status": status,
            "schema_version": SCHEMA_VERSION,
        }
        columns = ", ".join(record)
        placeholders = ", ".join(f":{key}" for key in record)
        self._conn.execute(
            f"INSERT OR REPLACE INTO human_typography_decisions ({columns}) VALUES ({placeholders})",
            record,
        )
        self._conn.commit()
        return self._row_payload(record)

    def get_for_candidate(self, *, owner: str, job_id: str, run_id: str, revision_id: str,
                          region_id: str, human_translation_decision_id: str,
                          candidate_id: str, font_file_hash: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM human_typography_decisions WHERE owner=? AND job_id=? AND run_id=? "
            "AND revision_id=? AND region_id=? AND human_translation_decision_id=? "
            "AND candidate_id=? AND font_file_hash=?",
            (str(owner), str(job_id), str(run_id), str(revision_id), str(region_id),
             str(human_translation_decision_id), str(candidate_id), str(font_file_hash)),
        ).fetchone()
        return self._row_payload(dict(row)) if row else None

    def get(self, decision_id: str, *, owner: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM human_typography_decisions WHERE font_choice_decision_id=? AND owner=?",
            (str(decision_id), str(owner)),
        ).fetchone()
        return self._row_payload(dict(row)) if row else None

    def latest_for_region(self, *, owner: str, job_id: str, run_id: str,
                          revision_id: str, region_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM human_typography_decisions WHERE owner=? AND job_id=? AND run_id=? "
            "AND revision_id=? AND region_id=? AND status != 'discarded' "
            "ORDER BY updated_at DESC LIMIT 1",
            (str(owner), str(job_id), str(run_id), str(revision_id), str(region_id)),
        ).fetchone()
        return self._row_payload(dict(row)) if row else None

    def set_status(self, decision_id: str, status: str, *, owner: str) -> dict[str, Any] | None:
        if status not in STATUSES:
            raise ValueError("unknown_typography_status")
        row = self.get(decision_id, owner=owner)
        if not row:
            return None
        self._conn.execute(
            "UPDATE human_typography_decisions SET status=?, updated_at=? WHERE font_choice_decision_id=?",
            (status, _utc_now(), str(decision_id)),
        )
        self._conn.commit()
        return self.get(decision_id, owner=owner)

    def discard(self, decision_id: str, *, owner: str) -> dict[str, Any] | None:
        return self.set_status(decision_id, "discarded", owner=owner)

    def _row_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row)
        try:
            payload["render_parameters"] = json.loads(str(payload.pop("render_parameters_json", "{}") or "{}"))
        except ValueError:
            payload["render_parameters"] = {}
        for column, output_name in (
            ("visual_evidence_json", "visual_evidence"),
            ("runner_up_candidate_json", "runner_up_candidate"),
        ):
            try:
                payload[output_name] = json.loads(str(payload.pop(column, "{}") or "{}"))
            except ValueError:
                payload[output_name] = {}
        return payload
