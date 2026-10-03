"""Offline contract tests for Download-only entitlement behavior."""

from app_ui import required_yk_for_run


def test_download_only_with_zero_balance_is_allowed_without_yk_cost():
    assert required_yk_for_run({"download_only": True, "user_balance": 0}) == 0


def test_normal_translation_keeps_one_yk_cost():
    assert required_yk_for_run({"download_only": False, "user_balance": 0}) == 1
    assert required_yk_for_run({"user_balance": 5}) == 1


def test_download_only_does_not_require_provider_credentials_by_contract():
    assert required_yk_for_run({"download_only": True}) == 0
