"""Authenticated streaming of a social chapter's private PDF.

Authorization always precedes storage: the chapter's visibility is checked with the user's
own JWT (Supabase RLS) and the asset link is resolved before any Google Drive call. A
denied request (anonymous, invalid token, other user on a private chapter, missing/unlinked
asset) never touches Drive. The Drive file id, filename, path and checksum are never
returned — only the PDF bytes stream through the backend's own Drive token.
"""

from __future__ import annotations

from typing import Any

from community_api import RangeNotSatisfiable, _parse_range, build_read_provider
from community_auth import AuthenticationRequired, RequestPrincipal
from chapter_asset_repository import AssetNotFound, ChapterAssetError


class SocialContentService:
    def __init__(self, social_repo, asset_repo, *, read_provider_factory=None):
        self._social = social_repo
        self._assets = asset_repo
        self._read_provider_factory = read_provider_factory or build_read_provider

    def _authorize(self, token: str, principal: RequestPrincipal, chapter_id: str) -> dict[str, Any]:
        """Enforce chapter visibility via the user's token (RLS), then resolve the asset.

        Both steps run BEFORE any storage provider is constructed. Any failure raises before
        Drive is touched. Not-found/invisible collapses to AssetNotFound (→404).
        """
        if not isinstance(principal, RequestPrincipal) or not principal.authenticated:
            raise AuthenticationRequired("authentication_required")
        # Supabase RLS decides visibility: owner sees own draft/private/community; other
        # authenticated members see only community; a hidden chapter raises (→404).
        self._social.get_chapter(token, chapter_id)
        try:
            return self._assets.get_readable_file(chapter_id)
        except ChapterAssetError as exc:
            raise AssetNotFound() from exc

    def head_content(self, token: str, principal: RequestPrincipal, chapter_id: str) -> dict[str, Any]:
        file = self._authorize(token, principal, chapter_id)
        return {"mime_type": "application/pdf", "total_size": int(file["size_bytes"]),
                "filename": "capitulo.pdf"}

    def open_content(self, token: str, principal: RequestPrincipal, chapter_id: str,
                     *, range_header: str = ""):
        file = self._authorize(token, principal, chapter_id)
        total = int(file["size_bytes"])
        start, end = _parse_range(range_header, total)  # raises RangeNotSatisfiable (→416)
        # Only now — after authorization + asset resolution — do we touch Drive.
        provider = self._read_provider_factory()
        stream = provider.open_stream(file["storage_file_id"], start=start, end=end)
        return {
            "mime_type": "application/pdf",
            "filename": "capitulo.pdf",
            "total_size": total,
            "start": stream.start,
            "end": stream.end,
            "content_length": stream.content_length,
            "partial": start is not None,
        }, stream


__all__ = ["SocialContentService", "RangeNotSatisfiable"]
