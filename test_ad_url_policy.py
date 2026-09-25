from types import SimpleNamespace

import desktop_app
from ad_url_policy import canonical_ad_url, is_allowed_ad_navigation, is_allowed_ad_url


def test_canonical_and_legacy_redirect_urls_are_accepted():
    canonical = "https://yomusekai.com.br/ad/home-728x90.html"
    legacy = "https://henriquepvar.github.io/ad/home-728x90.html"
    assert canonical_ad_url(canonical) == canonical
    assert is_allowed_ad_navigation(canonical, canonical)
    assert canonical_ad_url(legacy) == canonical
    assert is_allowed_ad_navigation(legacy, canonical)


def test_untrusted_host_path_scheme_and_encoded_traversal_are_rejected():
    requested = "https://henriquepvar.github.io/ad/home-728x90.html"
    for final in (
        "https://attacker.example/ad/home-728x90.html",
        "https://yomusekai.com.br/account/home-728x90.html",
        "http://yomusekai.com.br/ad/home-728x90.html",
        "javascript:alert(1)",
        "https://yomusekai.com.br/ad/%2e%2e/private.html",
        "https://yomusekai.com.br.evil.example/ad/home-728x90.html",
    ):
        assert not is_allowed_ad_url(final), final
        assert not is_allowed_ad_navigation(requested, final), final


def test_cross_provider_redirect_is_rejected_but_game_deals_same_origin_is_preserved():
    github = "https://henriquepvar.github.io/ad/home-728x90.html"
    vercel = "https://game-deals-alpha.vercel.app/ad/new-translation-728x90.html"
    assert not is_allowed_ad_navigation(github, vercel)
    assert is_allowed_ad_navigation(vercel, vercel)
    assert canonical_ad_url(vercel) == vercel


def test_navigation_completed_commits_valid_canonical_redirect(monkeypatch):
    events = []
    monkeypatch.setattr(desktop_app, "_runtime_log", lambda name, **fields: events.append((name, fields)))
    monkeypatch.setattr(desktop_app, "_native_poc_log", lambda *args, **kwargs: None)
    surface = desktop_app.NativeAdSurface.__new__(desktop_app.NativeAdSurface)
    placement = ("home", "https://henriquepvar.github.io/ad/home-728x90.html", 728, 90)
    surface._active_navigation = (1, placement)
    surface._navigation_generation = 1
    surface._navigation_in_progress = True
    surface._requested_placement = placement
    surface._ready = True
    surface.control = SimpleNamespace(CoreWebView2=SimpleNamespace(Source="https://yomusekai.com.br/ad/home-728x90.html"))
    surface._last_requested_bounds = None
    surface._on_navigation_completed(None, SimpleNamespace(IsSuccess=True, WebErrorStatus="unknown"))
    assert surface._committed_placement == placement
    assert surface._current_navigation_url == placement[1]
    assert surface._active_navigation is None
    assert not surface._navigation_in_progress
    assert any(name == "NAV_COMMIT_CURRENT" for name, _ in events)


def test_old_generation_remains_stale_and_dispatches_latest(monkeypatch):
    events = []
    monkeypatch.setattr(desktop_app, "_runtime_log", lambda name, **fields: events.append(name))
    monkeypatch.setattr(desktop_app, "_native_poc_log", lambda *args, **kwargs: None)
    surface = desktop_app.NativeAdSurface.__new__(desktop_app.NativeAdSurface)
    old = ("home", "https://yomusekai.com.br/ad/home-728x90.html", 728, 90)
    surface._active_navigation = (1, old)
    surface._navigation_generation = 2
    surface._navigation_in_progress = True
    surface._requested_placement = ("queue", "https://yomusekai.com.br/ad/queue-468x60.html", 468, 60)
    surface._ready = True
    surface.control = SimpleNamespace(CoreWebView2=SimpleNamespace(Source=old[1]))
    calls = []
    surface._navigate_requested_on_ui_thread = lambda: calls.append(True)
    surface._on_navigation_completed(None, SimpleNamespace(IsSuccess=True, WebErrorStatus="unknown"))
    assert "NAV_CALLBACK_STALE_IGNORED" in events
    assert calls == [True]
    assert surface._active_navigation is None
    assert not surface._navigation_in_progress


def test_current_generation_with_untrusted_final_url_fails_closed_and_releases_navigation(monkeypatch):
    events = []
    hidden = []
    monkeypatch.setattr(desktop_app, "_runtime_log", lambda name, **fields: events.append(name))
    monkeypatch.setattr(desktop_app, "_native_poc_log", lambda *args, **kwargs: None)
    surface = desktop_app.NativeAdSurface.__new__(desktop_app.NativeAdSurface)
    placement = ("home", "https://yomusekai.com.br/ad/home-728x90.html", 728, 90)
    surface._active_navigation = (3, placement)
    surface._navigation_generation = 3
    surface._navigation_in_progress = True
    surface._requested_placement = placement
    surface._ready = True
    surface.control = SimpleNamespace(CoreWebView2=SimpleNamespace(Source="https://attacker.example/ad/home-728x90.html"))
    surface._hide_surface_for_navigation_failure = lambda route: hidden.append(route)
    surface._on_navigation_completed(None, SimpleNamespace(IsSuccess=True, WebErrorStatus="unknown"))
    assert surface._committed_placement is None
    assert surface._active_navigation is None
    assert not surface._navigation_in_progress
    assert hidden == ["home"]
    assert "PLACEMENT_NAV_FAILURE" in events


def test_top_level_navigation_allowlist_blocks_unknown_targets(monkeypatch):
    blocked_events = []
    monkeypatch.setattr(desktop_app, "_runtime_log", lambda name, **fields: blocked_events.append(name))
    surface = desktop_app.NativeAdSurface.__new__(desktop_app.NativeAdSurface)
    surface._active_navigation = (1, ("home", "https://yomusekai.com.br/ad/home-728x90.html", 728, 90))
    surface._current_navigation_url = None
    args = SimpleNamespace(Uri="https://attacker.example/ad/home-728x90.html", Cancel=False)
    surface._on_navigation_starting(None, args)
    assert args.Cancel is True
    assert "AD_TOP_LEVEL_NAVIGATION_BLOCKED" in blocked_events
