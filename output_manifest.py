"""Schema and validation helpers for self-describing translation outputs."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse, urlunparse


MANIFEST_FILENAME = "run_manifest.json"
MANIFEST_VERSION = 3
_SUPPORTED_MANIFEST_VERSIONS = frozenset({1, 2, MANIFEST_VERSION})
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

# Output manifests are copied, listed and sometimes shared outside the machine that created a
# run.  Source provenance therefore consists solely of bounded scalar evidence.  In
# particular, candidate ids, URLs, DOM selectors, cookie-bearing headers and filesystem paths
# do not belong here.
_SAFE_SOURCE_CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}\Z")
_SAFE_RUN_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,119}\Z")
_MAX_SOURCE_COUNT = 1_000_000


def _safe_source_code(value: Any, *, maximum: int = 80) -> str:
    """Return a small identifier/reason code, never a URL or path-shaped value."""

    raw = str(value or "").strip()
    if len(raw) > maximum or not _SAFE_SOURCE_CODE_RE.fullmatch(raw):
        return ""
    return raw


def sanitize_run_slug(value: Any) -> str:
    """Return a safe output-folder identity, not a source-derived chapter name."""

    raw = str(value or "").strip().casefold()
    raw = re.sub(r"[^a-z0-9_-]+", "_", raw)
    raw = re.sub(r"_+", "_", raw).strip("_-")
    return raw[:120] if _SAFE_RUN_SLUG_RE.fullmatch(raw[:120]) else ""


def _safe_source_count(value: Any) -> int | None:
    """Accept a non-negative integer within a deliberately generous output bound."""

    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number < 0 or number > _MAX_SOURCE_COUNT:
        return None
    return number


def _safe_source_score(value: Any) -> float | None:
    """Keep only a finite confidence score in the normalised 0..1 range."""

    if isinstance(value, bool):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        return None
    return round(score, 6)


def sanitize_source_provenance(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build the public, scalar-only source evidence block for a run manifest.

    This deliberately ignores unknown keys.  It gives old manifests a compatible empty
    optional field while ensuring a newer manifest cannot accidentally carry a source URL,
    local folder or opaque candidate identifier through a nested diagnostic object.
    """

    if not isinstance(value, Mapping):
        return {}

    result: dict[str, Any] = {}
    source_type = str(value.get("source_type") or "").strip()
    if source_type in {"url", "local_folder"}:
        result["source_type"] = source_type

    for key, maximum in (
        ("adapter_name", 80),
        ("adapter_version", 40),
        ("transport_name", 80),
        ("outcome", 80),
    ):
        cleaned = _safe_source_code(value.get(key), maximum=maximum)
        if cleaned:
            result[key] = cleaned

    score = _safe_source_score(value.get("score", value.get("confidence")))
    if score is not None:
        result["score"] = score

    for key in ("candidate_count", "accepted_page_count", "rejected_page_count"):
        count = _safe_source_count(value.get(key))
        if count is not None:
            result[key] = count

    selection = value.get("selection")
    if isinstance(selection, Mapping):
        public_selection: dict[str, Any] = {}
        automatic = selection.get("automatic")
        if isinstance(automatic, bool):
            public_selection["mode"] = "automatic" if automatic else "manual"
        else:
            mode = _safe_source_code(selection.get("mode"), maximum=24)
            if mode in {"automatic", "manual"}:
                public_selection["mode"] = mode
        for key in ("selected_page_count", "accepted_candidate_count"):
            count = _safe_source_count(selection.get(key))
            if count is not None:
                public_selection[key] = count
        for key in ("manual_subset", "manual_reordered"):
            if isinstance(selection.get(key), bool):
                public_selection[key] = selection[key]
        reason = _safe_source_code(
            selection.get("reason_code", selection.get("reason")), maximum=80
        )
        if reason:
            public_selection["reason_code"] = reason
        if public_selection:
            result["selection"] = public_selection

    return result


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
    slug: str = "",
    series_slug: str = "",
    episode_number: str = "",
    source_type: str = "url",
    adapter_name: str = "",
    adapter_version: str = "",
    transport_name: str = "",
    source_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    # The descriptive fields are optional so a manifest written before they
    # existed stays valid; readers fall back to the path when they are absent.
    # A run identity belongs to its output directory.  Do not reconstruct it from the
    # redacted source URL: its path is intentionally an opaque fingerprint.
    run_slug = sanitize_run_slug(slug) or sanitize_run_slug(Path(str(pdf_path or "")).parent.name)
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
        "slug": run_slug,
        "source_type": "local_folder" if source_type == "local_folder" else "url",
        "adapter_name": _safe_source_code(adapter_name, maximum=80),
        "adapter_version": _safe_source_code(adapter_version, maximum=40),
        "transport_name": _safe_source_code(transport_name, maximum=80),
    }
    pdf_filename = Path(str(pdf_path or "")).name
    if pdf_filename:
        manifest["pdf_filename"] = pdf_filename
    if series_slug:
        manifest["series_slug"] = str(series_slug)
    if episode_number:
        manifest["episode_number"] = str(episode_number)
    provenance = sanitize_source_provenance(source_provenance)
    if provenance:
        manifest["source_provenance"] = provenance
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
    if payload.get("manifest_version") == MANIFEST_VERSION:
        slug = sanitize_run_slug(payload.get("slug"))
        if not slug or slug != payload.get("slug"):
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
    if "source_provenance" in payload:
        provenance = payload.get("source_provenance")
        if not isinstance(provenance, dict):
            return {}
        # A value written by this version must already be exactly the scalar-only public
        # representation.  Older manifests omit the optional field and remain valid.
        if provenance != sanitize_source_provenance(provenance):
            return {}
    return payload
