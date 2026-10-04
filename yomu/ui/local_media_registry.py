"""Loopback-only registry for previews of user-approved local chapter images.

The browser receives opaque ids and same-origin URLs only.  Absolute paths stay in
this process and can only be introduced by the native picker, never by HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import secrets
import threading
from typing import Iterable

from PIL import Image, UnidentifiedImageError

SUPPORTED_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
MAX_MEDIA_BYTES = 80 * 1024 * 1024


@dataclass(frozen=True)
class LocalMedia:
    media_id: str
    path: Path
    mime: str
    size: int
    width: int
    height: int
    selection_id: str
    display_name: str


class LocalMediaRegistry:
    """A non-enumerable, in-memory set of native-picker authorized images."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._media: dict[str, LocalMedia] = {}
        self._selections: dict[str, tuple[str, ...]] = {}

    @staticmethod
    def _approved_path(raw: str | Path, *, root: Path | None = None) -> tuple[Path, str, int, int, int]:
        path = Path(raw).resolve(strict=True)
        if not path.is_file():
            raise ValueError("local_media_not_file")
        if root is not None:
            try:
                path.relative_to(root.resolve(strict=True))
            except ValueError as exc:
                raise ValueError("local_media_outside_selected_folder") from exc
        mime = SUPPORTED_MIME.get(path.suffix.casefold())
        if not mime:
            raise ValueError("local_media_unsupported")
        size = path.stat().st_size
        if size <= 0 or size > MAX_MEDIA_BYTES:
            raise ValueError("local_media_size_invalid")
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                width, height = image.size
        except (OSError, UnidentifiedImageError, ValueError) as exc:
            raise ValueError("local_media_decode_invalid") from exc
        if width <= 0 or height <= 0:
            raise ValueError("local_media_dimensions_invalid")
        return path, mime, size, width, height

    def register_selection(self, paths: Iterable[str | Path], *, folder: str | Path | None = None) -> dict[str, object]:
        root = Path(folder).resolve(strict=True) if folder is not None else None
        selection_id = secrets.token_urlsafe(24)
        records: list[LocalMedia] = []
        seen: set[Path] = set()
        for raw in paths:
            path, mime, size, width, height = self._approved_path(raw, root=root)
            if path in seen:
                continue
            seen.add(path)
            records.append(LocalMedia(
                media_id=secrets.token_urlsafe(24), path=path, mime=mime, size=size,
                width=width, height=height, selection_id=selection_id, display_name=path.name,
            ))
        with self._lock:
            self._selections[selection_id] = tuple(record.media_id for record in records)
            self._media.update({record.media_id: record for record in records})
        return {
            "selection_id": selection_id,
            "items": [self.public(record) for record in records],
        }

    @staticmethod
    def public(record: LocalMedia) -> dict[str, object]:
        base = f"/api/local-media/{record.media_id}"
        return {
            "media_id": record.media_id,
            "name": record.display_name,
            "thumbnail_url": f"{base}/thumbnail",
            "preview_url": base,
            "width": record.width,
            "height": record.height,
        }

    def get(self, media_id: str) -> LocalMedia | None:
        with self._lock:
            return self._media.get(str(media_id))

    def ordered_paths(self, selection_id: str, media_ids: Iterable[str]) -> list[str]:
        requested = [str(value) for value in media_ids]
        with self._lock:
            allowed = set(self._selections.get(str(selection_id), ()))
            if not allowed or len(requested) != len(set(requested)) or any(item not in allowed for item in requested):
                raise ValueError("local_media_selection_invalid")
            records = [self._media.get(item) for item in requested]
        if any(record is None for record in records):
            raise ValueError("local_media_selection_revoked")
        return [str(record.path) for record in records if record is not None]

    def ordered_paths_many(self, selection_ids: Iterable[str], media_ids: Iterable[str]) -> list[str]:
        """Resolve an ordered UI list across one or more native picker selections."""
        requested = [str(value) for value in media_ids]
        selection_set = {str(value) for value in selection_ids}
        with self._lock:
            allowed = {
                media_id for selection_id in selection_set
                for media_id in self._selections.get(selection_id, ())
            }
            if not allowed or len(requested) != len(set(requested)) or any(item not in allowed for item in requested):
                raise ValueError("local_media_selection_invalid")
            records = [self._media.get(item) for item in requested]
        if any(record is None for record in records):
            raise ValueError("local_media_selection_revoked")
        return [str(record.path) for record in records if record is not None]

    def revoke(self, selection_id: str) -> bool:
        with self._lock:
            identifiers = self._selections.pop(str(selection_id), None)
            if identifiers is None:
                return False
            for media_id in identifiers:
                self._media.pop(media_id, None)
            return True

    def clear(self) -> None:
        """Drop all native-picker capabilities owned by this server instance."""
        with self._lock:
            self._media.clear()
            self._selections.clear()

    def thumbnail_bytes(self, media: LocalMedia, *, maximum: int = 320) -> bytes:
        with Image.open(media.path) as image:
            image.thumbnail((maximum, maximum))
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if "transparency" in image.info else "RGB")
            output = BytesIO()
            image.save(output, format="PNG", optimize=True)
            return output.getvalue()


LOCAL_MEDIA_REGISTRY = LocalMediaRegistry()
