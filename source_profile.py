"""Sanitised, opt-in reusable evidence for a previously successful generic reader.

Profiles are hints, never permission.  They contain no URL, query string, cookie, token or
chapter pixels; the next analysis still performs its normal SSRF checks, browser observation
and clustering.  A mismatching/expired profile simply contributes no signal.
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any

from chapter_source import (
    REVIEW_REQUIRED_MEDIUM_CONFIDENCE,
    SUPPORTED_GENERIC_HIGH_CONFIDENCE,
)

PROFILE_VERSION = 1
PROFILE_MAX_AGE_SECONDS = 90 * 24 * 60 * 60
DEFAULT_PROFILE_PATH = Path(__file__).resolve().parent / ".cache" / "runtime" / "source_profiles.json"
_EVIDENCE_RE = re.compile(r"^cluster:[0-9a-f]{20}$")


def _safe_host(value: Any) -> str:
    host = str(value or "").strip().lower()
    return host[:253] if host and all(char.isalnum() or char in ".-" for char in host) else ""


def profile_from_analysis(analysis: dict[str, Any], selection: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Derive a small reader hint from a *sanitised*, successfully used analysis."""
    if not isinstance(analysis, dict) or str(analysis.get("adapter") or "") != "universal":
        return None
    # This helper can be called outside the runner in maintenance tooling. Do not rely only
    # on the runner's FINISHED gate: a partial or otherwise terminally unsafe reader analysis
    # must never become a reusable hint.
    if str(analysis.get("outcome") or "") not in {
        SUPPORTED_GENERIC_HIGH_CONFIDENCE,
        REVIEW_REQUIRED_MEDIUM_CONFIDENCE,
    }:
        return None
    host = _safe_host(analysis.get("final_host"))
    accepted = analysis.get("accepted") if isinstance(analysis.get("accepted"), list) else []
    clusters = analysis.get("clusters") if isinstance(analysis.get("clusters"), list) else []
    if not host or not accepted or not clusters:
        return None
    accepted_ids = {
        str(item.get("id") or "") for item in accepted if isinstance(item, dict)
    }
    selected_ids = {
        str(value or "") for value in ((selection or {}).get("candidate_ids") or [])
        if str(value or "")
    }
    # Only the selection produced by this completed download is reusable evidence. A legacy
    # analysis or a stale preview cannot manufacture a profile for a later reader snapshot.
    if not selected_ids or not selected_ids.issubset(accepted_ids):
        return None
    chosen = next(
        (
            cluster for cluster in clusters
            if isinstance(cluster, dict)
            and selected_ids.issubset(
                {str(value or "") for value in cluster.get("candidate_ids") or []}
            )
        ),
        None,
    )
    if not isinstance(chosen, dict):
        return None
    key = str(chosen.get("key") or "")
    if not _EVIDENCE_RE.fullmatch(key):
        return None
    signals = [str(value)[:80] for value in (chosen.get("signals") or []) if str(value)][:12]
    return {
        "profile_version": PROFILE_VERSION,
        "host": host,
        "container_evidence": key,
        "positive_signals": signals,
        "strategy": "clustered_dom_network_json",
        "observed_score": float(chosen.get("score") or 0.0),
        "selection_mode": "manual" if selection and not selection.get("automatic", True) else "automatic",
        "validated_at": time.time(),
    }


class SourceProfileStore:
    """Small file-backed profile registry keyed by exact reader host."""

    def __init__(self, path: str | Path = DEFAULT_PROFILE_PATH):
        self.path = Path(path)

    def load(self, host: str) -> dict[str, Any] | None:
        host = _safe_host(host)
        if not host:
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        profile = data.get(host) if isinstance(data, dict) else None
        if not isinstance(profile, dict) or profile.get("profile_version") != PROFILE_VERSION:
            return None
        try:
            validated_at = float(profile.get("validated_at"))
        except (TypeError, ValueError):
            return None
        age = time.time() - validated_at
        if not math.isfinite(validated_at) or age < -300 or age > PROFILE_MAX_AGE_SECONDS:
            return None
        # Validate exact host again rather than treating a wildcard as permission.
        if _safe_host(profile.get("host")) != host:
            return None
        if not _EVIDENCE_RE.fullmatch(str(profile.get("container_evidence") or "")):
            return None
        return dict(profile)

    def record_success(self, analysis: dict[str, Any], selection: dict[str, Any] | None = None) -> dict[str, Any] | None:
        profile = profile_from_analysis(analysis, selection)
        if not profile:
            return None
        data: dict[str, Any] = {}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError, TypeError):
            pass
        data[profile["host"]] = profile
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        return dict(profile)
