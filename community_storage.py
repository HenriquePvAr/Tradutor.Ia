"""Storage abstraction for community PDFs, decoupled from any specific backend.

The community owns its metadata in the database; a StorageProvider only holds the PDF
bytes. Posts, the feed and the read endpoint never import a concrete provider, so the
backend (Google Drive today) can be swapped for S3/R2 later without touching them.

Uploads are resumable and chunked so a large PDF is never loaded whole into memory: a
caller creates a session, streams chunks by offset, and the provider reports how many
bytes it has so an interrupted upload can continue from there.

This module defines the interface, the typed results, and an in-memory FakeStorageProvider
used by every test - no network, no credentials.
"""

from __future__ import annotations

import abc
import hashlib
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


class StorageError(RuntimeError):
    """A storage operation failed. ``transient`` marks errors worth retrying."""

    def __init__(self, message: str, *, transient: bool = False, status: int | None = None):
        super().__init__(message)
        self.transient = transient
        self.status = status


@dataclass
class RemoteFileMetadata:
    file_id: str
    name: str
    mime_type: str
    size: int
    parent_id: str = ""
    trashed: bool = False
    checksum: str = ""  # provider-supplied checksum when available (e.g. md5)


@dataclass
class ResumableSession:
    """Opaque, credential-free handle to an in-progress upload.

    ``session_id`` is a provider reference (an upload URL or token) that the caller
    persists to resume later. It must never carry secrets that could not be logged.
    """

    session_id: str
    filename: str
    mime_type: str
    total_size: int
    uploaded: int = 0
    file_id: str = ""
    parent_id: str = ""


@dataclass
class ChunkResult:
    uploaded: int
    completed: bool
    file_id: str = ""


@dataclass
class StorageStream:
    """A byte stream for reading, sized so the caller can serve Range requests."""

    total_size: int
    content_length: int
    mime_type: str
    _chunks: Iterator[bytes]
    start: int = 0
    end: int = 0

    def iter_chunks(self) -> Iterator[bytes]:
        return self._chunks


class StorageProvider(abc.ABC):
    name: str = "abstract"

    @abc.abstractmethod
    def health_check(self) -> bool: ...

    @abc.abstractmethod
    def ensure_folder(self, name: str, parent_id: str) -> str: ...

    @abc.abstractmethod
    def create_resumable_session(
        self, *, filename: str, mime_type: str, size: int, parent_id: str, sha256: str = ""
    ) -> ResumableSession: ...

    @abc.abstractmethod
    def upload_chunk(self, session: ResumableSession, offset: int, data: bytes) -> ChunkResult: ...

    def abandon_resumable_session(self, session_id: str) -> None:
        """Forget an incomplete session when supported; remote providers may expire it."""
        return None

    @abc.abstractmethod
    def stat_file(self, file_id: str) -> RemoteFileMetadata: ...

    @abc.abstractmethod
    def open_stream(self, file_id: str, *, start: int | None = None,
                    end: int | None = None) -> StorageStream: ...

    @abc.abstractmethod
    def move_to_trash(self, file_id: str) -> None: ...

    @abc.abstractmethod
    def delete_file(self, file_id: str) -> None: ...

    @abc.abstractmethod
    def exists(self, file_id: str) -> bool: ...


@dataclass
class _FakeFile:
    file_id: str
    name: str
    mime_type: str
    parent_id: str
    data: bytearray = field(default_factory=bytearray)
    completed: bool = False
    trashed: bool = False


