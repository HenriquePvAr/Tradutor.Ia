"""Schema and validation helpers for self-describing translation outputs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse


MANIFEST_FILENAME = "run_manifest.json"
MANIFEST_VERSION = 1
_REQUIRED_FIELDS = {
    "manifest_version",
    "run_id",
    "created_at",
    "source_url",
    "commit_hash",
    "branch",
    "pipeline_version",
    "model",
    "final_status",
    "quality_passed",
    "manual_review_count",
    "rejected_count",
    "pdf_path",
}


def sanitize_source_url(url: str) -> str:
    """Keep only the non-sensitive origin and path in output metadata."""

    parsed = urlparse(str(url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def build_run_manifest(
    *,
    run_id: str,
    created_at: str,
    source_url: str,
    commit_hash: str,
    branch: str,
    pipeline_version: str,
    model: str,
    final_status: str,
    quality_passed: bool,
    manual_review_count: int,
    rejected_count: int,
    pdf_path: str,
) -> dict[str, Any]:
    return {
        "manifest_version": MANIFEST_VERSION,
        "run_id": str(run_id or ""),
        "created_at": str(created_at or ""),
        "source_url": sanitize_source_url(source_url),
        "commit_hash": str(commit_hash or ""),
        "branch": str(branch or ""),
        "pipeline_version": str(pipeline_version or ""),
        "model": str(model or ""),
        "final_status": str(final_status or ""),
        "quality_passed": bool(quality_passed),
        "manual_review_count": max(0, int(manual_review_count or 0)),
        "rejected_count": max(0, int(rejected_count or 0)),
        "pdf_path": str(pdf_path or ""),
    }


def load_verified_run_manifest(output_folder: Path) -> dict[str, Any]:
    """Return a schema-valid manifest, or an empty dict for old outputs."""

    path = Path(output_folder) / MANIFEST_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    if payload.get("manifest_version") != MANIFEST_VERSION:
        return {}
    if not _REQUIRED_FIELDS.issubset(payload):
        return {}
    if not all(
        str(payload.get(field) or "").strip()
        for field in (
            "run_id",
            "created_at",
            "source_url",
            "commit_hash",
            "branch",
            "pipeline_version",
            "model",
            "final_status",
            "pdf_path",
        )
    ):
        return {}
    if not isinstance(payload.get("quality_passed"), bool):
        return {}
    try:
        if int(payload.get("manual_review_count")) < 0:
            return {}
        if int(payload.get("rejected_count")) < 0:
            return {}
    except (TypeError, ValueError):
        return {}
    return payload
