from pathlib import Path

ROOT = Path(__file__).resolve().parent


def test_passive_slot_uses_public_property_and_never_touches_yk():
    source = (ROOT / "static/passive_ad_slot.js").read_text(encoding="utf-8")
    assert "https://henriquepvar.github.io/ad/banner-728x90.html" in source
    assert "__yomuPassiveAdsProviderEnabled" in source
    assert "credit_rewarded" not in source.lower()
    assert "daily_yk" not in source.lower()


def test_passive_slots_are_limited_to_neutral_views():
    source = (ROOT / "static/passive_ad_slot.js").read_text(encoding="utf-8")
    assert "#view-inicio" in source and "#view-rewards" in source and "#view-scans" in source
    assert "#view-leitor" not in source
