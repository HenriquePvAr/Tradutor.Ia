"""Contract tests for local removal of the obsolete profile-media runtime."""

from pathlib import Path

import app_ui
from ui_bridge import UiBridge

ROOT = Path(__file__).resolve().parent


def test_profile_media_routes_bridge_and_fetchers_are_absent():
    app_source = (ROOT / "app_ui.py").read_text(encoding="utf-8")
    bridge_source = (ROOT / "ui_bridge.py").read_text(encoding="utf-8")
    for source in (app_source, bridge_source):
        for marker in (
            "profile_media", "profile-media", "avatar_object_key", "banner_object_key",
            "profile_media_stream", "save_profile_media", "profile_media_provider_factory",
        ):
            assert marker not in source

    media_routes = [
        getattr(route, "path", "") for route in app_ui.app.routes
        if any(term in getattr(route, "path", "").casefold()
               for term in ("profile/media", "profile-media", "/avatar", "/banner"))
    ]
    assert media_routes == []

    bridge = UiBridge.__new__(UiBridge)
    for name in (
        "profile", "profile_root", "profile_media_root", "save_profile",
        "profile_for_user", "save_profile_media", "remove_profile_media",
        "profile_media_path", "profile_media_stream", "profile_media_provider_factory",
    ):
        assert not hasattr(bridge, name)


def test_profile_media_ui_upload_and_hydration_are_absent():
    shell = (ROOT / "ui" / "ui_shell.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "tradutor_ui.css").read_text(encoding="utf-8")
    for marker in ('id="view-profile"', 'id="avatarImageInput"', 'id="bannerImageInput"',
                   'data-media-kind="avatar"', 'data-media-kind="banner"'):
        assert marker not in shell
    for marker in ("uploadProfileMedia", "removeProfileMedia", "loadAuthenticatedMediaElement",
                   "/api/ui/profile/media", "/api/community/profiles/", "/functions/v1/profile-media"):
        assert marker not in js
    assert "community-author-avatar" not in css
    assert "profile_media" not in css


def test_community_card_uses_identity_without_remote_media_url():
    from community_service import CommunityService

    card = CommunityService._card({
        "id": "post-1", "user_id": "user-1", "series_title": "Series",
        "series_slug": "series", "episode_number": "1", "title": "Chapter",
        "description": "", "tags": [], "cover_reference": "", "status": "published",
        "views": 0, "published_at": "2026-01-01", "author_display_name": "Reader",
        "author_public_role": "", "author_avatar_object_key": "legacy:ignored",
    })
    assert card["author"]["display_name"] == "Reader"
    assert "avatar_url" not in card["author"]
    assert "object_key" not in card["author"]
