"""Turn an authorized local folder into a queued translation job.

Bridges the local-folder adapter (validation, snapshot, manifest) to the job store. Nothing
here fetches anything: the input is files the user already has on disk.

The job row carries provenance only — a fingerprint and an opaque snapshot reference. The
original folder and the snapshot location stay server-side; neither ever reaches the browser
or a social endpoint, because a filesystem path leaks the account name and the machine's
layout.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

from chapter_source import SourceError
from local_folder_source import (
    LOCAL_FOLDER_ADAPTER_NAME,
    LOCAL_FOLDER_ADAPTER_VERSION,
    LocalFolderChapterAdapter,
)

SOURCE_TYPE_LOCAL_FOLDER = "local_folder"
SOURCE_TYPE_URL = "url"

# Stages specific to the local path; the shared pipeline stages continue afterwards.
STAGE_VALIDATING_SOURCE = "validating_local_source"
STAGE_CREATING_SNAPSHOT = "creating_snapshot"
STAGE_VALIDATING_PAGES = "validating_pages"


def folder_fingerprint(folder: Path) -> str:
    """Stable, non-reversible reference to a folder, safe to show and to store."""
    return hashlib.sha256(str(folder).casefold().encode("utf-8")).hexdigest()[:16]


def display_name(folder: Path) -> str:
    """Only the final folder name is safe to display — never the full path."""
    return folder.name or "pasta"


def build_local_job_command(*, snapshot_ref: str, output: str, mode: str,
                            logical_pages: bool, use_cache: bool, force: bool,
                            use_context: bool, open_output: bool = False,
                            python_executable: str | None = None) -> list[str]:
    """Argument list for the local runner. Never a shell string.

    The runner receives an opaque snapshot reference, not a client-supplied path, so a
    crafted request cannot make it read arbitrary files.
    """
    command = [
        python_executable or sys.executable,
        str(Path(__file__).resolve().parent / "run_local_folder.py"),
        "--snapshot-ref", str(snapshot_ref),
        "--output", str(output),
        "--mode", str(mode),
    ]
    if logical_pages:
        command.append("--logical-pages")
    if force:
        command.append("--force")
    elif use_cache:
        command.append("--cache")
    if not use_context:
        command.append("--no-context")
    if open_output:
        command.append("--open-output")
    return command


def analyse_local_folder(folder: str, *, adapter: LocalFolderChapterAdapter | None = None
                         ) -> dict[str, Any]:
    """Validate the folder and summarise it for the UI. Raises SourceError on refusal."""
    adapter = adapter or LocalFolderChapterAdapter()
    resolved = adapter.validate_path(folder)
    analysis = adapter.analyze(resolved)
    return {"resolved": resolved, "analysis": analysis}


def public_summary(resolved: Path, analysis: Any) -> dict[str, Any]:
    """Counts only — no path, no user name, no snapshot location.

    The adapter is fail-closed: a rejected or duplicated file aborts the whole input gate
    rather than being silently dropped, so a successful analysis has nothing rejected. The
    counters are kept in the payload because the job row and the UI both report them, and a
    future tolerant mode would fill them in without changing this shape.
    """
    accepted = len(getattr(analysis, "pages", ()) or ())
    return {
        "source_type": SOURCE_TYPE_LOCAL_FOLDER,
        "adapter_name": LOCAL_FOLDER_ADAPTER_NAME,
        "adapter_version": LOCAL_FOLDER_ADAPTER_VERSION,
        "folder_name": display_name(resolved),
        "input_root_fingerprint": folder_fingerprint(resolved),
        "input_count": accepted,
        "candidate_count": accepted,
        "source_score": 1.0,
        "accepted_count": accepted,
        "rejected_count": 0,
        "duplicate_count": 0,
        "total_size_bytes": int(getattr(analysis, "total_bytes", 0) or 0),
        "logical_pages": True,
        "rejection_reasons": [],
    }


def job_fields(summary: dict[str, Any], snapshot_ref: str) -> dict[str, Any]:
    """The subset persisted on the job row."""
    # A stored local-folder summary from an older UI/test fixture predates the explicit
    # score/candidate columns.  The local adapter is deterministic: every accepted input
    # is a candidate and its successful validation has full confidence.  Preserve that
    # invariant without making an old, path-free summary impossible to resume.
    accepted_count = int(summary.get("accepted_count", summary.get("input_count", 0)) or 0)
    candidate_count = int(summary.get("candidate_count", accepted_count) or 0)
    source_score = summary.get("source_score", 1.0)
    return {
        "source_type": SOURCE_TYPE_LOCAL_FOLDER,
        "adapter_name": summary["adapter_name"],
        "adapter_version": summary["adapter_version"],
        "transport_name": "local_snapshot",
        "source_score": source_score,
        "candidate_count": candidate_count,
        "input_root_fingerprint": summary["input_root_fingerprint"],
        "snapshot_ref": snapshot_ref,
        "logical_pages": 1 if summary.get("logical_pages") else 0,
        "input_count": summary["input_count"],
        "accepted_count": accepted_count,
        "rejected_count": summary["rejected_count"],
        "duplicate_count": summary["duplicate_count"],
        "total_size_bytes": summary["total_size_bytes"],
    }


def ensure_not_both_sources(payload: dict[str, Any]) -> str:
    """A submission is either a URL or a folder — never both, never neither."""
    url = str(payload.get("url") or "").strip()
    folder = str(payload.get("local_folder") or "").strip()
    if url and folder:
        raise SourceError("invalid_request", "url_and_folder")
    if folder:
        return SOURCE_TYPE_LOCAL_FOLDER
    if url:
        return SOURCE_TYPE_URL
    raise SourceError("invalid_request", "missing_source")
