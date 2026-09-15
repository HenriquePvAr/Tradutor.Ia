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
