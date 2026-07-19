"""Trusted bridge from a local-folder snapshot to the ordinary page-processing pipeline.

The public folder is validated and snapshotted by :mod:`local_folder_source` before this
module is called.  This second boundary accepts only that generated snapshot manifest, checks
its hashes again, and materialises generated page names in the run's ``output/input`` folder.
It never opens a browser, performs an HTTP request, or exposes the original path/name in a
download report.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from chapter_source import INCOMPLETE_DOWNLOAD, NO_CHAPTER_IMAGES, SUPPORTED_SPECIFIC_ADAPTER
from image_validation import validate_image_bytes
from local_folder_source import (
    LOCAL_FOLDER_ADAPTER_NAME,
    LOCAL_FOLDER_ADAPTER_VERSION,
    LOCAL_FOLDER_MANIFEST_VERSION,
    HARD_MAX_BYTES_PER_FILE,
    HARD_MAX_TOTAL_BYTES,
    LOCAL_INPUT_LIMIT,
    LOCAL_INVALID_IMAGE,
    LOCAL_PATH_NOT_ALLOWED,
    LOCAL_REPARSE_POINT,
    LOCAL_WORKSPACE_INVALID,
    LocalFolderError,
)


REPO_ROOT = Path(__file__).resolve().parent
LOCAL_SNAPSHOT_ROOT = REPO_ROOT / ".cache" / "runtime" / "local_sources"
_SNAPSHOT_FILE_RE = re.compile(r"^\d{4}\.(?:png|jpg|jpeg|webp|avif)$", re.IGNORECASE)
_OUTPUT_FILE_RE = re.compile(r"^\d{3}\.(?:png|jpg|jpeg|webp|avif)$", re.IGNORECASE)
_OWNERSHIP_MARKER = ".tradutor_local_snapshot_input"


def local_source_reference(source_fingerprint: object) -> str:
    """Return the only local-source identifier allowed in reports/jobs/output manifests."""
    value = str(source_fingerprint or "").casefold()
    if not re.fullmatch(r"[0-9a-f]{16,64}", value):
        raise LocalFolderError(LOCAL_WORKSPACE_INVALID, "invalid_source_fingerprint")
    return f"local-folder:{value[:24]}"


def snapshot_workspace_root() -> Path:
    """Create only the ignored internal workspace root, never the user input root."""
    LOCAL_SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    return LOCAL_SNAPSHOT_ROOT.resolve()


def materialize_snapshot(
    manifest_path: str | Path,
    target_folder: str | Path,
    *,
    max_images: int | None = None,
    snapshot_root: str | Path | None = None,
    output_root: str | Path | None = None,
    clear_existing: bool = False,
) -> tuple[list[str], dict[str, Any]]:
    """Validate one owned snapshot and return the normal downloader-report shape.

    The snapshot cannot point at arbitrary local files: its manifest must be a direct child of
    the configured internal snapshot root and every page filename is generated/validated.
    A malformed/incomplete snapshot fails the whole input gate rather than silently producing
    a shorter chapter.
    """
    root = Path(snapshot_root or LOCAL_SNAPSHOT_ROOT).resolve()
    manifest_file = Path(manifest_path).expanduser().resolve(strict=True)
    try:
        manifest_file.relative_to(root)
    except ValueError as exc:
        raise LocalFolderError(LOCAL_PATH_NOT_ALLOWED, "manifest_outside_workspace") from exc
    if manifest_file.name != "manifest.json" or manifest_file.parent.parent != root:
        raise LocalFolderError(LOCAL_WORKSPACE_INVALID, "invalid_snapshot_layout")
    if _is_reparse(manifest_file) or _is_reparse(manifest_file.parent):
        raise LocalFolderError(LOCAL_REPARSE_POINT, "snapshot_reparse_point")
    payload = _load_manifest(manifest_file)
    reference = _validate_manifest(payload)
    pages = payload["pages"]
    if max_images is not None:
        if not isinstance(max_images, int) or max_images <= 0:
            raise LocalFolderError(LOCAL_INPUT_LIMIT, "invalid_max_images")
        pages = pages[:max_images]
    if not pages:
        raise LocalFolderError(NO_CHAPTER_IMAGES, "empty_snapshot_selection")

    destination_root = Path(target_folder).expanduser().resolve()
    allowed_output_root = Path(output_root or (REPO_ROOT / "output")).expanduser().resolve()
    try:
        destination_root.relative_to(allowed_output_root)
    except ValueError as exc:
        raise LocalFolderError(LOCAL_PATH_NOT_ALLOWED, "output_outside_root") from exc
    try:
        destination_root.relative_to(manifest_file.parent)
    except ValueError:
        pass
    else:
        raise LocalFolderError(LOCAL_PATH_NOT_ALLOWED, "output_inside_snapshot")
    destination_root.mkdir(parents=True, exist_ok=True)
    _prepare_owned_target(destination_root, clear_existing=clear_existing)

    downloaded: list[dict[str, Any]] = []
    paths: list[str] = []
    total_bytes = 0
    for index, raw in enumerate(pages, start=1):
        item = dict(raw)
        filename = str(item.get("filename") or "")
        if not _SNAPSHOT_FILE_RE.fullmatch(filename):
            raise LocalFolderError(LOCAL_WORKSPACE_INVALID, "invalid_snapshot_filename")
        source = (manifest_file.parent / filename).resolve(strict=True)
        if source.parent != manifest_file.parent or _is_reparse(source):
            raise LocalFolderError(LOCAL_REPARSE_POINT, "snapshot_page_reparse")
        data = _read_bounded(
            source,
            expected_size=item.get("byte_size"),
            remaining_total=HARD_MAX_TOTAL_BYTES - total_bytes,
        )
        validated = validate_image_bytes(
            data, min_width=480, min_height=1, min_bytes=12,
        )
        if validated.sha256 != str(item.get("sha256") or ""):
            raise LocalFolderError(LOCAL_INVALID_IMAGE, "snapshot_hash_mismatch")
        if validated.fmt != str(item.get("format") or ""):
            raise LocalFolderError(LOCAL_INVALID_IMAGE, "snapshot_format_mismatch")
        total_bytes += len(data)
        if total_bytes > HARD_MAX_TOTAL_BYTES:
            raise LocalFolderError(LOCAL_INPUT_LIMIT, "max_total_bytes")
        destination = destination_root / f"{index:03d}{source.suffix.casefold()}"
        _atomic_write(destination, data)
        paths.append(str(destination))
        downloaded.append({
            "path": str(destination),
            "url": f"{reference}/page/{index:04d}",
            "sha256": validated.sha256,
            "width": validated.width,
            "height": validated.height,
            "format": validated.fmt,
            "mime_type": str(item.get("mime_type") or ""),
            "is_chapter_candidate": True,
            "candidate_id": str(item.get("id") or ""),
            "logical_page": True,
        })

    analysis = {
        "source_type": "local_folder",
        "adapter": LOCAL_FOLDER_ADAPTER_NAME,
        "adapter_version": LOCAL_FOLDER_ADAPTER_VERSION,
        "outcome": SUPPORTED_SPECIFIC_ADAPTER,
        "confidence": 1.0,
        "candidate_count": len(downloaded),
        "accepted_count": len(downloaded),
        "discarded_count": 0,
        "accepted": [{
            "id": item["candidate_id"], "order": order, "width": item["width"],
            "height": item["height"], "origin": "local_snapshot", "visible": True,
        } for order, item in enumerate(downloaded, start=1)],
        "discarded": [], "clusters": [], "warnings": [], "profile_used": False,
    }
    selection = {
        "candidate_ids": [item["candidate_id"] for item in downloaded],
        "automatic": True,
        "accepted_candidate_count": len(downloaded),
        "selected_candidate_count": len(downloaded),
        "manual_subset": False,
    }
    report: dict[str, Any] = {
        "url": reference,
        "source_type": "local_folder",
        "adapter_name": LOCAL_FOLDER_ADAPTER_NAME,
        "adapter_version": LOCAL_FOLDER_ADAPTER_VERSION,
        "transport_name": "local_snapshot",
        "logical_pages": True,
        "requires_smart_split": False,
        "viewer_image_count": len(downloaded),
        "total_downloaded": len(downloaded),
        "total_bytes": total_bytes,
        "downloaded": downloaded,
        "source_analysis": analysis,
        "source_selection": selection,
        "download_gate": {
            "passed": True, "reasons": [], "expected_viewer_images": len(downloaded),
            "total_viewer_images": len(downloaded), "downloaded_viewer_images": len(downloaded),
            "missing_viewer_images": 0, "order_monotonic": True,
        },
    }
    _atomic_write(destination_root / _OWNERSHIP_MARKER, reference.encode("ascii"))
    return paths, report


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise LocalFolderError(LOCAL_WORKSPACE_INVALID, "snapshot_manifest_unreadable") from exc
    if not isinstance(payload, dict):
        raise LocalFolderError(LOCAL_WORKSPACE_INVALID, "snapshot_manifest_invalid")
    return payload


def _validate_manifest(payload: dict[str, Any]) -> str:
    if payload.get("manifest_version") != LOCAL_FOLDER_MANIFEST_VERSION:
        raise LocalFolderError(LOCAL_WORKSPACE_INVALID, "snapshot_manifest_version")
    if payload.get("source_type") != "local_folder":
        raise LocalFolderError(LOCAL_WORKSPACE_INVALID, "snapshot_manifest_source_type")
    if payload.get("adapter_name") != LOCAL_FOLDER_ADAPTER_NAME:
        raise LocalFolderError(LOCAL_WORKSPACE_INVALID, "snapshot_manifest_adapter")
    pages = payload.get("pages")
    if not isinstance(pages, list) or not pages:
        raise LocalFolderError(NO_CHAPTER_IMAGES, "snapshot_manifest_pages")
    if len(pages) > 400:
        raise LocalFolderError(LOCAL_INPUT_LIMIT, "max_files")
    if not all(isinstance(item, dict) for item in pages):
        raise LocalFolderError(LOCAL_WORKSPACE_INVALID, "snapshot_manifest_pages")
    return local_source_reference(payload.get("source_fingerprint"))


def _read_bounded(path: Path, *, expected_size: object, remaining_total: int) -> bytes:
    """Read a snapshot page with the same hard caps as the original intake.

    A snapshot is an internal workspace, not an immutable filesystem primitive.  It can be
    changed between initial validation and a resumed job, so do not trust its manifest size or
    call ``Path.read_bytes()``.  The descriptor identity, byte count and aggregate chapter
    budget are all checked while reading before a destination page is created.
    """

    try:
        declared = int(expected_size)
    except (TypeError, ValueError) as exc:
        raise LocalFolderError(LOCAL_WORKSPACE_INVALID, "snapshot_size_invalid") from exc
    if declared < 12 or declared > HARD_MAX_BYTES_PER_FILE:
        raise LocalFolderError(LOCAL_INPUT_LIMIT, "max_bytes_per_file")
    if remaining_total < 0 or declared > remaining_total:
        raise LocalFolderError(LOCAL_INPUT_LIMIT, "max_total_bytes")
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise LocalFolderError(LOCAL_WORKSPACE_INVALID, "snapshot_page_unreadable") from exc
    if _is_reparse(path) or not stat.S_ISREG(before.st_mode):
        raise LocalFolderError(LOCAL_REPARSE_POINT, "snapshot_page_reparse")
    if before.st_size != declared:
        raise LocalFolderError(LOCAL_INVALID_IMAGE, "snapshot_size_mismatch")
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0) or 0)
    flags |= int(getattr(os, "O_NOFOLLOW", 0) or 0)
    chunks: list[bytes] = []
    total = 0
    try:
        descriptor = os.open(str(path), flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != _file_identity(before):
                raise LocalFolderError(LOCAL_INVALID_IMAGE, "snapshot_changed_before_read")
            while total < declared:
                chunk = handle.read(min(1024 * 1024, declared - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > remaining_total:
                    raise LocalFolderError(LOCAL_INPUT_LIMIT, "max_total_bytes")
                chunks.append(chunk)
            # Probe exactly one byte: this detects a race/growth without reading an unbounded
            # payload into memory.
            if handle.read(1):
                raise LocalFolderError(LOCAL_INVALID_IMAGE, "snapshot_size_mismatch")
    except LocalFolderError:
        raise
    except OSError as exc:
        raise LocalFolderError(LOCAL_WORKSPACE_INVALID, "snapshot_page_unreadable") from exc
    try:
        after = os.lstat(path)
    except OSError as exc:
        raise LocalFolderError(LOCAL_INVALID_IMAGE, "snapshot_changed_during_read") from exc
    if _is_reparse(path) or not stat.S_ISREG(after.st_mode):
        raise LocalFolderError(LOCAL_REPARSE_POINT, "snapshot_page_reparse")
    if _file_identity(before) != _file_identity(after):
        raise LocalFolderError(LOCAL_INVALID_IMAGE, "snapshot_changed_during_read")
    if total != declared:
        raise LocalFolderError(LOCAL_INVALID_IMAGE, "snapshot_size_mismatch")
    return b"".join(chunks)


def _file_identity(entry: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(getattr(entry, "st_dev", 0) or 0),
        int(getattr(entry, "st_ino", 0) or 0),
        int(getattr(entry, "st_size", 0) or 0),
        int(getattr(entry, "st_mtime_ns", int(entry.st_mtime * 1_000_000_000))),
    )


def _is_reparse(path: Path) -> bool:
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0) or 0)
    except OSError:
        return True
    return path.is_symlink() or bool(attributes & 0x400)


def _prepare_owned_target(target: Path, *, clear_existing: bool) -> None:
    children = list(target.iterdir())
    if not children:
        return
    marker = target / _OWNERSHIP_MARKER
    if not clear_existing or not marker.is_file() or _is_reparse(marker):
        raise LocalFolderError(LOCAL_WORKSPACE_INVALID, "output_target_not_owned")
    for child in children:
        if child.is_symlink() or _is_reparse(child):
            raise LocalFolderError(LOCAL_REPARSE_POINT, "output_reparse_point")
        if child.name != _OWNERSHIP_MARKER and not _OUTPUT_FILE_RE.fullmatch(child.name):
            raise LocalFolderError(LOCAL_WORKSPACE_INVALID, "output_target_not_owned")
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        except OSError as exc:
            raise LocalFolderError(LOCAL_WORKSPACE_INVALID, "output_cleanup_failed") from exc


def _atomic_write(path: Path, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).replace(path)
    finally:
        temporary_path = Path(temporary)
        if temporary_path.exists():
            temporary_path.unlink()
