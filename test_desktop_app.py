from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

from pathlib import Path
import asyncio

import desktop_app
import app_ui


def test_desktop_defaults_are_loopback_and_current_port():
    assert desktop_app.DEFAULT_HOST == "127.0.0.1"
    assert desktop_app.DEFAULT_PORT > 0


def test_webview_profile_is_persistent_and_user_scoped(tmp_path, monkeypatch):
    local_app_data = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    first = desktop_app._webview_start_options()
    profile = Path(first["storage_path"])
    marker = profile / "session-marker.test"
    marker.write_text("launch-one", encoding="utf-8")
    second = desktop_app._webview_start_options()
    assert first["private_mode"] is False
    assert second == first
    assert marker.read_text(encoding="utf-8") == "launch-one"
    assert profile == local_app_data / "YomuSekai" / "webview-profile"
    assert profile.is_relative_to(local_app_data)


def test_web_mode_csp_allows_only_canonical_ad_frame_hosts():
    messages = []

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def send(message):
        messages.append(message)

    middleware = app_ui.SecurityHeadersMiddleware(inner)
    asyncio.run(middleware({"type": "http", "path": "/"}, None, send))
    headers = {key.lower(): value for key, value in messages[0]["headers"]}
    csp = headers[b"content-security-policy"].decode("latin-1")
    frame_src = next(part for part in csp.split(";") if part.strip().startswith("frame-src "))
    assert "https://henriquepvar.github.io" in frame_src
    assert "https://yomusekai.com.br" in frame_src
    assert "https://game-deals-alpha.vercel.app" in frame_src
    frame_sources = set(frame_src.split()[1:])
    assert "https:" not in frame_sources
    assert "*" not in frame_sources
    assert frame_sources <= {
        "'self'", app_ui._SUPABASE_CSP_ORIGIN,
        "https://henriquepvar.github.io", "https://yomusekai.com.br",
        "https://game-deals-alpha.vercel.app",
    }


def test_desktop_url_is_loopback():
    assert desktop_app._url("127.0.0.1", 8080) == "http://127.0.0.1:8080/"
    assert desktop_app._desktop_url("127.0.0.1", 8080).endswith("/?desktop=1")


def test_desktop_launcher_has_webview2_and_health_lifecycle():
    source = Path("desktop_app.py").read_text(encoding="utf-8")
    assert "gui=\"edgechromium\"" in source
    assert "icon=str(APP_ICON_PATH)" in source
    assert "_wait_ready" in source
    assert "_stop_server" in source
    assert "subprocess.Popen" in source
    assert 'env["TRADUTOR_RUNTIME_MODE"] = "desktop"' in source
    assert "**_webview_start_options()" in source


def test_desktop_auth_handoff_is_memory_only_and_single_use():
    source = Path("app_ui.py").read_text(encoding="utf-8")
    assert "_DESKTOP_HANDOFFS" in source
    assert "secrets.token_urlsafe" in source
    assert "_DESKTOP_HANDOFF_TTL_SECONDS = 600" in source
    assert "/api/auth/desktop/handoff/start" in source
    assert "/api/auth/desktop/handoff/{handoff_id}/consume" in source
    assert "Cache-Control\": \"no-store\"" in source
    assert "item.get(\"consumed\")" in source


def test_desktop_callback_completes_server_side_without_external_pkce():
    source = Path("app_ui.py").read_text(encoding="utf-8")
    assert "desktop = params.get(\"desktop\") == \"1\"" in source
    assert "Solicitação recebida" in source
    assert "Você pode voltar ao Yomu Sekai" in source
    callback = Path("ui/auth_callback.html").read_text(encoding="utf-8")
    assert "desktopHandoff" in callback


def test_client_handoff_state_is_sanitized_and_single_source():
    source = Path("static/auth_ui.js").read_text(encoding="utf-8")
    assert "const desktopHandoffState" in source
    assert "window.__tradutorDesktopHandoffState" in source
    assert "idPresent: Boolean(desktopHandoffState.handoffId)" in source
    assert "desktopHandoffState.handoffId" in source
    assert "HANDOFF_ID_VALUE" not in source


def test_server_handoff_diagnostics_never_return_capability_values():
    source = Path("app_ui.py").read_text(encoding="utf-8")
    assert "/api/auth/desktop/handoff/diagnostics" in source
    assert '"has_auth_code": bool(ready)' in source
    assert '"active_count"' in source
    assert '"ready_count"' in source
    assert "/api/auth/desktop/client-state" in source
    assert '"current_runtime_state"' in source
    assert '"code": code' not in source[source.find('def desktop_handoff_diagnostics'):source.find('def desktop_handoff_consume')]


def test_recovery_password_render_reenables_shared_submit_button():
    source = Path("static/auth_ui.js").read_text(encoding="utf-8")
    assert "submit.disabled = false" in source
    assert "recoveryPasswordView && submit.dataset.busy !== '1'" in source


def test_official_branding_assets_are_preserved_and_ready_for_packaging():
    source = Path("assets/branding/yomu-sekai-logo.png")
    ico = Path("assets/branding/generated/yomu-sekai.ico")
    assert source.exists()
    assert ico.exists()
    assert source.stat().st_size > 1_000_000
    assert ico.stat().st_size > 10_000


def test_desktop_uses_stable_windows_app_user_model_id():
    source = Path("desktop_app.py").read_text(encoding="utf-8")
    assert "YomuSekai.Desktop" in source


def test_favicon_and_packaging_reference_yomu_sekai_icon():
    app_source = Path("app_ui.py").read_text(encoding="utf-8")
    spec = Path("packaging/tradutor_ia.spec").read_text(encoding="utf-8")
    iss = Path("packaging/TradutorIA.iss").read_text(encoding="utf-8")
    assert "yomu-sekai.ico" in app_source
    assert "yomu-sekai.ico" in spec
    assert "SetupIconFile" in iss and "yomu-sekai.ico" in iss


def test_desktop_rejects_non_loopback_cli():
    try:
        desktop_app.main(["--host", "0.0.0.0"])
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover
        raise AssertionError("non-loopback host should be rejected")


def test_external_provider_config_is_read_only_from_user_data(tmp_path, monkeypatch):
    config = tmp_path / ".env"
    config.write_text(
        "COMMUNITY_STORAGE_PROVIDER=google_drive\n"
        "COMMUNITY_DRIVE_ROOT_FOLDER_ID=root-id\n"
        "GOOGLE_OAUTH_CLIENT_ID=client-id\n"
        "GOOGLE_OAUTH_CLIENT_SECRET=client-secret\n"
        "GOOGLE_OAUTH_TOKEN_PATH=C:/private/token.json\n"
        "SUPABASE_SECRET_KEY=must-not-load\n",
        encoding="utf-8",
    )
    env = {"TRADUTOR_EXTERNAL_CONFIG": str(config)}
    desktop_app._load_external_provider_config(env)
    assert env["COMMUNITY_STORAGE_PROVIDER"] == "google_drive"
    assert env["COMMUNITY_DRIVE_ROOT_FOLDER_ID"] == "root-id"
    assert env["GOOGLE_OAUTH_CLIENT_ID"] == "client-id"
    assert env["GOOGLE_OAUTH_CLIENT_SECRET"] == "client-secret"
    assert env["GOOGLE_OAUTH_TOKEN_PATH"].endswith("token.json")
    assert "SUPABASE_SECRET_KEY" not in env
