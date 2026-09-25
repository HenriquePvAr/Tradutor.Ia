from pathlib import Path

import app_version


def test_canonical_versions_are_current():
    assert app_version.PRODUCT_VERSION == "0.9.0"
    assert app_version.BUILD_VERSION == "0.9.1-beta.14"
    assert app_version.DISPLAY_VERSION == app_version.BUILD_VERSION


def test_installer_uses_release_define_not_stale_beta20():
    script = Path("packaging/TradutorIA.iss").read_text(encoding="utf-8")
    assert "AppVersion={#ProductVersion}" in script
    assert "Beta20" not in script


def test_installer_declares_windows_uninstall_icon_and_shortcut_icon():
    script = Path("packaging/TradutorIA.iss").read_text(encoding="utf-8")
    icon_path = r"{app}\_internal\assets\branding\generated\yomu-sekai.ico"
    assert f"UninstallDisplayIcon={icon_path}" in script
    assert script.count(f'IconFilename: "{icon_path}"') == 2


def test_installer_icon_file_is_staged_by_the_canonical_pyinstaller_spec():
    script = Path("packaging/TradutorIA.iss").read_text(encoding="utf-8")
    spec = Path("packaging/tradutor_ia.spec").read_text(encoding="utf-8")
    assert "SetupIconFile=" in script and "assets\\branding\\generated\\yomu-sekai.ico" in script
    assert '(str(ROOT / "assets" / "branding"), "assets/branding")' in spec
    assert 'icon=str(ROOT / "assets" / "branding" / "generated" / "yomu-sekai.ico")' in spec


def test_update_manifest_defaults_to_canonical_product_version():
    source = Path("tools/release/build_update_manifest.py").read_text(encoding="utf-8")
    assert "default=PRODUCT_VERSION" in source
