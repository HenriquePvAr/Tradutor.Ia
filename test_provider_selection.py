"""Coverage for the community social-provider mount and its bootstrap contract.

Root cause: static/social_community.js used to decide whether to mount the Supabase
social UI by checking for the #communityFeed DOM element (ui/ui_shell.html renders it
unconditionally, so the check always found it and the social UI never mounted). The fix
makes app_ui.py compute a sanitized `community.social` record at startup (provider,
available, reason_code — never a URL, key, path or raw exception) and expose it through
/api/ui/bootstrap; the frontend now decides from that instead of the DOM. See
static/social_community.js (mountCommunityProvider/fetchCommunityProviderState) and
.runtime/claude-community-supabase-mount/test_provider_mount.js for the frontend half.
"""

import _test_bootstrap  # noqa: F401

import os
import unittest
from pathlib import Path
from types import SimpleNamespace

import app_ui
from community_auth import RequestPrincipal

ROOT = Path(__file__).resolve().parent


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class FakeAuthenticated:
    def __init__(self, principal):
        self._principal = principal

    def authenticate_request(self, request):
        return self._principal


class FakeAnonymousAuth:
    def authenticate_request(self, request):
        return RequestPrincipal.anonymous()


class FakeBridge:
    def bootstrap(self, cursor, *, principal):
        return {"history": [], "queue": [], "community": {}, "profile": {}}


PRINCIPAL = RequestPrincipal(
    user_id="owner-a", authenticated=True, roles=frozenset({"user"}),
    auth_source="local_test", session_id="session-a",
)


class BootstrapSocialStatusTests(unittest.TestCase):
    """api_bootstrap() must merge the real, backend-computed provider state — never a
    hardcoded default — into payload["community"]["social"], for every auth outcome."""

    def setUp(self):
        self._orig_status = app_ui._SOCIAL_STATUS

    def tearDown(self):
        app_ui._SOCIAL_STATUS = self._orig_status

    def _bootstrap(self, auth, bridge=None, sync=lambda p: {}, profile=lambda p: {}):
        import app_ui as mod
        orig_auth, orig_bridge = mod.AUTH, mod.BRIDGE
        orig_sync, orig_profile = mod._sync_public_profile, mod._profile_for_principal
        orig_enrich = mod._enrich_history_publications
        mod.AUTH = auth
        mod.BRIDGE = bridge or FakeBridge()
        mod._sync_public_profile = sync
        mod._profile_for_principal = profile
        mod._enrich_history_publications = lambda history: None
        try:
            return mod.api_bootstrap(SimpleNamespace(), cursor=0)
        finally:
            mod.AUTH, mod.BRIDGE = orig_auth, orig_bridge
            mod._sync_public_profile, mod._profile_for_principal = orig_sync, orig_profile
            mod._enrich_history_publications = orig_enrich

    def test_authenticated_bootstrap_exposes_supabase_available_state(self):
        app_ui._SOCIAL_STATUS = {"provider": "supabase", "available": True, "reason_code": None}
        payload = self._bootstrap(FakeAuthenticated(PRINCIPAL))
        self.assertEqual(payload["community"]["social"], {"provider": "supabase", "available": True, "reason_code": None})

    def test_authenticated_bootstrap_exposes_local_provider_state(self):
        app_ui._SOCIAL_STATUS = {"provider": "local", "available": True, "reason_code": None}
        payload = self._bootstrap(FakeAuthenticated(PRINCIPAL))
        self.assertEqual(payload["community"]["social"]["provider"], "local")
        self.assertTrue(payload["community"]["social"]["available"])

    def test_authenticated_bootstrap_exposes_unavailable_reason_code(self):
        app_ui._SOCIAL_STATUS = {
            "provider": "supabase", "available": False,
            "reason_code": "social_provider_not_configured",
        }
        payload = self._bootstrap(FakeAuthenticated(PRINCIPAL))
        social = payload["community"]["social"]
        self.assertFalse(social["available"])
        self.assertEqual(social["reason_code"], "social_provider_not_configured")

    def test_unknown_provider_reason_code_reaches_bootstrap(self):
        app_ui._SOCIAL_STATUS = {"provider": "unknown", "available": False, "reason_code": "social_provider_unknown"}
        payload = self._bootstrap(FakeAuthenticated(PRINCIPAL))
        self.assertEqual(payload["community"]["social"]["reason_code"], "social_provider_unknown")

    def test_anonymous_bootstrap_still_exposes_social_state(self):
        # The provider decision must be available before login too: the frontend needs it
        # to pick the right UI even while showing the login gate.
        app_ui._SOCIAL_STATUS = {"provider": "supabase", "available": True, "reason_code": None}
        payload = self._bootstrap(FakeAnonymousAuth())
        self.assertEqual(payload["community"]["social"]["provider"], "supabase")
        self.assertFalse(payload["community"]["authenticated"])

    def test_auth_error_path_still_exposes_social_state(self):
        class ExplodingAuth:
            def authenticate_request(self, request):
                raise RuntimeError("boom")

        app_ui._SOCIAL_STATUS = {"provider": "local", "available": True, "reason_code": None}
        payload = self._bootstrap(ExplodingAuth())
        self.assertEqual(payload["community"]["auth_state"], "auth_error")
        self.assertEqual(payload["community"]["social"]["provider"], "local")

    def test_social_status_never_carries_raw_config_or_secrets(self):
        # Whatever _SOCIAL_STATUS was computed to, the merged payload only ever carries the
        # three closed fields — never a URL, key, path, or a raw exception message.
        app_ui._SOCIAL_STATUS = {
            "provider": "supabase", "available": False,
            "reason_code": "social_provider_not_configured",
        }
        payload = self._bootstrap(FakeAuthenticated(PRINCIPAL))
        social = payload["community"]["social"]
        self.assertEqual(set(social), {"provider", "available", "reason_code"})
        blob = str(social)
        for leak in ("SUPABASE_URL", "supabase.co", "SUPABASE_PUBLISHABLE_KEY", "Traceback", "SocialConfigError"):
            self.assertNotIn(leak, blob, leak)