class FakeStorageProvider(StorageProvider):
    """In-memory provider for tests: real chunked/resumable semantics, no network.

    Configurable to be offline, to fail a number of chunk attempts transiently, or to
    corrupt the stored size - so the resilience paths (retry, verification mismatch,
    provider down) can be exercised deterministically.
    """

    name = "fake"

    def __init__(self, *, online: bool = True, transient_failures: int = 0,
                 corrupt_size: bool = False):
        self.online = online
        self._transient_left = transient_failures
        self.corrupt_size = corrupt_size
        self._files: dict[str, _FakeFile] = {}
        self._sessions: dict[str, ResumableSession] = {}
        self._folders: dict[tuple[str, str], str] = {}
        self._counter = 0
        self._lock = threading.Lock()

    def _next_id(self, prefix: str) -> str:
        with self._lock:
            self._counter += 1
            return f"{prefix}_{self._counter}"

    def _require_online(self) -> None:
        if not self.online:
            raise StorageError("provider offline", transient=True, status=503)

    def health_check(self) -> bool:
        return self.online

    def ensure_folder(self, name: str, parent_id: str) -> str:
        self._require_online()
        key = (parent_id, name)
        if key not in self._folders:
            self._folders[key] = self._next_id("folder")
        return self._folders[key]

    def create_resumable_session(self, *, filename, mime_type, size, parent_id, sha256="") -> ResumableSession:
        self._require_online()
        file_id = self._next_id("file")
        self._files[file_id] = _FakeFile(file_id, filename, mime_type, parent_id)
        session = ResumableSession(
            session_id=self._next_id("session"), filename=filename, mime_type=mime_type,
            total_size=size, uploaded=0, file_id=file_id, parent_id=parent_id,
        )
        self._sessions[session.session_id] = session
        return session

    def upload_chunk(self, session: ResumableSession, offset: int, data: bytes) -> ChunkResult:
        self._require_online()
        stored = self._sessions.get(session.session_id)
        if stored is None:
            raise StorageError("unknown or expired upload session", status=404)
        if self._transient_left > 0:
            self._transient_left -= 1
            raise StorageError("temporary upstream error", transient=True, status=503)
        if offset != stored.uploaded:
            raise StorageError(f"out-of-order chunk: got {offset}, expected {stored.uploaded}", status=400)
        blob = self._files[stored.file_id]
        blob.data.extend(data)
        stored.uploaded += len(data)
        session.uploaded = stored.uploaded
        completed = stored.uploaded >= stored.total_size
        if completed:
            blob.completed = True
        return ChunkResult(uploaded=stored.uploaded, completed=completed,
                           file_id=stored.file_id if completed else "")

    def abandon_resumable_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def stat_file(self, file_id: str) -> RemoteFileMetadata:
        self._require_online()
        blob = self._files.get(file_id)
        if blob is None:
            raise StorageError("file not found", status=404)
        size = len(blob.data)
        if self.corrupt_size:
            size = max(0, size - 1)
        return RemoteFileMetadata(
            file_id=blob.file_id, name=blob.name, mime_type=blob.mime_type, size=size,
            parent_id=blob.parent_id, trashed=blob.trashed,
            checksum=hashlib.md5(bytes(blob.data)).hexdigest(),
        )

    def open_stream(self, file_id, *, start=None, end=None) -> StorageStream:
        self._require_online()
        blob = self._files.get(file_id)
        if blob is None or blob.trashed:
            raise StorageError("file not found", status=404)
        total = len(blob.data)
        lo = 0 if start is None else max(0, int(start))
        hi = total - 1 if end is None else min(total - 1, int(end))
        if lo > hi:
            raise StorageError("invalid range", status=416)
        payload = bytes(blob.data[lo:hi + 1])

        def _gen(chunk=64 * 1024):
            for i in range(0, len(payload), chunk):
                yield payload[i:i + chunk]

        return StorageStream(total_size=total, content_length=len(payload),
                             mime_type=blob.mime_type, _chunks=_gen(), start=lo, end=hi)

    def move_to_trash(self, file_id: str) -> None:
        self._require_online()
        blob = self._files.get(file_id)
        if blob:
            blob.trashed = True

    def delete_file(self, file_id: str) -> None:
        self._require_online()
        self._files.pop(file_id, None)

    def exists(self, file_id: str) -> bool:
        self._require_online()
        blob = self._files.get(file_id)
        return bool(blob and not blob.trashed)

    # Test helper: the actual stored bytes, to assert integrity.
    def _stored_bytes(self, file_id: str) -> bytes:
        return bytes(self._files[file_id].data)


