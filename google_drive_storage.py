"""Google Drive implementation of the community StorageProvider.

Keeps files private in a configured root folder of the administrator's Drive - never
sets an "anyone" permission and never returns a Drive link. Uploads use Drive's resumable
protocol (initiate a session, PUT chunks with Content-Range, 308 while incomplete, 200/201
on completion), so a large PDF is streamed in chunks and an interrupted upload can resume
from the byte offset Drive reports.

The HTTP transport and the credentials are injected, so every path is testable without a
real network or OAuth: tests pass a fake transport and a fake token source. Credentials
(access/refresh tokens, client secret) are never logged and never stored in the community
database.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Protocol

from community_storage import (
    ChunkResult, RemoteFileMetadata, ResumableSession, StorageError,
    StorageProvider, StorageStream,
)

DRIVE_FILES = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    content: bytes = b""
    _iter: Iterator[bytes] | None = None

    def json(self) -> dict[str, Any]:
        import json
        return json.loads(self.content.decode("utf-8")) if self.content else {}

    def iter_content(self, chunk_size: int = 65536) -> Iterator[bytes]:
        if self._iter is not None:
            return self._iter
        data = self.content

        def _gen():
            for i in range(0, len(data), chunk_size):
                yield data[i:i + chunk_size]
        return _gen()


class HttpTransport(Protocol):
    def request(self, method: str, url: str, *, headers: dict | None = None,
                data: bytes | None = None, stream: bool = False) -> HttpResponse: ...


class TokenSource(Protocol):
    def access_token(self) -> str: ...
    def refresh(self) -> str: ...


class GoogleDriveStorageProvider(StorageProvider):
    name = "google_drive"

    def __init__(self, *, transport: HttpTransport, tokens: TokenSource,
                 root_folder_id: str, chunk_size: int = 8 * 1024 * 1024):
        self._t = transport
        self._tokens = tokens
        self.root_folder_id = root_folder_id
        self.chunk_size = chunk_size
        self._folder_cache: dict[tuple[str, str], str] = {}

    # ---- request helper with refresh + backoff ------------------------------
    def _request(self, method: str, url: str, *, headers: dict | None = None,
                 data: bytes | None = None, stream: bool = False,
                 allow_statuses: tuple[int, ...] = ()) -> HttpResponse:
        headers = dict(headers or {})
        refreshed = False
        for attempt in range(MAX_RETRIES + 1):
            headers["Authorization"] = f"Bearer {self._tokens.access_token()}"
            resp = self._t.request(method, url, headers=headers, data=data, stream=stream)
            if resp.status == 401 and not refreshed:
                self._tokens.refresh()
                refreshed = True
                continue
            if resp.status in RETRY_STATUSES and attempt < MAX_RETRIES:
                self._sleep_backoff(attempt, resp)
                continue
            if 200 <= resp.status < 300 or resp.status in allow_statuses:
                return resp
            transient = resp.status in RETRY_STATUSES
            raise StorageError(f"drive {method} {url} -> {resp.status}",
                               transient=transient, status=resp.status)
        raise StorageError(f"drive {method} exhausted retries", transient=True)

    @staticmethod
    def _sleep_backoff(attempt: int, resp: HttpResponse) -> None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            time.sleep(min(30, int(retry_after)))
            return
        time.sleep(min(30.0, (2 ** attempt) * 0.2) + random.uniform(0, 0.2))

    # ---- provider API -------------------------------------------------------
    def health_check(self) -> bool:
        try:
            self._request("GET", f"{DRIVE_FILES}/{self.root_folder_id}?fields=id")
            return True
        except StorageError:
            return False

    def ensure_folder(self, name: str, parent_id: str) -> str:
        import json
        parent = parent_id or self.root_folder_id
        key = (parent, name)
        if key in self._folder_cache:
            return self._folder_cache[key]
        # Look up an existing child folder by name, then create it if missing.
        safe = name.replace("'", r"\'")
        q = (f"name='{safe}' and mimeType='application/vnd.google-apps.folder' "
             f"and '{parent}' in parents and trashed=false")
        resp = self._request("GET", f"{DRIVE_FILES}?q={_urlencode(q)}&fields=files(id,name)")
        files = resp.json().get("files", [])
        if files:
            folder_id = files[0]["id"]
        else:
            body = json.dumps({"name": name, "mimeType": "application/vnd.google-apps.folder",
                               "parents": [parent]}).encode("utf-8")
            created = self._request("POST", f"{DRIVE_FILES}?fields=id",
                                    headers={"Content-Type": "application/json"}, data=body)
            folder_id = created.json()["id"]
        self._folder_cache[key] = folder_id
        return folder_id

    def create_resumable_session(self, *, filename, mime_type, size, parent_id, sha256="") -> ResumableSession:
        import json
        metadata = json.dumps({"name": filename, "parents": [parent_id or self.root_folder_id]})
        resp = self._request(
            "POST", DRIVE_UPLOAD,
            headers={"Content-Type": "application/json; charset=UTF-8",
                     "X-Upload-Content-Type": mime_type,
                     "X-Upload-Content-Length": str(size)},
            data=metadata.encode("utf-8"), allow_statuses=(200,))
        location = resp.headers.get("Location") or resp.headers.get("location")
        if not location:
            raise StorageError("drive resumable session had no Location")
        return ResumableSession(session_id=location, filename=filename, mime_type=mime_type,
                                total_size=size, uploaded=0, parent_id=parent_id)

    def upload_chunk(self, session: ResumableSession, offset: int, data: bytes) -> ChunkResult:
        total = session.total_size
        end = offset + len(data) - 1
        headers = {"Content-Length": str(len(data)),
                   "Content-Range": f"bytes {offset}-{end}/{total}"}
        resp = self._request("PUT", session.session_id, headers=headers, data=data,
                             allow_statuses=(200, 201, 308))
        if resp.status in (200, 201):
            file_id = resp.json().get("id", "")
            session.uploaded = total
            return ChunkResult(uploaded=total, completed=True, file_id=file_id)
        # 308: Drive reports the highest received byte in the Range header.
        rng = resp.headers.get("Range") or resp.headers.get("range") or ""
        uploaded = offset + len(data)
        if rng.startswith("bytes=0-"):
            try:
                uploaded = int(rng.split("-", 1)[1]) + 1
            except (ValueError, IndexError):
                pass
        session.uploaded = uploaded
        return ChunkResult(uploaded=uploaded, completed=False)

    def stat_file(self, file_id: str) -> RemoteFileMetadata:
        resp = self._request(
            "GET", f"{DRIVE_FILES}/{file_id}?fields=id,name,mimeType,size,parents,trashed,md5Checksum")
        data = resp.json()
        return RemoteFileMetadata(
            file_id=data.get("id", file_id), name=data.get("name", ""),
            mime_type=data.get("mimeType", ""), size=int(data.get("size") or 0),
            parent_id=(data.get("parents") or [""])[0], trashed=bool(data.get("trashed")),
            checksum=data.get("md5Checksum", ""))

    def open_stream(self, file_id, *, start=None, end=None) -> StorageStream:
        meta = self.stat_file(file_id)
        headers = {}
        if start is not None or end is not None:
            lo = 0 if start is None else int(start)
            hi = meta.size - 1 if end is None else int(end)
            headers["Range"] = f"bytes={lo}-{hi}"
        else:
            lo, hi = 0, meta.size - 1
        resp = self._request("GET", f"{DRIVE_FILES}/{file_id}?alt=media",
                             headers=headers, stream=True, allow_statuses=(200, 206))
        length = (hi - lo + 1)
        return StorageStream(total_size=meta.size, content_length=length,
                             mime_type=meta.mime_type or "application/pdf",
                             _chunks=resp.iter_content(), start=lo, end=hi)

    def move_to_trash(self, file_id: str) -> None:
        import json
        self._request("PATCH", f"{DRIVE_FILES}/{file_id}",
                      headers={"Content-Type": "application/json"},
                      data=json.dumps({"trashed": True}).encode("utf-8"))

    def delete_file(self, file_id: str) -> None:
        self._request("DELETE", f"{DRIVE_FILES}/{file_id}", allow_statuses=(204,))

    def exists(self, file_id: str) -> bool:
        try:
            return not self.stat_file(file_id).trashed
        except StorageError as exc:
            if exc.status == 404:
                return False
            raise


def _urlencode(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")