class SocialStatusComputationTests(unittest.TestCase):
    """The module-level classification app_ui.py performs at import/startup time."""

    def test_current_environment_classification_is_closed_and_sanitized(self):
        # Whatever this machine's local .env selects, the resulting status must always be
        # one of the closed shapes: a known provider name, a bool, and either None or one
        # of the closed reason codes.
        status = app_ui._SOCIAL_STATUS
        self.assertEqual(set(status), {"provider", "available", "reason_code"})
        self.assertIn(status["provider"], {"supabase", "local", "unknown"})
        self.assertIsInstance(status["available"], bool)
        if status["available"]:
            self.assertIsNone(status["reason_code"])
        else:
            self.assertIn(status["reason_code"], {
                "social_provider_not_configured",
                "social_provider_unknown",
                "social_provider_initialization_failed",
            })

    def test_reason_codes_used_in_source_match_the_documented_closed_set(self):
        source = read("app_ui.py")
        closed_set = {
            "social_provider_not_configured",
            "social_provider_unknown",
            "social_provider_initialization_failed",
        }
        for code in closed_set:
            self.assertIn(f'"{code}"', source, code)


class BootFailureBeforeFixDocumentedTests(unittest.TestCase):
    """Static assertions proving the original DOM-presence bug is actually gone."""

    def test_boot_no_longer_keys_off_communityFeed_dom_element(self):
        source = read("static/social_community.js")
        self.assertNotIn("document.getElementById('communityFeed')", source)

    def test_ui_shell_still_renders_communityFeed_unconditionally(self):
        # Confirms the root cause precondition still holds: the legacy container is
        # always present, so DOM-presence genuinely could never have distinguished the
        # providers — the fix had to move the decision server-side, not just relocate it.
        shell = read("ui/ui_shell.html")
        self.assertIn('id="communityFeed"', shell)

    def test_boot_source_derives_provider_from_bootstrap_not_dom(self):
        source = read("static/social_community.js")
        self.assertIn("/api/ui/bootstrap", source)
        self.assertIn("data.community", source)
        self.assertIn("data.community.social", source)

    def test_bootstrap_default_payload_no_longer_hardcodes_available_true_for_social(self):
        # api_bootstrap()'s literal default dict must route through _SOCIAL_STATUS, not a
        # hardcoded stand-in that always claims the social backend is up.
        source = read("app_ui.py")
        bootstrap_fn = source[source.index("def api_bootstrap"):source.index("\n\n\n", source.index("def api_bootstrap"))]
        self.assertIn("_SOCIAL_STATUS", bootstrap_fn)


if __name__ == "__main__":
    unittest.main()
