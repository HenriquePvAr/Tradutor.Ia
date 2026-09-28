"""Egress hotfix: Drive-backed avatar/banner must be downloaded once per object_key.

Physical audit of Supabase logs showed the packaged desktop re-downloaded the same
avatar (~1.73 MB) and banner (~1.92 MB) on every UI render/poll via
``/functions/v1/profile-media`` (Cache-Control: no-store), producing ~97% of the egress
that exhausted the Free tier. The local route now caches the bytes on disk under their
``drive:<id>`` object_key and serves repeats from disk with zero remote fetches.

These tests drive the real ``app_ui.api_profile_media`` route with a counting fake for
the remote fetch, and assert the fetch count across renders/polls and key changes.
"""
from pathlib import Path
from types import SimpleNamespace

import app_ui
from community_auth import RequestPrincipal


def _png(filler: int = 4000) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * filler


class _Resp:
    def __init__(self, data: bytes, ctype: str = "image/png"):
        self._data = data
        self.headers = SimpleNamespace(get_content_type=lambda: ctype)

    def read(self, _n: int = -1) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _Counter:
    def __init__(self, data: bytes):
        self.calls = 0
        self.data = data

    def __call__(self, *_a, **_k):
        self.calls += 1
        return _Resp(self.data)


def _principal(user_id: str) -> RequestPrincipal:
    return RequestPrincipal(user_id=user_id, authenticated=True,
                            roles=frozenset({"user"}), auth_source="local_test",
                            session_id="session-1")


def _wire(monkeypatch, tmp_path, refs: dict, user: str = "user-a"):
    """Point the route at a temp cache + a fake remote; return the fetch counter."""
    state = {"refs": dict(refs)}
    bridge = SimpleNamespace(
        runtime_root=Path(tmp_path),
        profile_for_user=lambda uid: {
            "avatar_media_path": state["refs"].get("avatar", ""),
            "banner_media_path": state["refs"].get("banner", ""),
            "avatar_media_type": "image/png", "banner_media_type": "image/png",
        },
        profile_media_path=lambda kind, user_id="": None,
        profile_media_stream=lambda kind, user_id="": None,
    )
    monkeypatch.setattr(app_ui, "BRIDGE", bridge)
    monkeypatch.setattr(app_ui, "_ui_principal", lambda req, **k: _principal(user))
    monkeypatch.setattr(app_ui, "_license_bearer_token", lambda req: "tok")
    # Skip the remote profile-metadata enrichment so the local object_key is authoritative.
    monkeypatch.setattr(app_ui, "_SOCIAL_STATUS", {"provider": "none", "available": False})
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setattr(app_ui.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("1.2.3.4", 443))])
    monkeypatch.setattr(app_ui, "_https_context", lambda: None)
    counter = _Counter(_png())
    monkeypatch.setattr(app_ui.urllib_request, "urlopen", counter)
    app_ui._PROFILE_MEDIA_UPSTREAM_BACKOFF.clear()
    return counter, state


def _get(kind: str):
    return app_ui.api_profile_media(object(), kind)


def test_first_fetch_then_repeats_serve_from_cache(monkeypatch, tmp_path):
    counter, _ = _wire(monkeypatch, tmp_path, {"avatar": "drive:FILE1"})
    r0 = _get("avatar")
    assert r0.status_code == 200 and counter.calls == 1          # first access hits remote once
    for _ in range(100):
        r = _get("avatar")
        assert r.status_code == 200
    assert counter.calls == 1                                    # 100 re-renders: zero extra egress


def test_object_key_change_refetches_only_that_asset(monkeypatch, tmp_path):
    counter, state = _wire(monkeypatch, tmp_path, {"avatar": "drive:A1", "banner": "drive:B1"})
    _get("avatar"); _get("banner")
    assert counter.calls == 2
    for _ in range(10):
        _get("avatar"); _get("banner")
    assert counter.calls == 2                                    # stable: all from cache
    state["refs"]["avatar"] = "drive:A2"                         # avatar changed, banner same
    _get("avatar"); _get("banner")
    assert counter.calls == 3                                    # +1 avatar only, banner still cached


def test_account_isolation(monkeypatch, tmp_path):
    counter, _ = _wire(monkeypatch, tmp_path, {"avatar": "drive:SHARED"}, user="user-a")
    _get("avatar")
    assert counter.calls == 1
    # Same object_key, different user -> different cache key -> must fetch, never reuse A's file.
    counter2, _ = _wire(monkeypatch, tmp_path, {"avatar": "drive:SHARED"}, user="user-b")
    _get("avatar")
    assert counter2.calls == 1


def test_corrupt_cache_entry_refetches(monkeypatch, tmp_path):
    counter, _ = _wire(monkeypatch, tmp_path, {"avatar": "drive:FILE1"})
    _get("avatar")
    assert counter.calls == 1
    # Corrupt the cached bytes (not a valid image) -> next read is a miss -> one refetch.
    path = app_ui._profile_media_cache_path("user-a", "avatar", "drive:FILE1")
    path.write_bytes(b"not-an-image-garbage")
    _get("avatar")
    assert counter.calls == 2


def test_sixty_minute_polling_simulation_reduction(monkeypatch, tmp_path):
    counter, _ = _wire(monkeypatch, tmp_path, {"avatar": "drive:FILE1", "banner": "drive:B1"})
    # 60 minutes of UI polling at ~1 render/min re-requesting both images (the old behavior
    # did a full remote download every time); object_key never changes.
    renders = 60
    for _ in range(renders):
        _get("avatar"); _get("banner")
    old_fetches = renders * 2          # what no-store did: one download per asset per render
    new_fetches = counter.calls        # avatar once + banner once
    assert new_fetches == 2
    reduction = (old_fetches - new_fetches) / old_fetches * 100
    assert reduction >= 98.0           # ~98.3% for 60 renders
    print(f"OLD_REMOTE_MEDIA_FETCH_COUNT={old_fetches} NEW={new_fetches} REDUCTION={reduction:.1f}%")


def test_looks_like_image_magic():
    assert app_ui._looks_like_image(_png())
    assert app_ui._looks_like_image(b"\xff\xd8\xff\xe0" + b"\x00" * 20)   # JPEG
    assert not app_ui._looks_like_image(b"<html>error</html>")
    assert not app_ui._looks_like_image(b"")
