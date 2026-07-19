"""Hermetic, fail-closed intake for chapter images stored in a local folder.

This module deliberately has no dependency on Selenium, requests, a downloader or the
job/UI layers.  It validates a user-selected directory, snapshots its direct image files
into a caller-owned workspace, and emits only sanitised metadata.  The integration layer
can therefore give the OCR pipeline a stable local manifest without retaining an absolute
source path in an output, job-facing DTO, or log.

The policy is intentionally conservative:

* only an explicit allowlist of roots is accepted (the default is ``<repo>/input`` when it
  already exists; the module never creates it);
* traversal, UNC/device paths, symlinks and Windows reparse points fail closed;
* only direct regular files are considered, in deterministic natural order;
* every accepted file is signature/Pillow/OpenCV validated and constrained by chapter
  limits before it is copied;
* a duplicate, invalid image or limit breach fails the whole intake instead of silently
  producing a shorter chapter.

The snapshot contains generated names only.  Its manifest carries hashes and opaque
fingerprints, never the original folder, file names, or paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np

from chapter_source import NO_CHAPTER_IMAGES, SourceError
from image_validation import DuplicateTracker, ValidatedImage, validate_image_bytes


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_ALLOWED_ROOT = REPO_ROOT / "input"

LOCAL_FOLDER_ADAPTER_NAME = "local_folder"
LOCAL_FOLDER_ADAPTER_VERSION = "1"
LOCAL_FOLDER_MANIFEST_VERSION = 1

LOCAL_INPUT_NOT_CONFIGURED = "local_input_not_configured"
LOCAL_PATH_UNSUPPORTED = "local_path_unsupported"
LOCAL_PATH_TRAVERSAL = "local_path_traversal"
LOCAL_PATH_NOT_ALLOWED = "local_path_not_allowed"
LOCAL_PATH_NOT_FOUND = "local_path_not_found"
LOCAL_FOLDER_ROOT_NOT_SELECTABLE = "local_folder_root_not_selectable"
LOCAL_REPARSE_POINT = "local_reparse_point"
LOCAL_UNSUPPORTED_EXTENSION = "local_unsupported_extension"
LOCAL_INVALID_IMAGE = "invalid_local_image"
LOCAL_INPUT_LIMIT = "local_input_limit"
LOCAL_INPUT_DUPLICATE = "local_input_duplicate"
LOCAL_WORKSPACE_OVERLAP = "local_workspace_overlap"
LOCAL_WORKSPACE_INVALID = "local_workspace_invalid"
LOCAL_SNAPSHOT_CONFLICT = "local_snapshot_conflict"

_ALLOWED_EXTENSION_FORMATS = {
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".webp": "webp",
    ".avif": "avif",
}
_IMAGE_MIME_BY_FORMAT = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "avif": "image/avif",
}
_DISALLOWED_IMAGE_EXTENSIONS = frozenset({
    ".gif", ".bmp", ".tif", ".tiff", ".ico", ".svg", ".heic", ".heif",
})
_SNAPSHOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")
_NATURAL_PART_RE = re.compile(r"(\d+)")

HARD_MAX_BYTES_PER_FILE = 32 * 1024 * 1024
HARD_MAX_TOTAL_BYTES = 1024 * 1024 * 1024
HARD_MAX_FILES = 400
# A folder full of unrelated files should not turn source validation into an unbounded
# directory walk.  This is deliberately larger than the page cap so harmless sidecar files
# do not change the chapter contract.
HARD_MAX_DIRECTORY_ENTRIES = 1600


class LocalFolderError(SourceError):
    """A coded local-source failure whose detail never contains a source path."""


@dataclass(frozen=True)
class LocalFolderLimits:
    """Secure upper bounds for one local chapter intake.

    Callers may lower a limit for an isolated run/test, but may never increase the safe
    project-wide defaults through this value object.
    """

    max_bytes_per_file: int = HARD_MAX_BYTES_PER_FILE
    max_total_bytes: int = HARD_MAX_TOTAL_BYTES
    max_files: int = HARD_MAX_FILES

    def __post_init__(self) -> None:
        if not 1 <= int(self.max_bytes_per_file) <= HARD_MAX_BYTES_PER_FILE:
            raise ValueError("invalid_local_max_bytes_per_file")
        if not 1 <= int(self.max_total_bytes) <= HARD_MAX_TOTAL_BYTES:
            raise ValueError("invalid_local_max_total_bytes")
        if not 1 <= int(self.max_files) <= HARD_MAX_FILES:
            raise ValueError("invalid_local_max_files")
        if int(self.max_total_bytes) < int(self.max_bytes_per_file):
            raise ValueError("local_total_limit_smaller_than_file_limit")


@dataclass(frozen=True)
class LocalFolderPage:
    """An internally-addressable validated source page.

    ``source_path`` and ``source_name`` intentionally stay out of ``public()`` and the
    persisted snapshot manifest.  They exist only while the trusted local process creates
    its immutable snapshot.
    """

    order: int
    source_path: Path
    source_name: str
    source_name_fingerprint: str
    sha256: str
    width: int
    height: int
    fmt: str
    mime_type: str
    byte_size: int

    @property
    def candidate_id(self) -> str:
        return f"local-{self.order:04d}-{self.sha256[:16]}"

    @property
    def snapshot_filename(self) -> str:
        extension = next(
            suffix for suffix, fmt in _ALLOWED_EXTENSION_FORMATS.items() if fmt == self.fmt
        )
        return f"{self.order:04d}{extension}"

    def public(self) -> dict[str, object]:
        return {
            "id": self.candidate_id,
            "order": self.order,
            "width": self.width,
            "height": self.height,
            "format": self.fmt,
            "mime_type": self.mime_type,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
            "source_name_fingerprint": self.source_name_fingerprint,
        }


@dataclass(frozen=True)
class LocalFolderAnalysis:
    """Validated local-folder evidence, with a private source path and public DTO."""

    source_folder: Path
    source_fingerprint: str
    pages: tuple[LocalFolderPage, ...]
    total_bytes: int
    limits: LocalFolderLimits

    @property
    def adapter_name(self) -> str:
        return LOCAL_FOLDER_ADAPTER_NAME

    @property
    def adapter_version(self) -> str:
        return LOCAL_FOLDER_ADAPTER_VERSION

    def public(self) -> dict[str, object]:
        return {
            "source_type": "local_folder",
            "adapter": self.adapter_name,
            "adapter_version": self.adapter_version,
            "source_fingerprint": self.source_fingerprint,
            "accepted_count": len(self.pages),
            "total_bytes": self.total_bytes,
            "candidate_ids": [page.candidate_id for page in self.pages],
            "pages": [page.public() for page in self.pages],
        }


@dataclass(frozen=True)
class LocalFolderSnapshot:
    """Internal handle for a snapshot plus a path-free manifest payload."""

    workspace: Path
    manifest_path: Path
    analysis: LocalFolderAnalysis

    def public(self) -> dict[str, object]:
        payload = _load_json(self.manifest_path)
        return payload if isinstance(payload, dict) else {}


def natural_sort_key(value: str | Path) -> tuple[tuple[tuple[int, object, int], ...], str, str]:
    """Locale-independent natural order for chapter file names.

    Numeric runs compare numerically, so ``1, 2, 3, 10`` retain page order.  Length and
    normalized/original names make ties such as ``1`` and ``01`` deterministic.
    """

    original = Path(value).name
    normalized = unicodedata.normalize("NFKC", original)
    folded = normalized.casefold()
    parts: list[tuple[int, object, int]] = []
    for part in _NATURAL_PART_RE.split(folded):
        if not part:
            continue
        if part.isdigit():
            parts.append((0, int(part), len(part)))
        else:
            parts.append((1, part, 0))
    return tuple(parts), folded, normalized


def configured_allowed_roots(value: str | None = None) -> tuple[Path, ...]:
    """Resolve configured trusted roots without creating any directory.

    The environment format follows ``os.pathsep`` (``;`` on Windows).  A missing setting
    means only the conventional repository ``input/`` directory, and only when it already
    exists as a real directory.  A malformed configuration fails closed rather than
    silently broadening access.
    """

    raw = os.getenv("LOCAL_INPUT_ROOTS") if value is None else value
    candidates: list[Path]
    if raw is None:
        candidates = [DEFAULT_ALLOWED_ROOT]
    else:
        candidates = [Path(part.strip()).expanduser() for part in raw.split(os.pathsep) if part.strip()]
    roots: list[Path] = []
    for candidate in candidates:
        _reject_raw_path(str(candidate))
        # The conventional ``input/`` folder is a disabled-by-absence default.  It must
        # never be created merely because the adapter is imported; selecting a local source
        # later yields ``local_input_not_configured`` if the operator has not made it.
        if raw is None and not candidate.exists():
            continue
        if not candidate.is_absolute():
            raise LocalFolderError(LOCAL_INPUT_NOT_CONFIGURED, "root_not_absolute")
        if not candidate.exists() or not candidate.is_dir():
            raise LocalFolderError(LOCAL_INPUT_NOT_CONFIGURED, "root_not_directory")
        _ensure_no_reparse_components(candidate)
        resolved = candidate.resolve(strict=True)
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


class LocalFolderPolicy:
    """Path-policy boundary for a local chapter folder.

    It is intentionally separate from the adapter so the policy and its Windows-specific
    reparse protections can be tested without starting a pipeline.
    """

    def __init__(
        self,
        allowed_roots: Iterable[str | Path] | None = None,
        *,
        allow_root_folder: bool = False,
    ) -> None:
        if allowed_roots is None:
            roots = configured_allowed_roots()
        else:
            roots = _resolve_explicit_roots(allowed_roots)
        self.allowed_roots = tuple(roots)
        self.allow_root_folder = bool(allow_root_folder)

    @classmethod
    def from_environment(cls) -> "LocalFolderPolicy":
        return cls()

    def validate_folder(self, folder: str | Path) -> Path:
        if not self.allowed_roots:
            raise LocalFolderError(LOCAL_INPUT_NOT_CONFIGURED, "no_allowed_roots")
        raw = str(folder or "").strip()
        _reject_raw_path(raw)
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise LocalFolderError(LOCAL_PATH_UNSUPPORTED, "path_not_absolute")
        if not path.exists() or not path.is_dir():
            raise LocalFolderError(LOCAL_PATH_NOT_FOUND, "folder_not_found")
        _ensure_no_reparse_components(path)
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise LocalFolderError(LOCAL_PATH_NOT_FOUND, "folder_unresolvable") from exc
        root = _containing_root(resolved, self.allowed_roots)
        if root is None:
            raise LocalFolderError(LOCAL_PATH_NOT_ALLOWED, "outside_allowed_root")
        if not self.allow_root_folder and resolved == root:
            raise LocalFolderError(LOCAL_FOLDER_ROOT_NOT_SELECTABLE, "root_not_chapter")
        # Resolve and containment-check again immediately before enumeration.  This is not
        # a substitute for OS ACLs against a hostile same-machine writer, but it catches a
        # normal symlink/junction swap before any source bytes are opened.
        _ensure_no_reparse_components(path)
        try:
            again = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise LocalFolderError(LOCAL_PATH_NOT_FOUND, "folder_changed") from exc
        if again != resolved or _containing_root(again, self.allowed_roots) is None:
            raise LocalFolderError(LOCAL_PATH_NOT_ALLOWED, "folder_changed")
        return resolved


class LocalFolderChapterAdapter:
    """A local-only adapter that validates and snapshots chapter page folders.

    The class exposes adapter metadata expected by an integration layer but deliberately
    does not inherit URL behaviour.  It never navigates, creates a browser, fetches a URL,
    or treats a local path as ``file://``.
    """

    name = LOCAL_FOLDER_ADAPTER_NAME
    adapter_version = LOCAL_FOLDER_ADAPTER_VERSION
    source_type = "local_folder"
    allowed_hosts: tuple[str, ...] = ()
    is_specific = True

    def __init__(
        self,
        policy: LocalFolderPolicy | None = None,
        *,
        limits: LocalFolderLimits | None = None,
    ) -> None:
        self.policy = policy or LocalFolderPolicy.from_environment()
        self.limits = limits or LocalFolderLimits()

    def supports(self, source: object) -> bool:
        """Return true only for a local path-shaped value, never any URL scheme."""

        if not isinstance(source, (str, Path)):
            return False
        value = str(source).strip()
        if re.match(r"^[A-Za-z]:[\\/]", value):
            return True                         # Windows drive path, not a URI scheme.
        return bool(value) and not re.match(r"^[a-z][a-z0-9+.-]*:", value.casefold())

    def validate_path(self, folder: str | Path) -> Path:
        return self.policy.validate_folder(folder)

    def analyze(self, folder: str | Path) -> LocalFolderAnalysis:
        source_folder = self.validate_path(folder)
        return self._scan(source_folder)

    # British spelling keeps parity with existing source-analysis terminology without
    # forcing the future integration layer to choose one spelling.
    analyse = analyze

    def snapshot(
        self,
        folder: str | Path,
        workspace_root: str | Path,
        *,
        snapshot_id: str | None = None,
    ) -> LocalFolderSnapshot:
        """Create an isolated stable-at-intake copy and a sanitised manifest.

        The original directory is only opened for reading.  Each source file is rechecked
        around its read, then written by generated ordinal name under a newly created child
        workspace.  A failed intake removes only that child workspace.
        """

        source_folder = self.validate_path(folder)
        root = _validate_workspace_root(workspace_root)
        if _paths_overlap(source_folder, root):
            raise LocalFolderError(LOCAL_WORKSPACE_OVERLAP, "source_workspace_overlap")
        identifier = snapshot_id or uuid.uuid4().hex
        if not _SNAPSHOT_ID_RE.fullmatch(identifier):
            raise LocalFolderError(LOCAL_WORKSPACE_INVALID, "invalid_snapshot_id")
        workspace = root / identifier
        try:
            workspace.mkdir(parents=False, exist_ok=False)
        except FileExistsError as exc:
            raise LocalFolderError(LOCAL_SNAPSHOT_CONFLICT, "workspace_exists") from exc
        except OSError as exc:
            raise LocalFolderError(LOCAL_WORKSPACE_INVALID, "workspace_create_failed") from exc

        try:
            analysis = self._scan(source_folder, snapshot_folder=workspace)
            manifest = self.build_page_manifest(analysis, snapshot_id=identifier)
            manifest_path = workspace / "manifest.json"
            _atomic_write_json(manifest_path, manifest)
            _ensure_no_reparse_components(workspace)
            return LocalFolderSnapshot(workspace=workspace, manifest_path=manifest_path,
                                       analysis=analysis)
        except Exception:
            _remove_owned_workspace(root, workspace)
            raise

    def build_page_manifest(
        self,
        analysis: LocalFolderAnalysis,
        *,
        snapshot_id: str,
    ) -> dict[str, object]:
        """Build the persistable, path-free page manifest for a validated snapshot."""

        if not isinstance(analysis, LocalFolderAnalysis):
            raise TypeError("analysis must be LocalFolderAnalysis")
        if not _SNAPSHOT_ID_RE.fullmatch(str(snapshot_id or "")):
            raise LocalFolderError(LOCAL_WORKSPACE_INVALID, "invalid_snapshot_id")
        return _snapshot_manifest(analysis, str(snapshot_id))

    def _scan(
        self,
        source_folder: Path,
        *,
        snapshot_folder: Path | None = None,
    ) -> LocalFolderAnalysis:
        # An intentional direct-folder policy: nested pages are ambiguous and make link
        # traversal harder to reason about.  Direct ordinary folders are ignored; any direct
        # reparse point fails the intake, even if it would not have been an image.
        entries: list[Path] = []
        try:
            folder_before = os.lstat(source_folder)
            direct_entries = []
            for entry in source_folder.iterdir():
                if len(direct_entries) >= HARD_MAX_DIRECTORY_ENTRIES:
                    raise LocalFolderError(LOCAL_INPUT_LIMIT, "max_directory_entries")
                direct_entries.append(entry)
        except OSError as exc:
            raise LocalFolderError(LOCAL_PATH_NOT_FOUND, "folder_unreadable") from exc
        for entry in direct_entries:
            if _is_reparse_point(entry):
                raise LocalFolderError(LOCAL_REPARSE_POINT, "entry_reparse_point")
            try:
                mode = os.lstat(entry).st_mode
            except OSError as exc:
                raise LocalFolderError(LOCAL_PATH_NOT_FOUND, "entry_unreadable") from exc
            if not stat.S_ISREG(mode):
                continue
            suffix = entry.suffix.casefold()
            if suffix in _DISALLOWED_IMAGE_EXTENSIONS:
                raise LocalFolderError(LOCAL_UNSUPPORTED_EXTENSION, "image_extension")
            if suffix in _ALLOWED_EXTENSION_FORMATS:
                entries.append(entry)

        entries.sort(key=natural_sort_key)
        if not entries:
            raise LocalFolderError(NO_CHAPTER_IMAGES, "no_allowed_local_images")
        if len(entries) > self.limits.max_files:
            raise LocalFolderError(LOCAL_INPUT_LIMIT, "max_files")

        pages: list[LocalFolderPage] = []
        duplicates = DuplicateTracker()
        total_bytes = 0
        for index, entry in enumerate(entries, start=1):
            _ensure_no_reparse_components(entry)
            data, before, after = _read_regular_file_bounded(
                entry,
                max_bytes=self.limits.max_bytes_per_file,
                remaining_total=self.limits.max_total_bytes - total_bytes,
            )
            if before != after:
                raise LocalFolderError(LOCAL_INVALID_IMAGE, "source_changed_during_read")
            total_bytes += len(data)
            if total_bytes > self.limits.max_total_bytes:
                raise LocalFolderError(LOCAL_INPUT_LIMIT, "max_total_bytes")
            page = _validate_local_page(entry, data, index)
            # ``DuplicateTracker`` is shared with the transport validator.  Duplicates are
            # deliberately terminal for local folders: dropping one would change the user
            # supplied page sequence without an explicit review action.
            validated = ValidatedImage(
                data=data, width=page.width, height=page.height,
                fmt=page.fmt, sha256=page.sha256,
            )
            if duplicates.is_duplicate(validated):
                raise LocalFolderError(LOCAL_INPUT_DUPLICATE, "duplicate_image_bytes")
            if snapshot_folder is not None:
                _ensure_no_reparse_components(snapshot_folder)
                destination = snapshot_folder / page.snapshot_filename
                _atomic_write_bytes(destination, data)
                _verify_snapshot_copy(destination, page.sha256)
            pages.append(page)

        try:
            folder_after = os.lstat(source_folder)
        except OSError as exc:
            raise LocalFolderError(LOCAL_PATH_NOT_FOUND, "folder_changed") from exc
        if _is_reparse_point(source_folder) or _file_identity(folder_before) != _file_identity(folder_after):
            raise LocalFolderError(LOCAL_INVALID_IMAGE, "folder_changed_during_intake")

        source_fingerprint = _fingerprint_path(source_folder)
        return LocalFolderAnalysis(
            source_folder=source_folder,
            source_fingerprint=source_fingerprint,
            pages=tuple(pages),
            total_bytes=total_bytes,
            limits=self.limits,
        )


def _resolve_explicit_roots(roots: Iterable[str | Path]) -> tuple[Path, ...]:
    resolved: list[Path] = []
    for raw_root in roots:
        raw = str(raw_root or "").strip()
        if not raw:
            continue
        _reject_raw_path(raw)
        root = Path(raw).expanduser()
        if not root.is_absolute() or not root.exists() or not root.is_dir():
            raise LocalFolderError(LOCAL_INPUT_NOT_CONFIGURED, "root_not_directory")
        _ensure_no_reparse_components(root)
        try:
            canonical = root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise LocalFolderError(LOCAL_INPUT_NOT_CONFIGURED, "root_unresolvable") from exc
        if canonical not in resolved:
            resolved.append(canonical)
    return tuple(resolved)


def _reject_raw_path(raw: str) -> None:
    if not raw or "\x00" in raw:
        raise LocalFolderError(LOCAL_PATH_UNSUPPORTED, "empty_or_nul")
    normalized = raw.replace("/", "\\")
    # UNC, verbatim and device namespace paths have different resolution rules from normal
    # local files.  Keeping them out avoids accidentally expanding the allowlist to a share
    # or a device object.
    if normalized.startswith("\\\\?\\") or normalized.startswith("\\\\.\\") or normalized.startswith("\\\\"):
        raise LocalFolderError(LOCAL_PATH_UNSUPPORTED, "unc_or_device_path")
    if any(part == ".." for part in re.split(r"[\\/]+", raw)):
        raise LocalFolderError(LOCAL_PATH_TRAVERSAL, "parent_segment")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", raw) and not re.match(r"^[A-Za-z]:[\\/]", raw):
        raise LocalFolderError(LOCAL_PATH_UNSUPPORTED, "url_scheme")


def _is_reparse_point(path: Path) -> bool:
    try:
        entry = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(entry.st_mode):
        return True
    attributes = int(getattr(entry, "st_file_attributes", 0) or 0)
    reparse_attribute = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_attribute)


def _ensure_no_reparse_components(path: Path) -> None:
    """Reject every existing component on the lexical path before resolving it."""

    try:
        parts = path.parts
    except TypeError as exc:
        raise LocalFolderError(LOCAL_PATH_UNSUPPORTED, "invalid_path") from exc
    if not parts:
        raise LocalFolderError(LOCAL_PATH_UNSUPPORTED, "invalid_path")
    current = Path(path.anchor) if path.anchor else Path(parts[0])
    start = 1 if path.anchor else 1
    for part in parts[start:]:
        current = current / part
        if _is_reparse_point(current):
            raise LocalFolderError(LOCAL_REPARSE_POINT, "reparse_component")


def _containing_root(path: Path, roots: Iterable[Path]) -> Path | None:
    for root in roots:
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return root
    return None


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _validate_workspace_root(workspace_root: str | Path) -> Path:
    raw = str(workspace_root or "").strip()
    _reject_raw_path(raw)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise LocalFolderError(LOCAL_WORKSPACE_INVALID, "workspace_not_absolute")
    parent = path if path.exists() else path.parent
    if not parent.exists() or not parent.is_dir():
        raise LocalFolderError(LOCAL_WORKSPACE_INVALID, "workspace_parent_missing")
    _ensure_no_reparse_components(parent)
    try:
        resolved_parent = parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LocalFolderError(LOCAL_WORKSPACE_INVALID, "workspace_parent_unresolvable") from exc
    if path.exists():
        if not path.is_dir():
            raise LocalFolderError(LOCAL_WORKSPACE_INVALID, "workspace_not_directory")
        _ensure_no_reparse_components(path)
        return path.resolve(strict=True)
    return resolved_parent / path.name


def _read_regular_file_bounded(
    path: Path,
    *,
    max_bytes: int,
    remaining_total: int,
) -> tuple[bytes, tuple[int, int, int, int], tuple[int, int, int, int]]:
    if remaining_total < 0:
        raise LocalFolderError(LOCAL_INPUT_LIMIT, "max_total_bytes")
    try:
        before_stat = os.lstat(path)
    except OSError as exc:
        raise LocalFolderError(LOCAL_PATH_NOT_FOUND, "file_unreadable") from exc
    if _is_reparse_point(path) or not stat.S_ISREG(before_stat.st_mode):
        raise LocalFolderError(LOCAL_REPARSE_POINT, "file_not_regular")
    if before_stat.st_size < 0 or before_stat.st_size > max_bytes:
        raise LocalFolderError(LOCAL_INPUT_LIMIT, "max_bytes_per_file")
    if before_stat.st_size > remaining_total:
        raise LocalFolderError(LOCAL_INPUT_LIMIT, "max_total_bytes")
    chunks: list[bytes] = []
    total = 0
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0) or 0)
    flags |= int(getattr(os, "O_NOFOLLOW", 0) or 0)
    try:
        descriptor = os.open(str(path), flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened_stat = os.fstat(handle.fileno())
            if (not stat.S_ISREG(opened_stat.st_mode)
                    or _file_identity(opened_stat) != _file_identity(before_stat)):
                raise LocalFolderError(LOCAL_INVALID_IMAGE, "source_changed_before_read")
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise LocalFolderError(LOCAL_INPUT_LIMIT, "max_bytes_per_file")
                if total > remaining_total:
                    raise LocalFolderError(LOCAL_INPUT_LIMIT, "max_total_bytes")
                chunks.append(chunk)
    except LocalFolderError:
        raise
    except OSError as exc:
        raise LocalFolderError(LOCAL_PATH_NOT_FOUND, "file_read_failed") from exc
    try:
        after_stat = os.lstat(path)
    except OSError as exc:
        raise LocalFolderError(LOCAL_INVALID_IMAGE, "source_changed_during_read") from exc
    if _is_reparse_point(path) or not stat.S_ISREG(after_stat.st_mode):
        raise LocalFolderError(LOCAL_REPARSE_POINT, "file_changed_to_reparse")
    return b"".join(chunks), _file_identity(before_stat), _file_identity(after_stat)


def _file_identity(entry: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(getattr(entry, "st_dev", 0) or 0),
        int(getattr(entry, "st_ino", 0) or 0),
        int(getattr(entry, "st_size", 0) or 0),
        int(getattr(entry, "st_mtime_ns", int(entry.st_mtime * 1_000_000_000))),
    )


def _validate_local_page(path: Path, data: bytes, order: int) -> LocalFolderPage:
    expected_format = _ALLOWED_EXTENSION_FORMATS[path.suffix.casefold()]
    try:
        validated = validate_image_bytes(
            data,
            # Match the pipeline's chapter-image tolerance: very short, real separator
            # strips are valid pages, but width must still be substantial.
            min_width=480,
            min_height=1,
            min_bytes=12,
            max_bytes=HARD_MAX_BYTES_PER_FILE,
        )
    except SourceError as exc:
        raise LocalFolderError(LOCAL_INVALID_IMAGE, "byte_validation") from exc
    if validated.fmt != expected_format:
        raise LocalFolderError(LOCAL_INVALID_IMAGE, "extension_format_mismatch")
    image_array = np.frombuffer(data, dtype=np.uint8)
    try:
        decoded = cv2.imdecode(image_array, cv2.IMREAD_UNCHANGED)
    except Exception as exc:  # pragma: no cover - backend-specific OpenCV failures
        raise LocalFolderError(LOCAL_INVALID_IMAGE, "opencv_decode") from exc
    if decoded is None or not getattr(decoded, "size", 0):
        raise LocalFolderError(LOCAL_INVALID_IMAGE, "opencv_decode")
    try:
        decoded_height, decoded_width = decoded.shape[:2]
    except (AttributeError, ValueError, TypeError) as exc:
        raise LocalFolderError(LOCAL_INVALID_IMAGE, "opencv_shape") from exc
    if (int(decoded_width), int(decoded_height)) != (validated.width, validated.height):
        raise LocalFolderError(LOCAL_INVALID_IMAGE, "opencv_dimensions")
    return LocalFolderPage(
        order=order,
        source_path=path,
        source_name=path.name,
        source_name_fingerprint=_fingerprint_name(path.name),
        sha256=validated.sha256,
        width=validated.width,
        height=validated.height,
        fmt=validated.fmt,
        mime_type=_IMAGE_MIME_BY_FORMAT[validated.fmt],
        byte_size=len(data),
    )


def _fingerprint_path(path: Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8", "surrogatepass")).hexdigest()[:24]


def _fingerprint_name(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8", "surrogatepass")).hexdigest()[:20]


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.remove(temporary_name)


def _verify_snapshot_copy(path: Path, expected_sha256: str) -> None:
    if _is_reparse_point(path):
        raise LocalFolderError(LOCAL_REPARSE_POINT, "snapshot_reparse_point")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise LocalFolderError(LOCAL_WORKSPACE_INVALID, "snapshot_unreadable") from exc
    if digest.hexdigest() != expected_sha256:
        raise LocalFolderError(LOCAL_WORKSPACE_INVALID, "snapshot_hash_mismatch")


def _snapshot_manifest(analysis: LocalFolderAnalysis, snapshot_id: str) -> dict[str, object]:
    pages = []
    for page in analysis.pages:
        item = page.public()
        item["filename"] = page.snapshot_filename
        # Per-page flag as well as the manifest-level one: the runner decides Smart Split
        # per page, and materialize_snapshot already emits it, so both producers agree.
        item["logical_page"] = True
        pages.append(item)
    return {
        "manifest_version": LOCAL_FOLDER_MANIFEST_VERSION,
        "source_type": "local_folder",
        "adapter_name": LOCAL_FOLDER_ADAPTER_NAME,
        "adapter_version": LOCAL_FOLDER_ADAPTER_VERSION,
        "snapshot_id": snapshot_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_fingerprint": analysis.source_fingerprint,
        "logical_pages": True,
        "requires_smart_split": False,
        "accepted_page_count": len(analysis.pages),
        "rejected_page_count": 0,
        "total_bytes": analysis.total_bytes,
        "limits": {
            "max_bytes_per_file": analysis.limits.max_bytes_per_file,
            "max_total_bytes": analysis.limits.max_total_bytes,
            "max_files": analysis.limits.max_files,
        },
        "pages": pages,
        "download_gate": {
            "passed": True,
            "reasons": [],
            "expected_viewer_images": len(analysis.pages),
            "total_viewer_images": len(analysis.pages),
            "downloaded_viewer_images": len(analysis.pages),
            "missing_viewer_images": 0,
            "order_monotonic": True,
        },
    }


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.remove(temporary_name)


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def _remove_owned_workspace(root: Path, workspace: Path) -> None:
    """Best-effort cleanup of only the direct child created by ``snapshot``."""

    try:
        if workspace.parent.resolve(strict=True) != root.resolve(strict=True):
            return
        if _is_reparse_point(workspace):
            return
        if workspace.is_dir():
            shutil.rmtree(workspace)
    except OSError:
        pass
