from pathlib import Path

ROOT = Path(__file__).resolve().parent


def test_passive_slot_uses_public_property_and_never_touches_yk():
    source = (ROOT / "static/passive_ad_slot.js").read_text(encoding="utf-8")
    assert "https://henriquepvar.github.io/ad/banner-728x90.html" in source
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
