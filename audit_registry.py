"""Additive registration & resolution of linguistic audit artifacts (BLOCO 3).

The audit report is tied to a base revision. Instead of rewriting historical
revision manifests, artifacts are stored in a dedicated, additive area and
indexed by a small registry keyed on ``revision_id``. Resolution is by identity
(output dir + revision id), never by glob, mtime, title or a "latest" heuristic;
the loaded report's hash is verified against the registry so a stale or swapped
file fails closed.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import region_taxonomy as tax

REGISTRY_SCHEMA_VERSION = "1"
_AUDIT_SUBDIR = "linguistic_audit"
_REPORT_NAME = "linguistic_page_audit.json"
_REGISTRY_NAME = "registry.json"

# Keys every audit report must carry to be accepted by the UI/decisions layer.
_REQUIRED_REPORT_KEYS = ("taxonomy_version", "revision_id", "records",
                         "by_normalized_category", "total_regions_audited")


def _audit_root(output_dir: str) -> Path:
    return Path(output_dir) / "quality_revision" / _AUDIT_SUBDIR


def canonical_hash(report: dict[str, Any]) -> str:
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_report_schema(report: dict[str, Any]) -> None:
    if not isinstance(report, dict):
        raise ValueError("audit_report_not_an_object")
    for key in _REQUIRED_REPORT_KEYS:
        if key not in report:
            raise ValueError(f"audit_report_missing_{key}")
    if str(report.get("taxonomy_version") or "") != tax.TAXONOMY_VERSION:
        raise ValueError("audit_report_taxonomy_version_mismatch")
    if not isinstance(report.get("records"), list):
        raise ValueError("audit_report_records_not_a_list")


def register_audit(output_dir: str, revision_id: str, report: dict[str, Any]) -> dict[str, Any]:
    """Persist a report under the audit area and index it. Additive only."""
    validate_report_schema(report)
    if str(report.get("revision_id") or "") != str(revision_id):
        raise ValueError("audit_report_revision_mismatch")
    root = _audit_root(output_dir)
    rev_dir = root / str(revision_id)
    rev_dir.mkdir(parents=True, exist_ok=True)
    report_path = rev_dir / _REPORT_NAME
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    source_hash = canonical_hash(report)
    entry = {
        "audit_artifact_id": hashlib.sha256(f"{revision_id}:{source_hash}".encode()).hexdigest()[:16],
        "revision_id": str(revision_id),
        "report_relpath": f"{_AUDIT_SUBDIR}/{revision_id}/{_REPORT_NAME}",
        "source_audit_hash": source_hash,
        "taxonomy_version": str(report.get("taxonomy_version")),
        "registry_schema_version": REGISTRY_SCHEMA_VERSION,
        "created_at": _utc_now(),
    }
    registry_path = root / _REGISTRY_NAME
    registry = _read_json(registry_path)
    if not isinstance(registry.get("artifacts"), dict):
        registry = {"registry_schema_version": REGISTRY_SCHEMA_VERSION, "artifacts": {}}
    registry["artifacts"][str(revision_id)] = entry
    registry_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
    return entry


def resolve_registered_audit(output_dir: str, revision_id: str) -> dict[str, Any] | None:
    """Return {'entry', 'report'} for a revision, or None. Hash-verified, confined."""
    root = _audit_root(output_dir).resolve()
    registry = _read_json(root / _REGISTRY_NAME)
    entry = (registry.get("artifacts") or {}).get(str(revision_id))
    if not entry:
        return None
    # Path confinement: the report must resolve to a location inside the audit
    # area, so a tampered report_relpath cannot escape via traversal.
    import os
    report_path = (Path(output_dir) / "quality_revision" / str(entry.get("report_relpath") or "")).resolve()
    if os.path.commonpath([str(report_path), str(root)]) != str(root):
        raise ValueError("audit_report_outside_audit_area")
    report = _read_json(report_path)
    if not report:
        raise ValueError("audit_report_unreadable")
    validate_report_schema(report)
    if canonical_hash(report) != str(entry.get("source_audit_hash") or ""):
        raise ValueError("audit_report_hash_mismatch")
    return {"entry": entry, "report": report}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
