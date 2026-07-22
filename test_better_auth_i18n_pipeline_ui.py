import _test_bootstrap  # noqa: F401

import os
import re
import unittest
from pathlib import Path
from unittest import mock

from community_auth import (
    AuthConfigurationError,
    BetterAuthProvider,
    build_auth_provider,
)

ROOT = Path(__file__).resolve().parent


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class SettingsReorganizationTests(unittest.TestCase):
    def setUp(self):
        self.shell = read("ui/ui_shell.html")

    def test_user_settings_no_longer_has_community_card(self):
        form = re.search(
            r'<form class="settings-product-grid" id="productSettingsForm">(.*?)</form>',
            self.shell,
            re.S,
        )
        self.assertIsNotNone(form)
        settings = form.group(1)
        self.assertNotIn("<div class=\"panel-label\">Comunidade</div>", settings)
        self.assertNotIn('name="default_visibility"', settings)
        self.assertNotIn('name="allow_comments"', settings)
        self.assertNotIn('name="community_notifications"', settings)
        self.assertNotIn('name="public_profile"', settings)
        self.assertNotIn('name="show_online_status"', settings)

    def test_contextual_community_preferences_still_exist(self):
        self.assertIn('id="publicationVisibility"', self.shell)
        self.assertIn('id="publicationAllowComments"', self.shell)
        self.assertIn('community-notification-preferences', self.shell)
        self.assertIn('profile-privacy-controls', self.shell)


class I18nTests(unittest.TestCase):
    def catalog_keys(self, rel: str) -> set[str]:
        return set(re.findall(r"'([^']+)'\s*:", read(rel)))

    def test_catalogs_have_matching_keys(self):
        base = self.catalog_keys("static/i18n/pt-BR.js")
        self.assertGreater(len(base), 50)
        for lang in ("en-US", "es-ES", "fr-FR", "ja-JP", "ko-KR"):
            with self.subTest(lang=lang):
                keys = self.catalog_keys(f"static/i18n/{lang}.js")
                self.assertEqual(keys, base)

    def test_i18n_uses_safe_dom_assignment(self):
        src = read("static/i18n/index.js")
        self.assertIn("textContent", src)
        self.assertIn("setAttribute('placeholder'", src)
        self.assertNotIn("innerHTML", src)

    def test_all_supported_languages_are_shown(self):
        shell = read("ui/ui_shell.html")
        for value in ("auto", "pt-BR", "en-US", "es-ES", "fr-FR", "ja-JP", "ko-KR"):
            self.assertIn(f'value="{value}"', shell)

    def test_nested_static_assets_keep_subdirectory(self):
        src = read("app_ui.py")
        self.assertIn("path.relative_to(STATIC_DIR).as_posix()", src)
        self.assertIn('"i18n" / "pt-BR.js"', src)


class BetterAuthBridgeTests(unittest.TestCase):
    def test_feature_flag_selects_better_auth_provider(self):
        provider = build_auth_provider({
            "AUTH_PROVIDER": "better_auth",
            "BETTER_AUTH_INTERNAL_URL": "http://127.0.0.1:8787",
            "BETTER_AUTH_URL": "http://127.0.0.1:8080",
        })
        self.assertIsInstance(provider, BetterAuthProvider)
        self.assertEqual(provider.public_config()["provider"], "better_auth")

    def test_better_auth_internal_url_must_be_loopback(self):
        with self.assertRaises(AuthConfigurationError):
            BetterAuthProvider(internal_url="https://example.com")

    def test_auth_ui_uses_provider_adapter_not_direct_supabase_module(self):
        self.assertIn("auth_provider.js", read("static/auth_ui.js"))
        adapter = read("static/auth_provider.js")
        self.assertIn("better_auth", adapter)
        self.assertIn("sign-in/email", adapter)
        self.assertIn("sign-up/email", adapter)
        self.assertNotIn("createClient", adapter)

    def test_app_mounts_same_origin_proxy_only_for_better_auth(self):
        src = read("app_ui.py")
        self.assertIn('@app.api_route("/api/auth/{auth_path:path}"', src)
        self.assertIn('auth_source", "") != "better_auth"', src)
        self.assertIn("auth_service_not_configured", src)
        self.assertIn("Set-Cookie", src)


class BetterAuthServiceScaffoldTests(unittest.TestCase):
    def test_service_uses_better_auth_and_hono(self):
        self.assertIn('"better-auth": "1.6.24"', read("apps/auth-service/package.json"))
        self.assertIn('"better-sqlite3": "12.11.1"', read("apps/auth-service/package.json"))
        self.assertIn('"hono": "4.12.31"', read("apps/auth-service/package.json"))
        self.assertIn("emailAndPassword", read("apps/auth-service/src/auth.ts"))
        self.assertIn("/internal/auth/session", read("apps/auth-service/src/server.ts"))

    def test_migration_scripts_are_dry_run_only_by_default(self):
        self.assertIn("remoteMigrationExecuted: false", read("scripts/auth-migration/audit.ts"))
        self.assertIn("intentionally disabled", read("scripts/auth-migration/migrate.ts"))
        self.assertIn("Do not run a remote cutover", read("scripts/auth-migration/rollback-plan.md"))


class PipelineIdentityUiTests(unittest.TestCase):
    def test_new_submission_tracks_current_job_identity(self):
        src = read("static/tradutor_ui.js")
        for token in (
            "currentRequestId",
            "currentJobId",
            "currentRunId",
            "currentSourceUrl",
            "resetActivePipelineIdentity",
            "old_pipeline_event_discarded",
        ):
            self.assertIn(token, src)

    def test_submit_uses_one_payload_after_identity_reset(self):
        src = read("static/tradutor_ui.js")
        self.assertIn("const payload = formPayload();", src)
        self.assertIn("resetActivePipelineIdentity(payload.url || payload.local_folder || '')", src)
        self.assertIn("JSON.stringify(payload)", src)


if __name__ == "__main__":
    unittest.main()
