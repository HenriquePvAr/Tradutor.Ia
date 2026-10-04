"""Append-only human decisions over persisted linguistic refinements."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1"
ACTIONS = {"keep_current": "current", "select_option": "natural"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RefinementSelectionStore:
    """Transactional, owner-scoped selection history; no provider dependency."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, timeout=5, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS natural_ptbr_selection_decisions (
                decision_id TEXT PRIMARY KEY,
                decision_hash TEXT NOT NULL UNIQUE,
                schema_version TEXT NOT NULL,
                owner TEXT NOT NULL,
                job_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                page_id TEXT NOT NULL,
                region_id TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                result_id TEXT NOT NULL,
                response_hash TEXT NOT NULL,
                previous_decision_id TEXT NOT NULL,
                selected_action TEXT NOT NULL,
                selected_option TEXT NOT NULL,
                selected_text TEXT NOT NULL,
                current_translation_before TEXT NOT NULL,
                effective_translation_after TEXT NOT NULL,
                reviewer TEXT NOT NULL,
                authorization TEXT NOT NULL,
                authorization_scope TEXT NOT NULL,
                authorization_timestamp TEXT NOT NULL,
                operation TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                reversible INTEGER NOT NULL,
                supersedes_decision_id TEXT NOT NULL,
                plan_hash TEXT NOT NULL
            )
        """)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def prepare(intent: dict[str, Any], *, plan_hash: str) -> dict[str, Any]:
        action = str(intent.get("selected_action") or "")
        option = str(intent.get("selected_option") or "")
        if ACTIONS.get(action) != option:
            raise ValueError("refinement_selection_invalid")
        result = dict(intent.get("result") or {})
        request = dict(result.get("request") or {})
        if result.get("status") not in {"valid_suggestion", "needs_human_review"}:
            raise ValueError("refinement_result_invalid")
        if str(request.get("owner") or "") != str(intent.get("owner") or ""):
            raise ValueError("refinement_result_owner_mismatch")
        for key in ("job_id", "run_id", "revision_id", "page_id", "region_id"):
            if str(request.get(key) or "") != str(intent.get(key) or ""):
                raise ValueError(f"refinement_result_{key}_mismatch")
        result_value = dict(result.get("result") or {})
        current = str(intent.get("current_translation_before") or "")
        selected = current if action == "keep_current" else str(result_value.get("natural_ptbr") or "")
        if not selected.strip():
            raise ValueError("refinement_selected_text_empty")
        if str(intent.get("source_hash") or "") != hashlib.sha256(
            str(request.get("source_text") or "").encode("utf-8")
        ).hexdigest():
            raise ValueError("refinement_source_hash_mismatch")
        identity = {
            "schema_version": SCHEMA_VERSION,
            "owner": str(intent["owner"]),
            "result_id": str(result.get("result_hash") or ""),
            "response_hash": str(result.get("result_hash") or ""),
            "selected_action": action,
            "selected_option": option,
            "selected_text": selected,
            "previous_decision_id": str(intent.get("previous_decision_id") or ""),
            "authorization": str(intent.get("authorization") or ""),
            "authorization_scope": str(intent.get("authorization_scope") or ""),
        }
        decision_hash = _hash(identity)
        timestamp = str(intent.get("authorization_timestamp") or _now())
        return {
            "decision_id": decision_hash, "decision_hash": decision_hash,
            "schema_version": SCHEMA_VERSION, "owner": identity["owner"],
            **{key: str(intent.get(key) or "") for key in (
                "job_id", "run_id", "revision_id", "page_id", "region_id", "source_hash")},
            "request_hash": str(request.get("request_hash") or ""),
            "result_id": identity["result_id"], "response_hash": identity["response_hash"],
            "previous_decision_id": identity["previous_decision_id"],
            "selected_action": action, "selected_option": option,
            "selected_text": selected, "current_translation_before": current,
            "effective_translation_after": selected,
            "reviewer": str(intent.get("reviewer") or ""),
            "authorization": identity["authorization"],
            "authorization_scope": identity["authorization_scope"],
            "authorization_timestamp": timestamp,
            "operation": "confirm_refinement_selection",
            "reason": str(intent.get("reason") or ""),
            "status": "confirmed_human_selection", "created_at": timestamp,
            "reversible": 1,
            "supersedes_decision_id": str(intent.get("supersedes_decision_id") or ""),
            "plan_hash": str(plan_hash),
        }

    def confirm_batch(self, intents: list[dict[str, Any]], *, plan_hash: str) -> list[dict[str, Any]]:
        if not intents:
            raise ValueError("refinement_selection_plan_empty")
        prepared = [self.prepare(intent, plan_hash=plan_hash) for intent in intents]
        owner_revision = {(r["owner"], r["job_id"], r["run_id"], r["revision_id"])
                          for r in prepared}
        if len(owner_revision) != 1:
            raise ValueError("refinement_selection_plan_scope_mismatch")
        columns = list(prepared[0])
        sql = (f"INSERT OR IGNORE INTO natural_ptbr_selection_decisions "
               f"({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            for record in prepared:
                latest = self.latest_for_region(
                    record["job_id"], record["run_id"], record["revision_id"],
                    record["region_id"], owner=record["owner"], in_transaction=True)
                if latest and latest["decision_hash"] != record["decision_hash"]:
                    raise ValueError("selection_concurrent_state_changed")
                self._conn.execute(sql, [record[key] for key in columns])
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return [self.get(record["decision_id"]) for record in prepared]

    def get(self, decision_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM natural_ptbr_selection_decisions WHERE decision_id=?",
            (str(decision_id),)).fetchone()
        return dict(row) if row else None

    def latest_for_region(self, job_id: str, run_id: str, revision_id: str,
                          region_id: str, *, owner: str,
                          in_transaction: bool = False) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM natural_ptbr_selection_decisions WHERE job_id=? AND run_id=? "
            "AND revision_id=? AND region_id=? AND owner=? ORDER BY created_at DESC LIMIT 1",
            (job_id, run_id, revision_id, region_id, owner)).fetchone()
        return dict(row) if row else None

    def list_for(self, job_id: str, run_id: str, revision_id: str,
                 *, owner: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self._conn.execute(
            "SELECT * FROM natural_ptbr_selection_decisions WHERE job_id=? AND run_id=? "
            "AND revision_id=? AND owner=? ORDER BY created_at, region_id",
            (job_id, run_id, revision_id, owner)).fetchall()]
