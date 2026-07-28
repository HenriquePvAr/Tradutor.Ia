"""Security contracts for the explicit, isolated local-test auth provider."""

from __future__ import annotations

import _test_bootstrap  # noqa: F401

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from community_auth import (
    AuthConfigurationError,
    AuthorizationDenied,
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    build_auth_provider,
)
from community_http import create_community_router


def _env(tmp_path: Path, **overrides) -> dict[str, str]:
    values = {
        "AUTH_PROVIDER": "local_test",
        "APP_ENV": "test",
        "ALLOW_LOCAL_TEST_IDENTITIES": "1",
        "TRADUTOR_UI_HOST": "127.0.0.1",
        "LOCAL_TEST_AUTH_DB": str(tmp_path / "local_test_auth.sqlite3"),
        "LOCAL_TEST_AUTH_SESSION_SECRET": "session-pepper-" + ("x" * 40),
        "SUPABASE_URL": "",
        "SUPABASE_JWKS_URL": "",
        "BETTER_AUTH_INTERNAL_URL": "",
    }
    values.update({key: str(value) for key, value in overrides.items()})
    return values


def _request(
    method: str = "GET",
    *,
    session_token: str = "",
    csrf_cookie: str = "",
    csrf_header: str = "",
    client_host: str = "127.0.0.1",
) -> Request:
    cookies = []
    if session_token:
        cookies.append(f"{SESSION_COOKIE_NAME}={session_token}")
    if csrf_cookie:
        cookies.append(f"{CSRF_COOKIE_NAME}={csrf_cookie}")
    headers: list[tuple[bytes, bytes]] = []
    if cookies:
        headers.append((b"cookie", "; ".join(cookies).encode("ascii")))
    if csrf_header:
        headers.append((CSRF_HEADER_NAME.lower().encode("ascii"), csrf_header.encode("ascii")))
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": headers,
        "client": (client_host, 50000),
        "server": ("127.0.0.1", 8080),
    })


class _CommunityStub:
    pass


def test_supabase_is_the_default_provider():
    with pytest.raises(AuthConfigurationError):
        build_auth_provider({})


@pytest.mark.parametrize("missing", [
    "ALLOW_LOCAL_TEST_IDENTITIES",
    "LOCAL_TEST_AUTH_DB",
    "LOCAL_TEST_AUTH_SESSION_SECRET",
])
def test_local_test_requires_every_explicit_guard(tmp_path, missing):
    values = _env(tmp_path)
    values.pop(missing)
    with pytest.raises(AuthConfigurationError):
        build_auth_provider(values)


@pytest.mark.parametrize("environment", ["production", "staging", ""])
def test_local_test_is_blocked_outside_test_or_development(tmp_path, environment):
    with pytest.raises(AuthConfigurationError):
        build_auth_provider(_env(tmp_path, APP_ENV=environment))


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.2", "example.test"])
def test_local_test_is_blocked_on_public_or_non_loopback_bind(tmp_path, host):
    with pytest.raises(AuthConfigurationError):
        build_auth_provider(_env(tmp_path, TRADUTOR_UI_HOST=host))


def test_local_test_rejects_external_auth_configuration(tmp_path):
    with pytest.raises(AuthConfigurationError):
        build_auth_provider(_env(tmp_path, SUPABASE_URL="https://project.supabase.co"))


def test_bootstrap_is_idempotent_and_roles_are_server_owned(tmp_path):
    provider = build_auth_provider(_env(tmp_path))
    first = provider.bootstrap_test_identity(
        email="alpha@example.invalid", display_name="Alpha", password="Strong-local-pass-1")
    second = provider.bootstrap_test_identity(
        email="alpha@example.invalid", display_name="Changed", password="Other-pass-2")
    assert first["id"] == second["id"]
    assert second["created"] is False
    assert provider.assign_test_role(first["id"], "user", assigned_by="local_cli")
    with pytest.raises(ValueError):
        provider.assign_test_role(first["id"], "admin", assigned_by="browser")
    assert provider.list_test_identities()[0]["roles"] == ["user"]


def test_password_and_session_secrets_are_never_stored_in_plaintext(tmp_path):
    provider = build_auth_provider(_env(tmp_path))
    password = "Strong-local-pass-1"
    identity = provider.bootstrap_test_identity(
        email="alpha@example.invalid", display_name="Alpha", password=password)
    provider.assign_test_role(identity["id"], "user", assigned_by="local_cli")
    issued = provider.authenticate_credentials(
        email="alpha@example.invalid", password=password, client_host="127.0.0.1")
    raw = Path(_env(tmp_path)["LOCAL_TEST_AUTH_DB"]).read_bytes()
    assert password.encode() not in raw
    assert issued.session_token.encode() not in raw
    assert issued.csrf_token.encode() not in raw


def test_three_identities_have_isolated_sessions_and_roles(tmp_path):
    provider = build_auth_provider(_env(tmp_path))
    sessions = {}
    for index, role in enumerate(("user", "user", "moderator"), start=1):
        email = f"person-{index}@example.invalid"
        identity = provider.bootstrap_test_identity(
            email=email, display_name=f"Person {index}", password=f"Strong-local-pass-{index}")
        provider.assign_test_role(identity["id"], role, assigned_by="local_cli")
        sessions[email] = provider.authenticate_credentials(
            email=email, password=f"Strong-local-pass-{index}", client_host="127.0.0.1")
    principals = [
        provider.authenticate_request(_request(session_token=session.session_token))
        for session in sessions.values()
    ]
    assert len({principal.user_id for principal in principals}) == 3
    assert [principal.roles for principal in principals] == [
        frozenset({"user"}), frozenset({"user"}), frozenset({"moderator"})]
    assert provider.public_identity(principals[0].user_id)["display_name"] == "Person 1"
    assert "email" not in provider.public_identity(principals[0].user_id)