class FilesystemStorageProvider(StorageProvider):
    """A fake provider that persists to a local directory, so a runner subprocess and the
    test share its state - used by the cross-process survival/recovery tests. Still no
    network and no credentials; the directory stands in for the remote drive.
    """

    name = "filesystem"

    def __init__(self, root: str | Path):
        self.root = Path(root)
        (self.root / "files").mkdir(parents=True, exist_ok=True)
        (self.root / "sessions").mkdir(parents=True, exist_ok=True)

    def _meta_path(self, file_id: str) -> Path:
        return self.root / "files" / f"{file_id}.json"

    def _data_path(self, file_id: str) -> Path:
        return self.root / "files" / f"{file_id}.bin"

    def _session_path(self, sid: str) -> Path:
        return self.root / "sessions" / f"{sid}.json"

    @staticmethod
    def _load(path: Path) -> dict:
        import json
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    @staticmethod
    def _save(path: Path, payload: dict) -> None:
        import json
        import os
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)

    def health_check(self) -> bool:
        return self.root.is_dir()

    def ensure_folder(self, name: str, parent_id: str) -> str:
        folder = f"folder_{parent_id}_{name}".replace("/", "_")
        return folder

    def create_resumable_session(self, *, filename, mime_type, size, parent_id, sha256="") -> ResumableSession:
        import uuid
        file_id = uuid.uuid4().hex
        self._save(self._meta_path(file_id), {
            "file_id": file_id, "name": filename, "mime_type": mime_type,
            "parent_id": parent_id, "completed": False, "trashed": False, "size": 0})
        self._data_path(file_id).write_bytes(b"")
        sid = uuid.uuid4().hex
        session = ResumableSession(session_id=sid, filename=filename, mime_type=mime_type,
                                   total_size=size, uploaded=0, file_id=file_id, parent_id=parent_id)
        self._save(self._session_path(sid), {"file_id": file_id, "uploaded": 0, "total": size})
        return session

    def upload_chunk(self, session: ResumableSession, offset: int, data: bytes) -> ChunkResult:
        s = self._load(self._session_path(session.session_id))
        if not s:
            raise StorageError("unknown or expired upload session", status=404)
        if offset != s["uploaded"]:
            raise StorageError(f"out-of-order chunk: got {offset}, expected {s['uploaded']}", status=400)
        with self._data_path(s["file_id"]).open("ab") as handle:
            handle.write(data)
        s["uploaded"] += len(data)
        self._save(self._session_path(session.session_id), s)
        session.uploaded = s["uploaded"]
        completed = s["uploaded"] >= s["total"]
        if completed:
            meta = self._load(self._meta_path(s["file_id"]))
            meta["completed"] = True
            meta["size"] = s["uploaded"]
            self._save(self._meta_path(s["file_id"]), meta)
        return ChunkResult(uploaded=s["uploaded"], completed=completed,
                           file_id=s["file_id"] if completed else "")

    def abandon_resumable_session(self, session_id: str) -> None:
        self._session_path(session_id).unlink(missing_ok=True)

    def stat_file(self, file_id: str) -> RemoteFileMetadata:
        meta = self._load(self._meta_path(file_id))
        if not meta:
            raise StorageError("file not found", status=404)
        data = self._data_path(file_id).read_bytes() if self._data_path(file_id).is_file() else b""
        return RemoteFileMetadata(
            file_id=file_id, name=meta["name"], mime_type=meta["mime_type"], size=len(data),
            parent_id=meta.get("parent_id", ""), trashed=meta.get("trashed", False),
            checksum=hashlib.md5(data).hexdigest())

    def open_stream(self, file_id, *, start=None, end=None) -> StorageStream:
        meta = self._load(self._meta_path(file_id))
        if not meta or meta.get("trashed"):
            raise StorageError("file not found", status=404)
        data = self._data_path(file_id).read_bytes()
        total = len(data)
        lo = 0 if start is None else max(0, int(start))
        hi = total - 1 if end is None else min(total - 1, int(end))
        if lo > hi:
            raise StorageError("invalid range", status=416)
        payload = data[lo:hi + 1]

        def _gen(chunk=64 * 1024):
            for i in range(0, len(payload), chunk):
                yield payload[i:i + chunk]

        return StorageStream(total_size=total, content_length=len(payload),
                             mime_type=meta["mime_type"], _chunks=_gen(), start=lo, end=hi)

    def move_to_trash(self, file_id: str) -> None:
        meta = self._load(self._meta_path(file_id))
        if meta:
            meta["trashed"] = True
            self._save(self._meta_path(file_id), meta)

    def delete_file(self, file_id: str) -> None:
        self._meta_path(file_id).unlink(missing_ok=True)
        self._data_path(file_id).unlink(missing_ok=True)

    def exists(self, file_id: str) -> bool:
        meta = self._load(self._meta_path(file_id))
        return bool(meta and not meta.get("trashed"))


def build_storage_provider(config: dict) -> StorageProvider:
    """Build the configured provider. Only fakes are constructible without credentials;
    the Google provider is built by its own module when configured with real OAuth."""
    name = (config or {}).get("storage_provider") or "fake"
    if name in {"fake", "memory"}:
        return FakeStorageProvider()
    if name == "filesystem":
        return FilesystemStorageProvider(config["storage_root"])
    if name == "google_drive":
        # Secrets come from the environment/token file, never from the passed config
        # (which is persisted with the job in the database).
        from google_drive_factory import GoogleDriveConfig, build_google_drive_provider
        gconfig = GoogleDriveConfig.from_env(
            root_folder_id_override=(config or {}).get("root_folder_id", ""))
        return build_google_drive_provider(gconfig)
    raise StorageError(f"unknown or unconfigured storage provider: {name}")
