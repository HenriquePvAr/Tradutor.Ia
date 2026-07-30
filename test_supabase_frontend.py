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
        self.assertIn("getGlobal('__tradutorAccessToken')", source)
        self.assertIn("setGlobal('__tradutorAccessToken'", source)
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

    def test_auth_ui_resolves_backend_session_before_visitor_state(self):
        source = _read("static/auth_ui.js")
        self.assertIn("/api/community/auth/session", source)
        self.assertIn("auth_loading", source)
        self.assertIn("session_expired", source)
        self.assertIn("credentials: 'same-origin'", source)

    def test_auth_bootstrap_defines_state_before_dynamic_dependency_import(self):
        source = _read("static/auth_ui.js")
        self.assertIn("window.__tradutorAuthState = 'auth_loading'", source)
        self.assertIn("import(`/static/auth_provider.js?v=${Date.now()}`)", source)
        self.assertIn("supabase_auth.js", _read("static/auth_provider.js"))
        self.assertIn("AUTH_BOOTSTRAP_TIMEOUT_MS", source)
        self.assertIn("withTimeout", source)
        self.assertIn("renderAuthShell('auth_error')", source)
        self.assertNotIn("from '/static/supabase_auth.js'", source)

    def test_auth_shell_clears_stale_identity_while_loading(self):
        source = _read("static/auth_ui.js")
        self.assertIn("Verificando sessão", source)
        self.assertIn("window.__tradutorAccessToken = ''", source)
        self.assertIn("if (state !== 'authenticated') window.__tradutorAccessToken = ''", source)
        self.assertIn("logoutBtn.hidden = state !== 'authenticated'", source)

    def test_authenticated_shell_preserves_bearer_for_community_requests(self):
        source = _read("static/auth_ui.js")
        self.assertIn(
            "if (state !== 'authenticated') window.__tradutorAccessToken = ''",
            source,
        )

    def test_login_submission_is_abortable_and_always_restores_button(self):
        source = _read("static/auth_ui.js")
        self.assertIn("new AbortController()", source)
        self.assertIn("controller.abort()", source)
        self.assertIn("finally", source)
        # The redesign moved the coercion onto its own line and set the ARIA
        # state beside it. The guarantee is unchanged and still asserted here:
        # the disabled flag is the loading flag and nothing else.
        self.assertIn("const isLoading = Boolean(loading)", source)
        self.assertIn("button.disabled = isLoading", source)
        self.assertIn("aria-disabled", source)
        self.assertIn("setSubmitLoading(submit, false", source)
        self.assertIn("submit.dataset.busy", source)

    def test_login_reconciles_canonical_session_without_reload(self):
        source = _read("static/auth_ui.js")
        for marker in (
            "AUTH_LOGIN_TIMEOUT_MS",
            "establishCanonicalSession",
            "session_not_established",
            "canonical_session_confirmed",
            "login_completed",
            "login_reconciled_after_timeout",
            "auth_timeout",
            "__tradutorAuthStore",
        ):
            self.assertIn(marker, source)

    def test_login_timeout_covers_complete_flow_and_top_level_finally(self):
        source = _read("static/auth_ui.js")
        self.assertIn("completeLoginFlow", source)
        self.assertIn("withTimeout(completeLoginFlow(email, password, controller.signal), AUTH_LOGIN_TIMEOUT_MS)", source)
        self.assertIn("login_request_started", source)
        self.assertIn("canonical_session_refresh_started", source)
        self.assertIn("login_finally_executed", source)

    def test_login_has_single_guarded_handler_and_recreates_abort_controller(self):
        source = _read("static/auth_ui.js")
        self.assertEqual(source.count("$('#authForm')?.addEventListener('submit'"), 1)
        self.assertIn("window.__tradutorAuthHandlersBound", source)
        self.assertIn("window.__tradutorActiveLoginController = controller", source)
        self.assertIn("auth_ui:canonical-submit-v3", source)

    def test_logout_clears_local_auth_even_when_sdk_signout_is_slow(self):
        auth = _read("static/auth_ui.js")
        sdk = _read("static/supabase_auth.js")
        self.assertIn("withTimeout(authApi.signOut(), AUTH_BOOTSTRAP_TIMEOUT_MS)", auth)
        self.assertIn("window.__tradutorAccessToken = ''", auth)
        self.assertIn("state: 'unauthenticated'", auth)
        self.assertIn("client.auth.signOut({scope: 'local'})", sdk)
        self.assertIn("SDK_SESSION_TIMEOUT_MS", sdk)

    def test_auth_asset_exposes_sanitized_build_marker(self):
        source = _read("static/auth_ui.js")
        self.assertIn("window.__tradutorAuthBuild", source)
        self.assertIn("new URL(import.meta.url)", source)

    def test_auth_heartbeat_keeps_refresh_and_canonical_state_in_sync(self):
        source = _read("static/auth_ui.js")
        self.assertIn("startAuthHeartbeat", source)
        self.assertIn("currentAccessToken", source)
        self.assertIn("auth_heartbeat_lost", source)

    def test_login_errors_have_stable_user_messages(self):
        source = _read("static/auth_ui.js")
        for message in (
            "E-mail ou senha inválidos.",
            "Esta conta não tem permissão para entrar.",
            "O login demorou para responder. Tente novamente.",
            "Não foi possível conectar ao serviço de autenticação.",
            "Não foi possível concluir o login.",
        ):
            self.assertIn(message, source)

    def test_sign_in_passes_abort_signal_to_auth_request(self):
        source = _read("static/supabase_auth.js")
        self.assertIn("export async function signIn(email, password, { signal } = {})", source)
        self.assertIn("signal,", source)
        self.assertIn("grant_type=password", source)

    def test_supabase_transport_reports_response_and_bounds_sdk_session_persistence(self):
        source = _read("static/supabase_auth.js")
        for marker in (
            "sign_in_started",
            "sign_in_response_received",
            "sign_in_error_received",
            "SDK_SESSION_TIMEOUT_MS",
            "sdk_sign_in_started",
            "sdk_sign_in_finished",
            "rest_sign_in_fallback",
            "memorySession",
            "getUser",
            "get_user_finished",
            "getCanonicalAccessToken",
        ):
            self.assertIn(marker, source)
        self.assertIn("__tradutorGetCanonicalAccessToken", _read("static/auth_ui.js"))

    def test_supabase_signin_uses_official_sdk_persistence_before_rest_fallback(self):
        source = _read("static/supabase_auth.js")
        self.assertIn("client.auth.signInWithPassword", source)
        self.assertIn("sdk_sign_in_started", source)
        self.assertIn("sdk_sign_in_finished", source)
        self.assertIn("rest_sign_in_fallback", source)
        self.assertIn("client.auth.setSession", source)

    def test_supabase_session_uses_stable_official_storage_and_restores_identity(self):
        source = _read("static/supabase_auth.js")
        for marker in (
            "persistSession: true",
            "autoRefreshToken: true",
            "storage: window.localStorage",
            "storageKey: stableStorageKey(cfg.supabase_url)",
            "function stableStorageKey",
            "client.auth.getSession()",
            "client.auth.getUser()",
            "INITIAL_SESSION_RESTORED",
            "sdk_session_persisted",
        ):
            self.assertIn(marker, source, marker)

    def test_pdf_popup_flow_does_not_replace_the_authenticated_tab(self):
        source = _read("static/tradutor_ui.js")
        helper = source[source.index("async function openAuthenticatedCommunityPdf"):source.index("async function loadCommunityFeed")]
        self.assertIn("window.open('', '_blank')", helper)
        self.assertIn("viewer.opener = null", helper)
        self.assertIn("viewer.location.href = objectUrl", helper)
        self.assertNotIn("window.location", helper)
        self.assertIn("viewer.close()", helper)

    def test_community_helper_resolves_token_asynchronously(self):
        source = _read("static/tradutor_ui.js")
        self.assertIn("const canonicalAccessToken = getGlobal('__tradutorGetCanonicalAccessToken')", source)
        self.assertIn("await canonicalAccessToken()", source)
        self.assertNotIn("const bearer = window.__tradutorAccessToken || ''", source)
        for marker in (
            "community_request_started",
            "authorization_header_present",
            "auth_transport",
            "reason_code",
        ):
            self.assertIn(marker, source)

    def test_social_helper_uses_canonical_async_token_provider(self):
        source = _read("static/social_api.js")
        self.assertIn("getCanonicalAccessToken", source)
        self.assertNotIn("import { currentAccessToken }", source)

    def test_public_config_exposes_only_sanitized_project_identity(self):
        source = _read("supabase_auth.py")
        for marker in ("hostname", "project_ref", "auth_method", "configured"):
            self.assertIn(marker, source)
        self.assertIn("build_version", _read("community_http.py"))

    def test_auth_asset_is_versioned_by_file_mtime(self):
        app_source = _read("app_ui.py")
        self.assertIn("AUTH_UI_ASSET", app_source)
        self.assertIn("st_mtime_ns", app_source)
        self.assertIn("path.relative_to(STATIC_DIR).as_posix()", app_source)
        self.assertIn("return f\"/static/{rel}?v={version}\"", app_source)

    def test_tradutor_refreshes_bootstrap_after_auth_change(self):
        source = _read("static/tradutor_ui.js")
        self.assertIn("tradutor-auth-changed", source)
        self.assertIn("void refreshBootstrap()", source)

    def test_shell_has_auth_controls(self):
        shell = _read("ui/ui_shell.html")
        for anchor in ("authSurface", "authOpenBtn", "authLogoutBtn",
                       "authForm", "authStatus"):
            self.assertIn(anchor, shell)
        self.assertNotIn("authModalOverlay", shell)

    def test_callback_route_registered(self):
        app_source = _read("app_ui.py")
        self.assertIn('/auth/callback', app_source)
        self.assertIn("auth_callback.html", app_source)


if __name__ == "__main__":
    unittest.main()
