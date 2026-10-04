"""Owner-scoped human mask refinements for visual preview reconstruction.

Mask edits are operation data, not runtime rules.  A saved mask is bound to the
job/run/revision/region/source hash and the automatic segmentation hash that it
refines.  Confirming a mask never disables safety gates.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1"
STATUSES = ("draft", "confirmed", "discarded")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rects(mask: Any) -> list[list[int]]:
    rects: list[list[int]] = []
    for item in mask or []:
        if not isinstance(item, (list, tuple)) or len(item) != 4:
            raise ValueError("mask_rect_invalid")
        x, y, w, h = [int(v) for v in item]
        if w <= 0 or h <= 0:
            raise ValueError("mask_rect_invalid")
        rects.append([x, y, w, h])
    return rects


def _area(rects: list[list[int]]) -> int:
    return sum(max(0, int(w)) * max(0, int(h)) for _, _, w, h in rects)


def _intersection_area(left: list[list[int]], right: list[list[int]]) -> int:
    total = 0
    for ax, ay, aw, ah in left:
        ar, ab = ax + aw, ay + ah
        for bx, by, bw, bh in right:
            br, bb = bx + bw, by + bh
            width = max(0, min(ar, br) - max(ax, bx))
            height = max(0, min(ab, bb) - max(ay, by))
            total += width * height
    return total


def validate_mask_payload(payload: dict[str, Any], *, region_box: list[int] | tuple[int, int, int, int],
                          base_segmentation_hash: str, source_hash: str) -> dict[str, Any]:
    """Validate a human mask draft without trusting it blindly."""
    if str(payload.get("base_segmentation_hash") or "") != str(base_segmentation_hash):
        raise ValueError("mask_segmentation_hash_mismatch")
    if str(payload.get("source_hash") or "") != str(source_hash):
        raise ValueError("mask_source_hash_mismatch")
    if len(region_box) != 4:
        raise ValueError("mask_region_geometry_unavailable")
    rx, ry, rw, rh = [int(v) for v in region_box]
    if rw <= 0 or rh <= 0:
        raise ValueError("mask_region_geometry_unavailable")
    include = _rects(payload.get("include_mask") or [])
    exclude = _rects(payload.get("exclude_mask") or [])
    protected = _rects(payload.get("protected_mask") or [])
    uncertain = _rects(payload.get("uncertain_mask") or [])
    for rect in include + exclude + protected + uncertain:
        x, y, w, h = rect
        if x < rx or y < ry or x + w > rx + rw or y + h > ry + rh:
            raise ValueError("mask_outside_authorized_region")
    final_metrics = payload.get("final_mask_metrics") if isinstance(payload.get("final_mask_metrics"), dict) else {}
    include_area = int(final_metrics.get("final_mask_area") or _area(include))
    region_area = max(1, rw * rh)
    if include_area <= 0:
        raise ValueError("mask_empty")
    mask_ratio = include_area / region_area
    if mask_ratio > 0.34:
        raise ValueError("mask_area_excessive")
    protected_area = _area(protected)
    uncertain_area = _area(uncertain)
    protected_overlap = int(final_metrics.get("protected_overlap") or _intersection_area(include, protected))
    if protected_overlap:
        raise ValueError("mask_protected_overlap")
    if uncertain_area:
        raise ValueError("mask_uncertain_pixels_unresolved")
    return {
        "include_area": include_area,
        "exclude_area": _area(exclude),
        "protected_area": protected_area,
        "protected_overlap": protected_overlap,
        "uncertain_area": uncertain_area,
        "region_area": region_area,
        "mask_ratio": round(mask_ratio, 4),
        "connected_components": int(final_metrics.get("connected_components") or len(include)),
        "mask_hash": str(final_metrics.get("mask_hash") or ""),
        "status": "valid_for_local_preview_candidate",
    }


class HumanMaskDecisionStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, timeout=5.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS human_mask_decisions (
                human_mask_decision_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                job_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                page_id TEXT NOT NULL,
                region_id TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                base_segmentation_hash TEXT NOT NULL,
                include_mask_json TEXT NOT NULL,
                exclude_mask_json TEXT NOT NULL,
                protected_mask_json TEXT NOT NULL,
                uncertain_mask_json TEXT NOT NULL,
                validation_json TEXT NOT NULL,
                notes TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                UNIQUE(owner, job_id, run_id, revision_id, region_id, base_segmentation_hash)
            )
        """)
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def upsert(self, *, owner: str, job_id: str, run_id: str, revision_id: str,
               page_id: str, region_id: str, source_hash: str,
               base_segmentation_hash: str, include_mask: Any = None,
               exclude_mask: Any = None, protected_mask: Any = None,
               uncertain_mask: Any = None, validation: dict[str, Any] | None = None,
               notes: str = "", status: str = "draft") -> dict[str, Any]:
        if not str(owner or "").strip():
            raise ValueError("authentication_required")
        if status not in STATUSES:
            raise ValueError("unknown_mask_status")
        now = _utc_now()
        existing = self.latest_for_region(
            owner=owner, job_id=job_id, run_id=run_id, revision_id=revision_id,
            region_id=region_id, base_segmentation_hash=base_segmentation_hash)
        record = {
            "human_mask_decision_id": (existing or {}).get("human_mask_decision_id") or uuid.uuid4().hex,
            "owner": str(owner),
            "job_id": str(job_id),
            "run_id": str(run_id),
            "revision_id": str(revision_id),
            "page_id": str(page_id),
            "region_id": str(region_id),
            "source_hash": str(source_hash),
            "base_segmentation_hash": str(base_segmentation_hash),
            "include_mask_json": json.dumps(_rects(include_mask or []), sort_keys=True),
            "exclude_mask_json": json.dumps(_rects(exclude_mask or []), sort_keys=True),
            "protected_mask_json": json.dumps(_rects(protected_mask or []), sort_keys=True),
            "uncertain_mask_json": json.dumps(_rects(uncertain_mask or []), sort_keys=True),
            "validation_json": json.dumps(validation or {}, ensure_ascii=False, sort_keys=True),
            "notes": str(notes or ""),
            "status": status,
            "created_at": (existing or {}).get("created_at") or now,
            "updated_at": now,
            "schema_version": SCHEMA_VERSION,
        }
        columns = ", ".join(record)
        placeholders = ", ".join(f":{key}" for key in record)
        self._conn.execute(
            f"INSERT OR REPLACE INTO human_mask_decisions ({columns}) VALUES ({placeholders})",
            record,
        )
        self._conn.commit()
        return self._row_payload(record)

    def latest_for_region(self, *, owner: str, job_id: str, run_id: str, revision_id: str,
                          region_id: str, base_segmentation_hash: str = "") -> dict[str, Any] | None:
        params: list[Any] = [str(owner), str(job_id), str(run_id), str(revision_id), str(region_id)]
        query = ("SELECT * FROM human_mask_decisions WHERE owner=? AND job_id=? AND run_id=? "
                 "AND revision_id=? AND region_id=? AND status != 'discarded'")
        if str(base_segmentation_hash or ""):
            query += " AND base_segmentation_hash=?"
            params.append(str(base_segmentation_hash))
        query += " ORDER BY updated_at DESC LIMIT 1"
        row = self._conn.execute(query, tuple(params)).fetchone()
        return self._row_payload(dict(row)) if row else None

    def _row_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row)
        for column, output_name in (
            ("include_mask_json", "include_mask"),
            ("exclude_mask_json", "exclude_mask"),
            ("protected_mask_json", "protected_mask"),
            ("uncertain_mask_json", "uncertain_mask"),
            ("validation_json", "validation"),
        ):
            try:
                payload[output_name] = json.loads(str(payload.pop(column, "[]" if column.endswith("mask_json") else "{}") or "[]"))
            except ValueError:
                payload[output_name] = [] if column.endswith("mask_json") else {}
        return payload
