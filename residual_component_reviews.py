"""Owner-scoped, content-addressed review decisions for residual components."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1"
CLASSIFICATIONS = {"text", "art", "uncertain"}
STATUSES = {"resolved", "blocked_pending_component_review"}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ResidualComponentReviewStore:
    def __init__(self, db_path: str | Path):
        self._conn = sqlite3.connect(str(db_path), timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS residual_component_reviews (
                component_review_decision_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                residual_analysis_id TEXT NOT NULL,
                component_id TEXT NOT NULL,
                component_bitmap_hash TEXT NOT NULL,
                pixel_decisions_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                authorization TEXT NOT NULL,
                reviewer TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                UNIQUE(owner, residual_analysis_id, component_id,
                       component_bitmap_hash, pixel_decisions_json)
            )
        """)
        self._conn.commit()

    def persist(
        self,
        *,
        owner: str,
        residual_analysis_id: str,
        component_id: str,
        component_bitmap_hash: str,
        pixel_decisions: list[dict[str, Any]],
        evidence: dict[str, Any],
        authorization: str,
        reviewer: str,
        status: str,
    ) -> dict[str, Any]:
        if authorization != "delegated_by_user":
            raise ValueError("component_review_authorization_required")
        if status not in STATUSES:
            raise ValueError("component_review_status_invalid")
        normalized = []
        for item in pixel_decisions:
            classification = str(item.get("classification") or "")
            if classification not in CLASSIFICATIONS:
                raise ValueError("component_review_classification_invalid")
            coordinate = [int(value) for value in item.get("coordinate", [])]
            value = [int(channel) for channel in item.get("value", [])]
            if len(coordinate) != 2 or len(value) not in (1, 3, 4):
                raise ValueError("component_review_pixel_invalid")
            normalized.append({
                "coordinate": coordinate,
                "value": value,
                "classification": classification,
                "confidence": round(float(item.get("confidence") or 0.0), 6),
                "reason_codes": sorted(
                    str(reason) for reason in item.get("reason_codes", [])
                    if str(reason)),
            })
        normalized.sort(key=lambda item: tuple(item["coordinate"]))
        if any(item["classification"] == "uncertain" for item in normalized):
            status = "blocked_pending_component_review"
        pixels_json = _canonical_json(normalized)
        identity_payload = _canonical_json({
            "owner": str(owner),
            "residual_analysis_id": str(residual_analysis_id),
            "component_id": str(component_id),
            "component_bitmap_hash": str(component_bitmap_hash),
            "pixel_decisions": normalized,
        }).encode("utf-8")
        decision_id = hashlib.sha256(identity_payload).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute("""
            INSERT OR IGNORE INTO residual_component_reviews VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            decision_id, str(owner), str(residual_analysis_id),
            str(component_id), str(component_bitmap_hash), pixels_json,
            _canonical_json(evidence or {}), authorization, str(reviewer),
            status, now, SCHEMA_VERSION,
        ))
        self._conn.commit()
        return self.get(decision_id)

    def get(self, decision_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("""
            SELECT * FROM residual_component_reviews
            WHERE component_review_decision_id=?
        """, (str(decision_id),)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["pixel_decisions"] = json.loads(
            result.pop("pixel_decisions_json"))
        result["evidence"] = json.loads(result.pop("evidence_json"))
        return result

    def latest(
        self,
        *,
        owner: str,
        residual_analysis_id: str,
        component_id: str,
    ) -> dict[str, Any] | None:
        row = self._conn.execute("""
            SELECT component_review_decision_id
            FROM residual_component_reviews
            WHERE owner=? AND residual_analysis_id=? AND component_id=?
            ORDER BY created_at DESC, component_review_decision_id DESC
            LIMIT 1
        """, (
            str(owner), str(residual_analysis_id), str(component_id),
        )).fetchone()
        return self.get(row[0]) if row else None

    def list_latest_for_analysis(
        self,
        *,
        owner: str,
        residual_analysis_id: str,
    ) -> list[dict[str, Any]]:
        rows = self._conn.execute("""
            SELECT component_review_decision_id
            FROM residual_component_reviews AS reviews
            WHERE owner=? AND residual_analysis_id=?
              AND NOT EXISTS (
                  SELECT 1
                  FROM residual_component_reviews AS newer
                  WHERE newer.owner=reviews.owner
                    AND newer.residual_analysis_id=reviews.residual_analysis_id
                    AND newer.component_id=reviews.component_id
                    AND (
                        newer.created_at > reviews.created_at
                        OR (
                            newer.created_at = reviews.created_at
                            AND newer.component_review_decision_id >
                                reviews.component_review_decision_id
                        )
                    )
              )
            ORDER BY component_id
        """, (str(owner), str(residual_analysis_id))).fetchall()
        return [
            review
            for row in rows
            if (review := self.get(row[0])) is not None
        ]

    def close(self) -> None:
        self._conn.close()