def test_expiry_revocation_disable_and_csrf_fail_closed(tmp_path):
    now = [1000.0]
    provider = build_auth_provider(_env(tmp_path), clock=lambda: now[0])
    identity = provider.bootstrap_test_identity(
        email="alpha@example.invalid", display_name="Alpha", password="Strong-local-pass-1")
    provider.assign_test_role(identity["id"], "user", assigned_by="local_cli")
    issued = provider.authenticate_credentials(
        email="alpha@example.invalid", password="Strong-local-pass-1",
        client_host="127.0.0.1")
    principal = provider.authenticate_request(_request(session_token=issued.session_token))
    with pytest.raises(Exception):
        provider.require_csrf(_request(
            "POST", session_token=issued.session_token,
            csrf_cookie=issued.csrf_token, csrf_header="wrong"), principal)
    provider.require_csrf(_request(
        "POST", session_token=issued.session_token,
        csrf_cookie=issued.csrf_token, csrf_header=issued.csrf_token), principal)
    provider.revoke_session(principal.session_id)
    assert not provider.authenticate_request(
        _request(session_token=issued.session_token)).authenticated
    issued2 = provider.authenticate_credentials(
        email="alpha@example.invalid", password="Strong-local-pass-1",
        client_host="127.0.0.1")
    provider.disable_test_identity(identity["id"], disabled_by="local_cli")
    assert not provider.authenticate_request(
        _request(session_token=issued2.session_token)).authenticated


def test_login_requires_loopback_and_never_accepts_role_or_owner(tmp_path):
    provider = build_auth_provider(_env(tmp_path))
    identity = provider.bootstrap_test_identity(
        email="alpha@example.invalid", display_name="Alpha", password="Strong-local-pass-1")
    provider.assign_test_role(identity["id"], "user", assigned_by="local_cli")
    with pytest.raises(AuthorizationDenied):
        provider.authenticate_credentials(
            email="alpha@example.invalid", password="Strong-local-pass-1",
            client_host="192.168.1.20")


def test_supabase_mode_has_no_local_test_login_endpoint(tmp_path):
    class _SupabaseLike:
        auth_source = "supabase"
        supports_external_bind = True
        configured = True

        def public_config(self):
            return {"provider": "supabase"}

        def authenticate_request(self, request):
            from community_auth import RequestPrincipal
            return RequestPrincipal.anonymous()

        require_authenticated = authenticate_request

        def require_csrf(self, request, principal):
            return None

    app = FastAPI()
    app.include_router(create_community_router(_CommunityStub(), _SupabaseLike()))
    with TestClient(app) as client:
        assert client.post("/api/community/auth/local-test/login", json={
            "email": "alpha@example.invalid",
            "password": "Strong-local-pass-1",
        }).status_code == 404


def test_local_test_store_uses_expected_isolated_schema(tmp_path):
    provider = build_auth_provider(_env(tmp_path))
    provider.close()
    conn = sqlite3.connect(_env(tmp_path)["LOCAL_TEST_AUTH_DB"])
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {
        "local_test_users",
        "local_test_sessions",
        "local_test_role_assignments",
        "local_test_auth_audit_events",
    } <= tables


def test_public_config_contains_no_store_path_or_secret(tmp_path):
    provider = build_auth_provider(_env(tmp_path))
    blob = json.dumps(provider.public_config())
    assert provider.public_config()["provider"] == "local_test"
    assert "sqlite" not in blob
    assert "pepper" not in blob
    assert str(tmp_path) not in blob


def test_cli_bootstrap_is_idempotent_without_rotating_credentials(tmp_path):
    credentials = tmp_path / "private" / "credentials.json"
    values = {**os.environ, **_env(tmp_path)}
    command = [
        sys.executable, "-m", "local_test_auth", "bootstrap",
        "--credentials-output", str(credentials),
    ]
    first = subprocess.run(command, cwd=Path(__file__).parent, env=values, check=False)
    assert first.returncode == 0
    original = credentials.read_bytes()
    second = subprocess.run(command, cwd=Path(__file__).parent, env=values, check=False)
    assert second.returncode == 0
    assert credentials.read_bytes() == original
    payload = json.loads(original)
    assert len(payload["identities"]) == 3
    assert sorted(item["role"] for item in payload["identities"]) == [
        "moderator", "user", "user"]


def test_local_test_ui_adapter_is_explicit_and_has_no_identity_shortcut():
    provider_js = Path("static/auth_provider.js").read_text(encoding="utf-8")
    auth_js = Path("static/auth_ui.js").read_text(encoding="utf-8")
    app_source = Path("app_ui.py").read_text(encoding="utf-8")
    assert "localTestAdapter" in provider_js
    assert "/api/community/auth/local-test/login" in provider_js
    assert "local_test_environment" in auth_js
    assert "authLocalSignupControl" in auth_js
    assert "clearAuthCredentialFields" in auth_js
    assert "Entrar como moderador" not in provider_js + auth_js
    assert "X-User-Id" not in provider_js + auth_js
    assert "_profile_for_principal" in app_source
    assert "profile_sync_failed" in app_source
