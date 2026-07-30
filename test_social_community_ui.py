"""Contract/security tests for the social community frontend — offline, source-level.

The project has no DOM test runner; like the existing frontend tests, these assert the
security-critical invariants of the JS/HTML/CSS source: the browser never hits the Data
API directly, no secret or ownership field is ever sent, user content is rendered safely
(no innerHTML with user data), and the module only mounts under the Supabase provider.
"""

import _test_bootstrap  # noqa: F401

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class SocialApiClientTests(unittest.TestCase):
    def setUp(self):
        self.src = read("static/social_api.js")

    def test_only_calls_backend_social_prefix(self):
        self.assertIn("/api/community/social", self.src)
        # Never the Supabase Data API directly, never a raw project URL.
        self.assertNotIn("/rest/v1", self.src)
        self.assertNotIn(".supabase.co", self.src)

    def test_no_secret_or_service_role(self):
        for bad in ("SUPABASE_SECRET_KEY", "sb_secret", "service_role", "publishable_key", "apikey"):
            self.assertNotIn(bad, self.src, bad)

    def test_forbidden_ownership_fields_stripped(self):
        # A whitelist-strip removes ownership/identity/status before any request.
        for field in ("owner_id", "author_id", "user_id", "reporter_id", "recipient_id",
                      "role", "admin", "moderator", "status"):
            self.assertIn(f"'{field}'", self.src, field)
        self.assertIn("FORBIDDEN", self.src)
        self.assertIn("sanitizeBody", self.src)

    def test_bearer_from_auth_layer_only(self):
        # Token comes from the canonical async auth provider, attached as Bearer.
        self.assertIn("getCanonicalAccessToken", self.src)
        self.assertIn("Authorization", self.src)
        self.assertIn("Bearer ${token}", self.src)

    def test_no_token_logging(self):
        self.assertNotIn("console.log", self.src)

    def test_all_documented_functions_exist(self):
        for fn in ("getMyProfile", "updateMyProfile", "getProfileByUsername", "getFeed",
                   "getMyWorks", "getWork", "createWork", "updateWork", "deleteWork",
                   "getWorkChapters", "getChapter", "createChapter", "updateChapter",
                   "deleteChapter", "getComments", "createComment", "updateComment",
                   "deleteComment", "likeChapter", "unlikeChapter", "likeComment",
                   "unlikeComment", "getFavorites", "favoriteWork", "unfavoriteWork",
                   "getHistory", "updateHistory", "createReport", "getMyReports",
                   "getNotifications", "markNotificationRead"):
            self.assertRegex(self.src, rf"export const {fn}\b", fn)

    def test_user_facing_error_messages_not_technical(self):
        self.assertIn("Sua sessão expirou", self.src)
        # Only the user-facing MESSAGES map matters here (comments may name internals).
        block = re.search(r"const MESSAGES = \{(.*?)\};", self.src, re.S)
        self.assertIsNotNone(block)
        messages = block.group(1)
        for tech in ("RLS", "PostgREST", "SQLSTATE", "constraint", "JWT", "SQL"):
            self.assertNotIn(tech, messages, tech)


class SocialCommunityUiTests(unittest.TestCase):
    def setUp(self):
        self.src = read("static/social_community.js")

    def test_no_direct_data_api_access(self):
        self.assertNotIn("/rest/v1", self.src)
        self.assertNotIn(".supabase.co", self.src)
        self.assertNotIn("createClient", self.src)

    def test_sdk_used_only_for_auth(self):
        # Only auth/session helpers are imported, and they come from the provider
        # layer rather than the Supabase module: importing that module here
        # fetched its CDN-hosted SDK on every page load, whatever provider was
        # actually active. The rule this test protects is unchanged - the Data
        # API is never touched from the browser.
        m = re.search(r"from '/static/auth_provider\.js'", self.src)
        self.assertIsNotNone(m)
        self.assertNotIn("from '/static/supabase_auth.js'", self.src)
        self.assertNotIn("supabase.from(", self.src)

    def test_no_innerhtml_with_user_content(self):
        # Safe DOM building only — no actual innerHTML assignment / HTML injection / eval.
        # Strip line comments first so documentation mentioning the word doesn't false-fail.
        code = "\n".join(line.split("//", 1)[0] for line in self.src.splitlines())
        for bad in (".innerHTML", ".outerHTML", "insertAdjacentHTML(", "eval(", "new Function"):
            self.assertNotIn(bad, code, bad)

    def test_renders_user_content_with_textcontent(self):
        self.assertIn("textContent", self.src)
        self.assertIn("node.textContent = String", self.src)

    def test_no_ownership_fields_sent_from_ui(self):
        # The UI forms build only allowed fields; it never assembles owner/author/user ids.
        for bad in ("owner_id:", "author_id:", "user_id:", "reporter_id:", "recipient_id:"):
            self.assertNotIn(bad, self.src, bad)

    def test_owner_gated_controls(self):
        self.assertIn("isOwner", self.src)
        self.assertIn("if (owner)", self.src)

    def test_soft_delete_shows_removed_placeholder(self):
        self.assertIn("Comentário removido", self.src)
        self.assertIn("is_deleted", self.src)

    def test_navigation_tabs_present(self):
        for label in ("Explorar", "Favoritos", "Continuar lendo", "Minhas obras",
                      "Meu perfil", "Notificações"):
            self.assertIn(label, self.src, label)

    def test_auth_gate_and_expiry_handling(self):
        self.assertIn("loginGate", self.src)
        self.assertIn("handleExpired", self.src)
        # A 401 clears sensitive state and does not loop refresh.
        self.assertIn("status === 401", self.src)

    def test_only_mounts_under_supabase_provider(self):
        self.assertIn("getSupabaseClient", self.src)
        self.assertIn("if (!client) return", self.src)

    def test_pdf_reader_deferred(self):
        # No fake read button; owner sees a discreet unlinked-file note.
        self.assertIn("Arquivo ainda não vinculado", self.src)
        self.assertNotIn("storage_file_id", self.src)
        self.assertNotIn("drive.google", self.src)

    def test_modal_accessibility(self):
        self.assertIn("aria-modal", self.src)
        self.assertIn("Escape", self.src)
        self.assertIn("lastFocus", self.src)

    def test_idempotent_like_favorite_with_rollback(self):
        self.assertIn("rollback", self.src.lower())
        self.assertIn("dataset.busy", self.src)


class SocialWiringTests(unittest.TestCase):
    def test_shell_has_live_region(self):
        self.assertIn('id="socialLive"', read("ui/ui_shell.html"))

    def test_app_loads_module(self):
        self.assertIn("social_community.js", read("app_ui.py"))

    def test_toast_bridge_exposed(self):
        self.assertIn("__tradutorToast", read("static/tradutor_ui.js"))


if __name__ == "__main__":
    unittest.main()
