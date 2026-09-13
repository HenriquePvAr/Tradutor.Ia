from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

from pathlib import Path


AUTH_UI = Path(__file__).parent / "static" / "auth_ui.js"


def _diagnostic_block() -> str:
    source = AUTH_UI.read_text(encoding="utf-8")
    start = source.index("const AUTH_RECOVERY_DIAGNOSTIC_KEY")
    end = source.index("let authPresentation", start)
    return source[start:end]


def test_recovery_diagnostic_is_durable_and_allowlisted():
    block = _diagnostic_block()
    assert "localStorage.setItem(AUTH_RECOVERY_DIAGNOSTIC_KEY" in block
    for field in ("timestamp", "operation", "name", "status", "code", "message"):
        assert f"{field}:" in block
    for forbidden in ("email:", "password:", "access_token:", "refresh_token:", "Authorization:", "apikey:", "service_role:"):
        assert forbidden not in block


def test_recovery_diagnostic_masks_pii_and_clears_success():
    block = _diagnostic_block()
    assert "sanitizeRecoveryMessage" in block
    assert "replace(/[A-Z0-9._%+-]+@" in block
    assert "replace(/https?:\\/\\/" in block
    assert "clearRecoveryDiagnostic();" in AUTH_UI.read_text(encoding="utf-8")
    assert "persistRecoveryDiagnostic(null, 'success');" in AUTH_UI.read_text(encoding="utf-8")


def test_recovery_failure_persists_before_ui_fallback():
    source = AUTH_UI.read_text(encoding="utf-8")
    assert source.index("persistRecoveryDiagnostic(err);") < source.index("setError(status === 429", source.index("persistRecoveryDiagnostic(err);"))


def test_recovery_priority_and_cooldown_contract():
    source = AUTH_UI.read_text(encoding="utf-8")
    assert "authEvent === 'PASSWORD_RECOVERY'" in source
    assert "yomu_recovery_intent" in source
    assert "showRecoveryView('password')" in source
    assert "RECOVERY_COOLDOWN_SECONDS = 60" in source
    assert "RECOVERY_COOLDOWN_KEY" in source
    assert "Enviar novo link em 00:" in source
    assert "Enviar novo link'" in source
    assert "sessionStorage.setItem(RECOVERY_COOLDOWN_KEY" in source


def test_recovery_destination_resolver_has_session_priority():
    source = AUTH_UI.read_text(encoding="utf-8")
    assert "function resolveAuthDestination" in source
    assert "destination === 'RECOVERY_PASSWORD'" in source
    assert "recovery_priority" in source
    assert "auth_event" in source
    assert "recovery_intent_valid" in source


def test_recovery_marker_is_not_cleared_by_auth_events():
    source = AUTH_UI.read_text(encoding="utf-8")
    render = source[source.index("function renderSession"):source.index("async function init")]
    # The cleanup helper is intentionally adjacent to renderSession; ensure
    # the event resolver itself does not clear markers implicitly.
    render_logic = render.split("function clearRecoveryContext", 1)[0]
    assert "sessionStorage.removeItem('yomu_recovery_intent')" not in render_logic
    assert "authEvent === 'INITIAL_SESSION'" in render
    assert "authEvent === 'SIGNED_IN'" in source


def test_callback_persists_recovery_event_before_redirect():
    callback = (Path(__file__).parent / "ui" / "auth_callback.html").read_text(encoding="utf-8")
    assert "PASSWORD_RECOVERY" in callback
    assert "yomu_recovery_intent" in callback
    assert "exchangeCodeForSession(code)" in callback
    assert "recovery_session_not_established" in callback
    assert "history.replaceState" in callback


def test_callback_diagnostic_is_sanitized_and_durable():
    callback = (Path(__file__).parent / "ui" / "auth_callback.html").read_text(encoding="utf-8")
    assert "callbackDiagnosticKey" in callback
    assert "localStorage.setItem(callbackDiagnosticKey" in callback
    assert "error?.status" in callback and "error?.code" in callback
    assert "error?.message" in callback
    assert "replace(/[A-Z0-9._%+-]+@" in callback
    for forbidden in ("token_hash:", "password:", "access_token:", "refresh_token:"):
        assert forbidden not in callback


def test_callback_disables_auto_url_detection_for_single_pkce_exchange():
    callback = (Path(__file__).parent / "ui" / "auth_callback.html").read_text(encoding="utf-8")
    auth = (Path(__file__).parent / "static" / "supabase_auth.js").read_text(encoding="utf-8")
    assert "getSupabaseClient({detectSessionInUrl: false})" in callback
    assert "options.detectSessionInUrl !== false" in auth


