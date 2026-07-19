"""Static contract checks for the Supabase auth frontend — offline, no browser.

Guards the security-critical invariants of the browser code: the secret key never
appears, only the publishable config is fetched, the Bearer token is attached from the
SDK-owned cache, the callback cannot open-redirect, and no token is logged.
"""

import _test_bootstrap  # noqa: F401

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class SupabaseFrontendTests(unittest.TestCase):
    def test_secret_key_never_referenced_in_frontend(self):
        for rel in ("static/supabase_auth.js", "static/auth_ui.js",
                    "static/tradutor_ui.js", "ui/ui_shell.html", "ui/auth_callback.html"):
            source = _read(rel)
            self.assertNotIn("SUPABASE_SECRET_KEY", source, rel)
            self.assertNotIn("sb_secret", source, rel)
            self.assertNotIn("service_role", source, rel)
            self.assertNotIn("secret_key", source.lower(), rel)

    def test_frontend_fetches_only_public_config(self):
        source = _read("static/supabase_auth.js")
        self.assertIn("/api/community/auth/config", source)
        self.assertIn("publishable_key", source)
        # The SDK is created from the public url + publishable key only.
        self.assertIn("createClient(cfg.supabase_url, cfg.publishable_key", source)

    def test_supabase_sdk_version_is_pinned(self):
        source = _read("static/supabase_auth.js")
        self.assertIn("@supabase/supabase-js@2.", source)
        self.assertNotIn("@supabase/supabase-js@latest", source)

    def test_api_attaches_bearer_from_sdk_cache(self):
        source = _read("static/tradutor_ui.js")
        self.assertIn("window.__tradutorAccessToken", source)
        self.assertIn("Authorization", source)
        self.assertIn("Bearer ${bearer}", source)

    def test_callback_has_no_open_redirect(self):
        source = _read("ui/auth_callback.html")
        # The post-auth destination is a constant, never read from a URL parameter.
        self.assertIn("window.location.replace('/')", source)
        self.assertNotIn("searchParams.get", source)
        self.assertNotIn("location.href = ", source)

    def test_no_token_or_session_logging(self):
        for rel in ("static/supabase_auth.js", "static/auth_ui.js", "ui/auth_callback.html"):
            source = _read(rel)
            self.assertNotIn("console.log", source, rel)

    def test_auth_ui_exposes_signup_login_logout(self):
        source = _read("static/auth_ui.js")
        for token in ("signUp", "signIn", "signOut", "onAuthChange"):
            self.assertIn(token, source)

    def test_shell_has_auth_controls(self):
        shell = _read("ui/ui_shell.html")
        for anchor in ("authModalOverlay", "authOpenBtn", "authLogoutBtn",
                       "authForm", "authStatus"):
            self.assertIn(anchor, shell)

    def test_callback_route_registered(self):
        app_source = _read("app_ui.py")
        self.assertIn('/auth/callback', app_source)
        self.assertIn("auth_callback.html", app_source)


if __name__ == "__main__":
    unittest.main()
