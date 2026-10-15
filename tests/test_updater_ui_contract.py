from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_updater_ui_elements_are_present_in_canonical_shell():
    html = (ROOT / "ui" / "ui_shell.html").read_text(encoding="utf-8")
    for element_id in (
        "updaterPanel",
        "updateAvailableBadge",
        "updaterInstalledVersion",
        "updaterAvailableVersion",
        "updaterReleaseNotes",
        "updaterStatus",
        "updaterCheckBtn",
        "updaterInstallBtn",
        "updaterCancelBtn",
    ):
        assert f'id="{element_id}"' in html

    css = (ROOT / "static" / "tradutor_ui.css").read_text(encoding="utf-8")
    assert "#updateAvailableBadge[hidden]" in css
    assert "#updaterInstallBtn[hidden]" in css


def test_updater_ui_uses_signed_manifest_and_backend_state_contract():
    source = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
    start = source.index("/* ---------- signed updater UI ---------- */")
    end = source.index("/* ---------- copy ---------- */", start)
    updater = source[start:end]

    assert "api('/api/update/manifest')" in updater
    assert "['update_available', 'mandatory'].includes(data.state)" in updater
    assert "data.state === 'ready_to_install'" in updater
    assert "data.download_token" in updater
    assert "data.handoff_token" in updater
    assert "JSON.stringify({handoff_token: handoffToken})" in updater
    assert "api('/api/update/install'" in updater
    assert "innerHTML" not in updater


def test_updater_ui_does_not_bypass_backend_signature_verification():
    source = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
    start = source.index("/* ---------- signed updater UI ---------- */")
    end = source.index("/* ---------- copy ---------- */", start)
    updater = source[start:end]

    assert "verify_manifest" not in updater
    assert "TRUSTED_PUBLIC_KEYS" not in updater
    assert "api('/api/update/download'" in updater
