from pathlib import Path

import app_version


def test_canonical_versions_are_current():
    assert app_version.PRODUCT_VERSION == "0.9.0"
    assert app_version.BUILD_VERSION == "0.9.1-beta.5"
    assert app_version.DISPLAY_VERSION == app_version.BUILD_VERSION


def test_installer_uses_release_define_not_stale_beta20():
    script = Path("packaging/TradutorIA.iss").read_text(encoding="utf-8")
    assert "AppVersion={#ProductVersion}" in script
    assert "Beta20" not in script


def test_update_manifest_defaults_to_canonical_product_version():
    source = Path("tools/release/build_update_manifest.py").read_text(encoding="utf-8")
    assert "default=PRODUCT_VERSION" in source
