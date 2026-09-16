from pathlib import Path

ROOT = Path(__file__).resolve().parent


def test_passive_slot_uses_public_property_and_never_touches_yk():
    source = (ROOT / "static/passive_ad_slot.js").read_text(encoding="utf-8")
    for url in ("/ad/home-728x90.html", "/ad/new-translation-728x90.html", "/ad/queue-468x60.html", "/ad/rewards-320x50.html", "/ad/translated-chapters-native.html"):
        assert url in source
    assert "__yomuPassiveAdsProviderEnabled" in source
    assert "providerEnabled" in source
    assert "credit_rewarded" not in source.lower()
    assert "daily_yk" not in source.lower()


def test_passive_slots_are_limited_to_neutral_views():
    source = (ROOT / "static/passive_ad_slot.js").read_text(encoding="utf-8")
    for view in ("#view-inicio", "#view-nova", "#view-queue", "#view-rewards", "#view-hist"):
        assert view in source
    assert "#view-scans" not in source
    for forbidden in ("#view-leitor", "#view-profile", "#view-cfg", "#view-updates", "#view-community"):
        assert forbidden not in source


def test_live_shell_declares_only_the_five_approved_placements():
    html = (ROOT / "ui/ui_shell.html").read_text(encoding="utf-8")
    for name in ("home", "new-translation", "queue", "rewards", "translated-chapters"):
        assert f'data-passive-ad-slot="{name}"' in html
    assert 'data-passive-ad-slot="scans"' not in html


def test_native_geometry_uses_single_flight_and_no_global_mutation_observer():
    source = (ROOT / "static/passive_ad_slot.js").read_text(encoding="utf-8")
    assert "MutationObserver" not in source
    assert "requestAnimationFrame" in source
    assert "lastSentKey" in source
    assert "inFlight" in source
    assert "pendingState" in source


def test_native_route_hook_is_explicit_and_resize_observer_is_scoped():
    source = (ROOT / "static/passive_ad_slot.js").read_text(encoding="utf-8")
    assert "yomu-passive-ad-route" in source
    assert "resizeObserver.disconnect()" in source
    assert "resizeObserver.observe(activeAdSlot)" in source
    assert "tradutor-auth-changed" in source
    assert "activeAdSlot.hidden = false" in source


def test_host_mapping_keeps_vercel_only_for_approved_placements():
    source = (ROOT / "static/passive_ad_slot.js").read_text(encoding="utf-8")
    assert "https://game-deals-alpha.vercel.app/ad/new-translation-728x90.html" in source
    assert "https://game-deals-alpha.vercel.app/ad/rewards-320x50.html" in source
    assert "https://henriquepvar.github.io/ad/home-728x90.html" in source
    assert "https://henriquepvar.github.io/ad/queue-468x60.html" in source
    assert "https://henriquepvar.github.io/ad/translated-chapters-native.html" in source


def test_native_surface_is_lazy_and_ads_off_hides_without_bounds_request():
    source = (ROOT / "desktop_app.py").read_text(encoding="utf-8")
    js = (ROOT / "static/passive_ad_slot.js").read_text(encoding="utf-8")
    assert "_request_native_ad_surface" in source
    assert "_NATIVE_AD_PENDING_BOUNDS" in source
    assert "if (!providerEnabled()) { activeAdSlot.hidden = true;" in js
    assert "_NATIVE_AD_SURFACE = NativeAdSurface(window)" not in source.split("def run", 1)[1].split("webview.start", 1)[0]


def test_native_bounds_are_coalesced_and_have_a_circuit_breaker():
    source = (ROOT / "desktop_app.py").read_text(encoding="utf-8")
    assert "_begininvoke_pending" in source
    assert "_bounds_coalesced" in source
    assert "AD_STORM_CIRCUIT_BREAKER" in source
    assert "len(self._native_bounds_events) > 500" in source
    assert "nativeDispatchTimer" in (ROOT / "static/passive_ad_slot.js").read_text(encoding="utf-8")
    assert "now - lastNativeDispatchAt < 200" in (ROOT / "static/passive_ad_slot.js").read_text(encoding="utf-8")


def test_native_route_navigation_is_deduplicated_and_stateful_visibility_is_logged():
    source = (ROOT / "desktop_app.py").read_text(encoding="utf-8")
    assert "_current_navigation_url" in source
    assert "PLACEMENT_NAV_START" in source
    assert "self._surface_visible" in source
    assert "if desired_visible and not self._surface_visible" in source
