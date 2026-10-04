"""Mission 2: the "Fontes" (supported sources) tab is an external-shortcut hub only.

It must expose exactly the three supported sites, open them in the system browser through an
allow-listed bridge method (never an arbitrary page-supplied URL), never fetch those sites on
page load, and never reintroduce the removed Profile experience.
"""
import re
import unittest
from pathlib import Path
from unittest import mock

import yomu.desktop.app as desktop_app

ROOT = Path(__file__).resolve().parents[1]
SHELL = (ROOT / "ui" / "ui_shell.html").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")

EXPECTED = {
    "webtoon": "https://www.webtoons.com/en/",
    "comix": "https://comix.to/browse?genres_in=101137",
    "vortex": "https://vortexscans.org/",
}


class SourcesTabTests(unittest.TestCase):
    # A) tab present in navigation
    def test_sources_tab_in_navigation(self):
        self.assertIn('data-tab="fontes"', SHELL)
        self.assertIn('id="view-fontes"', SHELL)
        self.assertIn("fontes: 'view-fontes'", UI_JS)

    # B) Profile stays removed (no profile/perfil nav tab)
    def test_profile_tab_still_absent(self):
        self.assertNotIn('data-tab="perfil"', SHELL)
        self.assertNotIn('data-tab="profile"', SHELL)

    # D) exactly three sources
    def test_exactly_three_sources(self):
        keys = re.findall(r'class="btn-primary source-open"[^>]*data-source="([a-z]+)"', SHELL)
        self.assertEqual(sorted(keys), ["comix", "vortex", "webtoon"])
        self.assertEqual(len(desktop_app.SUPPORTED_SOURCE_URLS), 3)

    # E/F/G) exact allow-listed destinations
    def test_exact_source_urls(self):
        self.assertEqual(desktop_app.SUPPORTED_SOURCE_URLS, EXPECTED)

    # H/§14) opening the page triggers no external request: the panel carries no eager
    # resource (img/iframe/href/src) pointing at the source domains -- only opaque keys.
    def test_page_load_has_no_external_resource(self):
        section = SHELL.split('id="view-fontes"', 1)[1].split("</section>", 1)[0]
        for domain in ("webtoons.com", "comix.to", "vortexscans.org"):
            self.assertNotIn(domain, section, f"{domain} must not be embedded in the page")
        for eager in ("<img", "<iframe", "<script", "src=", "href="):
            self.assertNotIn(eager, section, f"no eager resource ({eager}) on the Fontes panel")

    # I/J) the site opens only via the safe, allow-listed bridge on explicit key
    def test_bridge_opens_only_allowlisted_urls(self):
        for key, url in EXPECTED.items():
            with mock.patch("webbrowser.open", return_value=True) as opener:
                result = desktop_app.DesktopApi.open_supported_source(None, key)
            opener.assert_called_once_with(url)
            self.assertEqual(result, {"opened": True, "key": key})

    def test_bridge_rejects_unknown_key_without_opening(self):
        with mock.patch("webbrowser.open") as opener:
            with self.assertRaises(ValueError):
                desktop_app.DesktopApi.open_supported_source(None, "http://evil.example/")
            with self.assertRaises(ValueError):
                desktop_app.DesktopApi.open_supported_source(None, "")
        opener.assert_not_called()

    # the page wires clicks to the bridge method (explicit user action only)
    def test_page_wires_clicks_to_bridge(self):
        self.assertIn("source-open", UI_JS)
        self.assertIn("open_supported_source", UI_JS)


if __name__ == "__main__":
    unittest.main()