def test_recovery_success_finalizes_session_before_normal_login():
    source = AUTH_UI.read_text(encoding="utf-8")
    assert "RECOVERY_SIGNOUT_STARTED" in source
    assert "RECOVERY_SIGNOUT_SUCCESS" in source
    assert "requireExplicitLogin = true" in source
    assert "await withTimeout(authApi.signOut()" in source


def test_explicit_login_cannot_be_bypassed_by_residual_session():
    source = AUTH_UI.read_text(encoding="utf-8")
    assert "explicitLoginSubmissionInProgress" in source
    assert "LOGIN_SUBMIT_CLICKED" in source
    assert "SIGN_IN_WITH_PASSWORD_STARTED" in source
    assert "SIGN_IN_WITH_PASSWORD_SUCCESS" in source
    assert "SIGN_IN_WITH_PASSWORD_FAILURE" in source
    assert "login_session_gate_blocked" in source
    assert "app_open_reason: 'explicit_login'" in source


def test_login_failure_restores_editable_credentials_and_clears_password():
    source = AUTH_UI.read_text(encoding="utf-8")
    assert "function setAuthCredentialFieldsBusy" in source
    assert "function recoverLoginFormAfterFailure" in source
    assert "password_cleared: true" in source
    assert "email_editable" in source
    assert "password_editable" in source
    assert "recoverLoginFormAfterFailure(status === 400 || status === 401 ? 'invalid_credentials' : 'request_failed')" in source
    assert "setAuthCredentialFieldsBusy(false);" in source


def test_auth_runtime_markers_are_set_at_real_execution_points():
    source = AUTH_UI.read_text(encoding="utf-8")
    assert "window.__tradutorAuthScriptExecuted = true" in source
    assert "window.__tradutorAuthInitStarted = true" in source
    assert "window.__tradutorAuthInitCompleted = true" in source


def test_logout_clears_recovery_context_and_url():
    source = AUTH_UI.read_text(encoding="utf-8")
    assert "function clearRecoveryContext" in source
    assert "url.searchParams.delete(AUTH_MODE_PARAM)" in source
    assert "url.searchParams.delete('recovery')" in source
    assert "clearRecoveryContext();" in source


def test_recovery_password_visibility_is_delegated_and_expiration_is_visible():
    source = AUTH_UI.read_text(encoding="utf-8")
    assert "authToggleRecoveryPasswordConfirm" in source
    assert "event.preventDefault();" in source
    assert "RECOVERY_EXPIRED_OR_INVALID" in source
    assert "Esse link não é mais válido" in source


def test_recovery_success_enter_cleans_url_and_returns_to_login():
    source = AUTH_UI.read_text(encoding="utf-8")
    assert "function handleRecoverySuccessEnter" in source
    assert "recovery_success_exit_to_login" in source
    assert "handleRecoverySuccessEnter();" in source
    assert "url.searchParams.delete('recovery')" in source
    assert "url.searchParams.delete('code')" in source
    assert "url.searchParams.delete('type')" in source


def test_recovery_error_messages_are_user_facing_and_specific():
    source = AUTH_UI.read_text(encoding="utf-8")
    assert "Escolha uma senha diferente" in source
    assert "Link de recuperação expirado" in source
    assert "Link de recuperação inválido" in source
    assert "Senha muito fraca" in source
    assert "Não foi possível atualizar sua senha agora" in source


def test_auth_preview_is_multi_area_carousel():
    shell = (Path(__file__).parent / "ui" / "ui_shell.html").read_text(encoding="utf-8")
    css = (Path(__file__).parent / "static" / "tradutor_ui.css").read_text(encoding="utf-8")
    assert 'id="authProductCarousel"' in shell
    for label in ("Início", "Nova tradução", "Fila", "Capítulos traduzidos", "Leitor", "Comunidade", "Perfil"):
        assert label in shell
    assert "authProductSlide" in css
    assert "animation:authProductSlide" in css


def test_auth_preview_clones_full_application_shell():
    source = AUTH_UI.read_text(encoding="utf-8")
    shell = (Path(__file__).parent / "ui" / "ui_shell.html").read_text(encoding="utf-8")
    assert "const shellSource = document.querySelector('.shell')" in source
    assert "slide.className = `shell auth-product-real-slide" in source
    assert 'data-preview-sources="view-inicio,view-nova,view-queue' in shell
    assert "slide.querySelectorAll('.rail-tab')" in source
