from pathlib import Path


ROOT = Path(__file__).resolve().parent


def test_scans_and_community_share_single_coming_soon_badge():
    html = (ROOT / "ui" / "ui_shell.html").read_text(encoding="utf-8")
    scans = html.split('data-tab="scans"', 1)[1].split("</li>", 1)[0]
    community = html.split('data-tab="community"', 1)[1].split("</li>", 1)[0]
    assert scans.count('class="rail-dev-badge">FIX 12</small>') == 1
    assert community.count('class="rail-dev-badge">EM BREVE</small>') == 1


def test_beta5_version_is_current_build():
    source = (ROOT / "app_version.py").read_text(encoding="utf-8")
    assert 'BUILD_VERSION = "0.9.1-beta.12"' in source
