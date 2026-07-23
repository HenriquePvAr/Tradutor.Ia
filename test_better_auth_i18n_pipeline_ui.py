import _test_bootstrap  # noqa: F401

import os
import re
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

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

    def test_service_exposes_liveness_readiness_and_database_readiness(self):
        server = read("apps/auth-service/src/server.ts")
        self.assertIn('/internal/auth/live', server)
        self.assertIn('/internal/auth/ready', server)
        self.assertIn('/internal/auth/db-ready', server)
        self.assertIn('databaseReady', server)
        self.assertIn('Cache-Control', server)
        self.assertIn('503', server)

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


class AuthLoadingVisualContractTests(unittest.TestCase):
    def setUp(self):
        self.shell = read("ui/ui_shell.html")
        self.css = read("static/tradutor_ui.css")
        self.auth_js = read("static/auth_ui.js")
        self.ui_js = read("static/tradutor_ui.js")

    def test_login_is_full_page_surface_not_modal(self):
        self.assertIn('id="authSurface"', self.shell)
        self.assertIn('class="auth-login-page"', self.shell)
        self.assertNotIn('id="authModalOverlay"', self.shell)
        self.assertNotIn('id="authModalClose"', self.shell)
        self.assertNotIn("auth-reference-card", self.shell)
        self.assertNotIn('role="dialog" aria-modal="true" aria-labelledby="authTitle"', self.shell)
        self.assertNotIn("authModalOverlay", self.auth_js)
        self.assertNotIn("authModalClose", self.auth_js)

    def test_login_has_single_full_page_form_and_prefixed_reference_classes(self):
        self.assertEqual(self.shell.count("data-auth-login"), 1)
        self.assertEqual(self.shell.count('id="authForm"'), 1)
        auth_fragment = self.shell[self.shell.index('id="authSurface"'):]
        for generic in (
            'class="card"', 'class="grid"', 'class="logo"', 'class="stage"',
            'class="side"', 'class="pipeline"', 'class="badge"', 'class="row"',
            'class="footer"', 'class="title"', 'class="subtitle"',
        ):
            self.assertNotIn(generic, auth_fragment, generic)
        for expected in (
            "auth-login-stage", "auth-login-side", "auth-login-card",
            "auth-login-compare", "auth-marketing-pipeline", "auth-login-submit",
        ):
            self.assertIn(expected, auth_fragment)

    def test_login_responsive_contract_matches_reference_breakpoint(self):
        self.assertIn(".auth-login-page{position:fixed;inset:0", self.css)
        self.assertIn(".auth-login-stage{position:relative;flex:1.2", self.css)
        self.assertIn(".auth-login-side{flex:1", self.css)
        self.assertIn("@media (max-width:900px){.auth-login-stage{display:none}", self.css)
        self.assertIn("min-height:100dvh", self.css)

    def test_loading_uses_real_reference_images_and_original_structure(self):
        boot = self.shell[: self.shell.index('<div class="shell">')]
        for expected in (
            "app-loading-page", "app-loading-frame", "app-loading-compare",
            "app-loading-before", "app-loading-after", "app-loading-scanline",
            "app-loading-nodes", "app-loading-status-line",
            "app-loading-footer-row",
        ):
            self.assertIn(expected, boot)
        self.assertIn("app-loading-connector", self.ui_js + self.css)
        self.assertIn('/static/assets/reference-ui/original.jpg', boot)
        self.assertIn('/static/assets/reference-ui/translated.jpg', boot)
        surface_css = self.css[self.css.index("full-page auth/loading surfaces"):self.css.index("/* ---------- auth ---------- */")]
        self.assertNotIn("filter:url(#inkRough)", surface_css)
        self.assertNotIn("rl-compare::before", surface_css)

    def test_reference_assets_are_valid_distinct_jpegs(self):
        original = ROOT / "static/assets/reference-ui/original.jpg"
        translated = ROOT / "static/assets/reference-ui/translated.jpg"
        self.assertTrue(original.exists())
        self.assertTrue(translated.exists())
        original_bytes = original.read_bytes()
        translated_bytes = translated.read_bytes()
        self.assertGreater(len(original_bytes), 60000)
        self.assertGreater(len(translated_bytes), 60000)
        self.assertNotEqual(original_bytes, translated_bytes)
        self.assertEqual(original_bytes[:3], b"\xff\xd8\xff")
        self.assertEqual(translated_bytes[:3], b"\xff\xd8\xff")
        with Image.open(original) as img:
            self.assertEqual(img.size, (495, 550))
        with Image.open(translated) as img:
            self.assertEqual(img.size, (495, 550))

    def test_reference_asset_hashes_match_extracted_html_images(self):
        import hashlib
        self.assertEqual(
            hashlib.sha256((ROOT / "static/assets/reference-ui/original.jpg").read_bytes()).hexdigest(),
            "bfc1e5f1a9ca420dc685324d15a00ff06893fe311fc6b3b568aa9d14516af261",
        )
        self.assertEqual(
            hashlib.sha256((ROOT / "static/assets/reference-ui/translated.jpg").read_bytes()).hexdigest(),
            "04af5b3dfafd7b00c5f5791d62ad98caca95b762b0adabe3385e758df3085540",
        )

    def test_shell_state_machine_prevents_private_flash(self):
        self.assertIn('html:not([data-shell-state="authenticated"]) .shell{display:none;}', self.css)
        for state in ("booting", "unauthenticated", "authenticating", "authenticated", "boot_failed"):
            self.assertIn(state, self.ui_js + self.auth_js)
        self.assertIn("showLoginSurface", self.auth_js)
        self.assertIn("hideLoginSurface", self.auth_js)
        self.assertIn("setShellState", self.auth_js)

    def test_loading_advances_by_bootstrap_stage_and_fails_closed(self):
        self.assertIn("bootStageMeta", self.ui_js)
        self.assertIn("setBootStage(7)", self.ui_js)
        self.assertIn("setBootFailed", self.ui_js)
        self.assertIn("bootActions", self.shell)
        self.assertNotIn("bootEl?.addEventListener('click'", self.ui_js)
        self.assertIn("O carregamento demorou para responder.", self.ui_js)

    def test_auth_compare_slider_is_bound_without_extra_submit_listener(self):
        self.assertEqual(self.auth_js.count("$('#authForm')?.addEventListener('submit'"), 1)
        self.assertIn("authCompare", self.auth_js)
        self.assertIn("authCompareHandle", self.auth_js)
        self.assertIn("setComparePosition", self.auth_js)
        self.assertIn("forwardMs = 5200", self.auth_js)
        self.assertIn("pauseStartMs = 750", self.auth_js)
        self.assertNotIn("compare.addEventListener('mousedown'", self.auth_js)
        self.assertNotIn("touchstart", self.auth_js)
        self.assertIn("pointer-events:none", self.css)
        self.assertIn("object-fit:contain;object-position:top center", self.css)

    def test_auth_marketing_pipeline_ping_pongs_and_is_not_interactive(self):
        self.assertIn('id="authMarketingPipeline"', self.shell)
        self.assertEqual(self.shell.count("data-auth-pipeline-step"), 6)
        self.assertIn("sequence = [0, 1, 2, 3, 4, 5, 4, 3, 2, 1, 0]", self.auth_js)
        self.assertIn("__tradutorAuthPipelineTimer", self.auth_js)
        self.assertIn("auth-marketing-pipeline{", self.css)
        self.assertIn("pointer-events:none", self.css)
        pipeline_fragment = self.shell[self.shell.index('id="authMarketingPipeline"'):]
        self.assertNotIn("<button", pipeline_fragment.split("</div>\n\n      <div class=\"auth-login-format-row\">")[0])
        self.assertNotIn("tabindex", pipeline_fragment.split("</div>\n\n      <div class=\"auth-login-format-row\">")[0])

    def test_auth_form_removes_top_tabs_and_adds_required_copy_and_benefits(self):
        auth_fragment = self.shell[self.shell.index('id="authSurface"'):]
        self.assertNotIn('class="auth-tabs"', auth_fragment)
        self.assertEqual(auth_fragment.count("data-authmode-link"), 1)
        for key in (
            "auth.hero.title",
            "auth.hero.description",
            "auth.hero.complement",
            "auth.benefit.session",
            "auth.benefit.pdfs",
            "auth.benefit.progress",
        ):
            self.assertIn(key, auth_fragment)
        self.assertIn("auth-login-recovery", auth_fragment)
        self.assertIn("hidden>Recuperar acesso", auth_fragment)
        self.assertIn("auth-login-flow-intro", auth_fragment)
        self.assertNotIn("Do link ao PDF, acompanhe cada etapa pelo painel.", auth_fragment)
        compare_pos = auth_fragment.index('class="auth-login-compare-wrap"')
        flow_pos = auth_fragment.index("auth-login-flow-intro")
        pipeline_pos = auth_fragment.index('id="authMarketingPipeline"')
        self.assertLess(compare_pos, flow_pos)
        self.assertLess(flow_pos, pipeline_pos)

    def test_loading_visual_test_mode_is_local_and_fail_closed(self):
        self.assertIn("visual_boot_stage", self.ui_js)
        self.assertIn("__tradutorVisualTestEnabled", read("app_ui.py"))
        self.assertIn("TRADUTOR_UI_VISUAL_TEST", read("app_ui.py"))
        self.assertIn("window.__tradutorVisualTestEnabled === true", self.ui_js)
        self.assertIn("['127.0.0.1', 'localhost', '::1']", self.ui_js)
        self.assertIn("dataset.visualBootTest = '1'", self.ui_js)
        self.assertIn("document.documentElement.dataset.visualBootTest = '1'", self.ui_js)
        self.assertIn("window.__tradutorVisualTestEnabled === true && document.documentElement.dataset.visualBootTest === '1'", self.auth_js)
        self.assertIn("if (bootVisualTest) return;", self.ui_js)
        self.assertIn("bootHighestStage", self.ui_js)
        self.assertIn("bootProgressBar", self.shell)
        self.assertIn("app-loading-progress", self.css)
        self.assertIn("visual_reduced_motion", self.ui_js)
        self.assertIn('data-visual-reduced-motion="1"', self.css)
        for stage in ("init: 1", "local: 2", "auth: 3", "session: 4", "profile: 5", "settings: 6", "community: 7", "ready: 8"):
            self.assertIn(stage, self.ui_js)
        self.assertIn("stage < 1 || stage > 8", self.ui_js)
        self.assertIn("raw === 'error'", self.ui_js)

    def test_visible_ui_strings_do_not_contain_known_mojibake(self):
        checked = {
            "app_ui.py": read("app_ui.py"),
            "ui/ui_shell.html": self.shell,
            "static/tradutor_ui.js": self.ui_js,
            "static/auth_ui.js": self.auth_js,
        }
        mojibake_patterns = (
            "\u00c3\u0192", "\u00c3\u201a", "\u00c3\u00a2\u00e2\u201a\u00ac", "\ufffd",
            "anÃ", "traduÃ", "configuraÃ", "publicaÃ", "revisÃ", "sessÃ",
            "nÃ£", "jÃ¡", "exibiÃ", "pÃ¡", "possÃ", "cabeÃ", "sÃ£",
        )
        for rel, text in checked.items():
            with self.subTest(file=rel):
                for pattern in mojibake_patterns:
                    self.assertNotIn(pattern, text)
        self.assertIn("Tentar nova análise", self.shell)
        self.assertIn("Esta revisão de fonte não pode mais ser repetida.", checked["app_ui.py"])
        self.assertIn("Os cabeçalhos do PDF não são seguros.", self.ui_js)

    def test_new_auth_and_loading_strings_exist_in_all_catalogs(self):
        required = {
            "auth.hero.title", "auth.hero.description", "auth.hero.complement",
            "auth.compare.automatic", "auth.compare.caption",
            "auth.pipeline.download", "auth.pipeline.ocr", "auth.pipeline.organize",
            "auth.pipeline.translate", "auth.pipeline.cache", "auth.pipeline.pdf",
            "auth.local_panel", "auth.service_connected", "auth.welcome_back",
            "auth.continue_work", "auth.no_account", "auth.create_local",
            "auth.benefit.session", "auth.benefit.pdfs", "auth.benefit.progress",
            "loading.title", "loading.subtitle",
        }
        for lang in ("pt-BR", "en-US", "es-ES", "fr-FR", "ja-JP", "ko-KR"):
            with self.subTest(lang=lang):
                keys = set(re.findall(r"'([^']+)'\s*:", read(f"static/i18n/{lang}.js")))
                self.assertTrue(required.issubset(keys))


if __name__ == "__main__":
    unittest.main()
