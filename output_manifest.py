"""Schema and validation helpers for self-describing translation outputs."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse


MANIFEST_FILENAME = "run_manifest.json"
MANIFEST_VERSION = 2
_SUPPORTED_MANIFEST_VERSIONS = frozenset({1, MANIFEST_VERSION})
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
    """Keep an auditable origin and opaque path fingerprint in output metadata.

    Query strings and userinfo are obvious credential carriers, but signed readers sometimes
    place a token directly in the path as well. Outputs only need to correlate a source with
    its host/path shape, never to recreate the request, so retain a short one-way fingerprint
    instead of the literal path.
    """

    raw = str(url or "").strip()
    local = re.fullmatch(r"local-folder:([0-9a-fA-F]{16,64})", raw)
    if local:
        # A local input is represented only by an opaque content/source fingerprint.  It is
        # not a URI and must never fall through to a filesystem path or a browser address.
        return f"local-folder:{local.group(1).casefold()[:24]}"
    try:
        parsed = urlparse(raw)
        scheme = parsed.scheme.casefold()
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    if scheme not in {"http", "https"} or not host:
        return ""
    # ``parsed.netloc`` retains userinfo.  Rebuild it from hostname/port so a malformed
    # submitted URL can never copy credentials into an output manifest or diagnostic.
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    default_port = 80 if scheme == "http" else 443
    netloc = display_host if not port or port == default_port else f"{display_host}:{port}"
    raw_path = parsed.path or "/"
    path_fingerprint = hashlib.sha256(raw_path.encode("utf-8", "ignore")).hexdigest()[:12]
    return urlunparse((scheme, netloc, f"/path-{path_fingerprint}", "", "", ""))


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
    series_slug: str = "",
    episode_number: str = "",
    source_type: str = "url",
    adapter_name: str = "",
    adapter_version: str = "",
    transport_name: str = "",
) -> dict[str, Any]:
    # The descriptive fields are optional so a manifest written before they
    # existed stays valid; readers fall back to the path when they are absent.
    manifest = {
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
        "source_type": "local_folder" if source_type == "local_folder" else "url",
        "adapter_name": str(adapter_name or "")[:80],
        "adapter_version": str(adapter_version or "")[:40],
        "transport_name": str(transport_name or "")[:80],
    }
    pdf_filename = Path(str(pdf_path or "")).name
    if pdf_filename:
        manifest["pdf_filename"] = pdf_filename
    if series_slug:
        manifest["series_slug"] = str(series_slug)
    if episode_number:
        manifest["episode_number"] = str(episode_number)
    return manifest


def load_verified_run_manifest(output_folder: Path) -> dict[str, Any]:
    """Return a schema-valid manifest, or an empty dict for old outputs."""

    path = Path(output_folder) / MANIFEST_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    if payload.get("manifest_version") not in _SUPPORTED_MANIFEST_VERSIONS:
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
