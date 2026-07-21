"""Hermetic security coverage for community authentication and authorization."""

from __future__ import annotations

import _test_bootstrap  # noqa: F401

from dataclasses import FrozenInstanceError
from concurrent.futures import ThreadPoolExecutor
import json
import logging
from pathlib import Path
import socket

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

from community_api import CommunityApi
from community_auth import (
    AuthenticationRequired,
    AuthConfigurationError,
    AuthorizationDenied,
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    LocalSessionAuthProvider,
    RequestPrincipal,
    ResourceNotFound,
    SESSION_COOKIE_NAME,
    configured_bind_host,
    validate_bind_security,
)
from community_authorization import (
    authorize_admin_operation,
    authorize_manage_post,
    authorize_moderation,
    can_read_post,
)
from community_http import CommunityNetworkBoundaryMiddleware, create_community_router
from community_service import CommunityError
from community_storage import FilesystemStorageProvider
from community_store import FileStatus, Moderation, PostStatus, Visibility
from job_store import JobStatus, JobStore
from ui_history import UIHistoryStore
from offline_test_guard import (
    _loopback_test_connect,
    _loopback_test_connect_ex,
    _loopback_test_create_connection,
)


PRIVATE_BYTES = b"%PDF-private-authorization-boundary-" + b"p" * 137
PUBLIC_BYTES = b"%PDF-public-offline-" + bytes(range(64))


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


class ProviderFactorySpy:
    def __init__(self, root):
        self.backing = FilesystemStorageProvider(root)
        self.builds = 0
        self.open_calls = 0
        self.trash_calls = 0
        self.delete_calls = 0

    def reset(self) -> None:
        self.builds = self.open_calls = self.trash_calls = self.delete_calls = 0

    def factory(self):
        self.builds += 1
        return self

    def open_stream(self, file_id, *, start=None, end=None):
        self.open_calls += 1
        return self.backing.open_stream(file_id, start=start, end=end)

    def move_to_trash(self, file_id):
        self.trash_calls += 1
        return self.backing.move_to_trash(file_id)

    def delete_file(self, file_id):
        self.delete_calls += 1
        return self.backing.delete_file(file_id)


class SecurityHarness:
    def __init__(self, tmp_path):
        self.output_root = tmp_path / "output"
        self.jobs = JobStore(tmp_path / "jobs.sqlite3")
        self.spy = ProviderFactorySpy(tmp_path / "storage")
        self.api = CommunityApi(
            self.jobs,
            community_db_path=tmp_path / "community.sqlite3",
            output_root=self.output_root,
            read_provider_factory=self.spy.factory,
        )
        self.auth = LocalSessionAuthProvider(
            bootstrap_secret="local-bootstrap-secret-" + "x" * 32,
            bootstrap_user_id="owner-a",
            bootstrap_roles=("admin",),
        )
        self.owner = self.auth.issue_session(user_id="owner-a")
        self.other = self.auth.issue_session(user_id="user-b")
        self.admin = self.auth.issue_session(user_id="admin-a", roles=("admin",))
        self.moderator = self.auth.issue_session(user_id="moderator-a", roles=("moderator",))
        app = FastAPI()
        app.add_middleware(CommunityNetworkBoundaryMiddleware, auth=self.auth)
        app.include_router(create_community_router(self.api, self.auth))
        self.app = app
        self.client = TestClient(app, client=("127.0.0.1", 50000))
        self.public_id = self.seed_post(
            owner="owner-a", visibility=Visibility.PUBLIC, data=PUBLIC_BYTES,
            title="Public post")
        self.private_id = self.seed_post(
            owner="owner-a", visibility=Visibility.PRIVATE, data=PRIVATE_BYTES,
            title="Private title")
        self.private_file = self.api.store.file_for_post(self.private_id)
        self.spy.reset()

    def close(self):
        self.client.close()
        self.api.close()
        self.jobs.close()

    def seed_post(
        self,
        *,
        owner: str,
        visibility: str,
        data: bytes = b"%PDF-offline",
        status: str = PostStatus.PUBLISHED,
        moderation: str = Moderation.APPROVED,
        verified: bool = True,
        title: str = "Post",
    ) -> str:
        post_id = self.api.store.create_post(
            user_id=owner,
            visibility=visibility,
            title=title,
            series_title=title,
        )
        if verified:
            session = self.spy.backing.create_resumable_session(
                filename=f"{post_id}.pdf",
                mime_type="application/pdf",
                size=len(data),
                parent_id="offline",
            )
            uploaded = self.spy.backing.upload_chunk(session, 0, data)
            file_id = self.api.store.create_file(
                post_id=post_id,
                filename=f"{post_id}-private-name.pdf",
                mime_type="application/pdf",
                size_bytes=len(data),
                sha256=f"sha-{post_id}",
                storage_provider="filesystem",
            )
            self.api.store.update_file(
                file_id,
                upload_status=FileStatus.VERIFIED,
                storage_file_id=uploaded.file_id,
            )
        self.api.store.set_post_status(post_id, status, moderation_status=moderation)
        return post_id

    def create_finished_translation_job(
        self,
        *,
        owner: str,
        name: str = "owned-job",
        data: bytes = b"%PDF-owned-job",
        status: str = JobStatus.FINISHED,
        job_type: str = "translation",
        recorded_pdf_name: str = "chapter.pdf",
    ) -> tuple[str, str]:
        output_dir = self.output_root / name
        output_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = output_dir / recorded_pdf_name
        pdf_path.write_bytes(data)
        job_id = self.jobs.create_job(
            source_url="https://example.invalid/offline",
            output_dir=str(output_dir),
            command=["offline"],
            configuration={
                "job_type": job_type,
                "community_owner_id": owner,
            },
        )
        claimed = self.jobs.claim_next_job(f"worker-{name}", 1)
        assert claimed and claimed["id"] == job_id
        worker = claimed["worker_id"]
        self.jobs.transition(job_id, JobStatus.STARTING, expected_worker=worker)
        self.jobs.transition(job_id, JobStatus.RUNNING, expected_worker=worker)
        job = self.jobs.transition(
            job_id,
            status,
            expected_worker=worker,
            exit_code=0,
            pdf_path=str(pdf_path),
        )
        (output_dir / "job_manifest.json").write_text(
            json.dumps({
                "job_id": job_id,
                "run_id": job["run_id"],
                "status": status,
                "exit_code": 0,
                "pdf_path": str(pdf_path),
            }),
            encoding="utf-8",
        )
        return job_id, str(pdf_path)

    @staticmethod
    def headers(issued=None, *, csrf=False, extra=None):
        headers = dict(extra or {})
        if issued is not None:
            cookie = f"{SESSION_COOKIE_NAME}={issued.session_token}"
            if csrf:
                cookie += f"; {CSRF_COOKIE_NAME}={issued.csrf_token}"
                headers[CSRF_HEADER_NAME] = issued.csrf_token
            headers["Cookie"] = cookie
        return headers


@pytest.fixture
def harness(tmp_path):
    value = SecurityHarness(tmp_path)
    try:
        yield value
    finally:
        value.close()


@pytest.fixture(autouse=True)
def allow_only_asgi_loopback(monkeypatch):
    """TestClient needs a Windows loopback self-pipe; all non-loopback remains denied."""
    monkeypatch.setattr(socket.socket, "connect", _loopback_test_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _loopback_test_connect_ex)
    monkeypatch.setattr(socket, "create_connection", _loopback_test_create_connection)


def test_request_principal_is_immutable_and_typed():
    principal = RequestPrincipal("owner-a", True, frozenset({"admin"}), "test", "session-a")
    with pytest.raises(FrozenInstanceError):
        principal.user_id = "user-b"
    assert principal.roles == frozenset({"admin"})


def test_anonymous_principal_is_explicit_and_empty():
    principal = RequestPrincipal.anonymous()
    assert not principal.authenticated
    assert principal.user_id == ""
    assert principal.roles == frozenset()


def test_future_verified_jwt_principal_may_omit_session_id():
    principal = RequestPrincipal("supabase-sub", True, auth_source="supabase", session_id=None)
    assert principal.authenticated and principal.session_id is None


@pytest.mark.parametrize("user_id", ["", "with space", "../owner", "é"])
def test_invalid_user_ids_fail_closed(user_id):
    with pytest.raises(ValueError):
        RequestPrincipal(user_id, True, auth_source="test", session_id="session-a")


def test_roles_are_normalized_as_a_single_trusted_role():
    principal = RequestPrincipal("owner-a", True, roles="ADMIN", auth_source="test")
    assert principal.roles == frozenset({"admin"})


def test_backend_issues_random_server_side_session():
    auth = LocalSessionAuthProvider()
    first = auth.issue_session(user_id="owner-a")
    second = auth.issue_session(user_id="owner-a")
    assert first.session_token != second.session_token
    assert first.principal.user_id == "owner-a"
    assert first.session_token not in repr(first)
    assert first.csrf_token not in repr(first)


def test_invalid_session_cookie_is_anonymous():
    auth = LocalSessionAuthProvider()
    assert not auth.authenticate_request(_request(session_token="x" * 43)).authenticated


def test_invalid_session_cookie_gets_401_on_private_http(harness):
    response = harness.client.get(
        f"/api/community/posts/{harness.private_id}/pdf",
        headers={"Cookie": f"{SESSION_COOKIE_NAME}={'x' * 43}"},
    )
    assert response.status_code == 401
    assert harness.spy.builds == 0


def test_expired_session_returns_401(harness):
    harness.auth._clock = lambda: harness.owner.expires_at + 1
    response = harness.client.get(
        f"/api/community/posts/{harness.private_id}/pdf",
        headers=harness.headers(harness.owner),
    )
    assert response.status_code == 401
    assert harness.spy.builds == 0


def test_revoked_session_returns_401(harness):
    harness.auth.revoke_session(harness.owner.principal.session_id)
    response = harness.client.get(
        f"/api/community/posts/{harness.private_id}/pdf",
        headers=harness.headers(harness.owner),
    )
    assert response.status_code == 401
    assert harness.spy.builds == 0


def test_csrf_requires_cookie_and_header_from_same_active_session(harness):
    response = harness.client.post(
        f"/api/community/posts/{harness.private_id}/unpublish",
        headers=harness.headers(harness.owner),
    )
    assert response.status_code == 403
    assert harness.api.store.get_post(harness.private_id)["status"] == PostStatus.PUBLISHED


def test_mismatched_csrf_is_rejected(harness):
    headers = harness.headers(harness.owner, csrf=True)
    headers[CSRF_HEADER_NAME] = "wrong-csrf-token-" + "x" * 32
    response = harness.client.post(
        f"/api/community/posts/{harness.private_id}/unpublish",
        headers=headers,
    )
    assert response.status_code == 403
    assert harness.api.store.get_post(harness.private_id)["status"] == PostStatus.PUBLISHED


def test_csrf_from_a_different_session_is_rejected(harness):
    headers = harness.headers(harness.owner)
    headers["Cookie"] += f"; {CSRF_COOKIE_NAME}={harness.other.csrf_token}"
    headers[CSRF_HEADER_NAME] = harness.other.csrf_token
    response = harness.client.post(
        f"/api/community/posts/{harness.private_id}/unpublish",
        headers=headers,
    )
    assert response.status_code == 403


def test_valid_csrf_allows_owner_mutation(harness):
    response = harness.client.post(
        f"/api/community/posts/{harness.private_id}/unpublish",
        headers=harness.headers(harness.owner, csrf=True),
    )
    assert response.status_code == 200, response.text
    assert harness.api.store.get_post(harness.private_id)["status"] == PostStatus.UNPUBLISHED


def test_bootstrap_is_loopback_only_and_identity_is_server_configured(tmp_path):
    auth = LocalSessionAuthProvider(
        bootstrap_secret="b" * 40,
        bootstrap_user_id="fixed-owner",
        bootstrap_roles=("admin",),
    )
    issued = auth.bootstrap_session(_request(), "b" * 40)
    assert issued.principal.user_id == "fixed-owner"
    assert issued.principal.roles == frozenset({"admin"})
    with pytest.raises(AuthorizationDenied):
        auth.bootstrap_session(_request(client_host="192.0.2.10"), "b" * 40)


def test_bootstrap_unicode_secret_fails_as_authentication_not_type_error(harness):
    with pytest.raises(AuthenticationRequired):
        harness.auth.bootstrap_session(_request(), "é" * 40)


def test_bootstrap_response_sets_httponly_strict_cookies_without_body_token(harness):
    secret = "local-bootstrap-secret-" + "x" * 32
    response = harness.client.post(
        "/api/community/auth/local-session",
        headers={"X-Tradutor-Bootstrap-Secret": secret},
    )
    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    session_cookie = next(value for value in cookies if value.startswith(SESSION_COOKIE_NAME))
    csrf_cookie = next(value for value in cookies if value.startswith(CSRF_COOKIE_NAME))
    assert "HttpOnly" in session_cookie and "SameSite=strict" in session_cookie
    assert "HttpOnly" not in csrf_cookie and "SameSite=strict" in csrf_cookie
    assert "Path=/" in session_cookie and "Max-Age=900" in session_cookie
    session_token = session_cookie.split("=", 1)[1].split(";", 1)[0]
    csrf_token = csrf_cookie.split("=", 1)[1].split(";", 1)[0]
    assert session_token not in response.text
    assert csrf_token not in response.text
    assert "csrf" not in response.text.lower()
    assert response.headers["cache-control"] == "private, no-store"


def test_http_bootstrap_body_cannot_choose_identity_or_roles(harness):
    response = harness.client.post(
        "/api/community/auth/local-session",
        json={"user_id": "user-b", "roles": ["moderator"]},
        headers={
            "X-Tradutor-Bootstrap-Secret": "local-bootstrap-secret-" + "x" * 32,
        },
    )
    assert response.status_code == 200
    assert response.json()["user_id"] == "owner-a"
    assert response.json()["roles"] == ["admin"]


def test_auth_operations_emit_no_token_output(capsys, caplog):
    caplog.set_level(logging.DEBUG)
    auth = LocalSessionAuthProvider()
    issued = auth.issue_session(user_id="owner-a", roles=("admin",))
    auth.authenticate_request(_request(session_token=issued.session_token))
    auth.revoke_session(issued.principal.session_id)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    rendered_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert issued.session_token not in rendered_logs
    assert issued.csrf_token not in rendered_logs


def test_https_bootstrap_cookie_is_secure(tmp_path):
    jobs = JobStore(tmp_path / "jobs.sqlite3")
    api = CommunityApi(jobs, community_db_path=tmp_path / "community.sqlite3",
                       output_root=tmp_path / "output")
    auth = LocalSessionAuthProvider(
        bootstrap_secret="s" * 40, bootstrap_user_id="owner-a")
    app = FastAPI()
    app.add_middleware(CommunityNetworkBoundaryMiddleware, auth=auth)
    app.include_router(create_community_router(api, auth))
    client = TestClient(app, base_url="https://testserver", client=("127.0.0.1", 50000))
    try:
        response = client.post(
            "/api/community/auth/local-session",
            headers={"X-Tradutor-Bootstrap-Secret": "s" * 40},
        )
        assert response.status_code == 200
        cookies = response.headers.get_list("set-cookie")
        assert len(cookies) == 2
        assert all("Secure" in value for value in cookies)
    finally:
        client.close()
        api.close()
        jobs.close()


def test_session_metadata_is_never_cacheable(harness):
    response = harness.client.get(
        "/api/community/auth/session",
        headers=harness.headers(harness.other),
    )
    assert response.status_code == 200
    assert response.json()["user_id"] == "user-b"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Cookie"


def test_default_bind_is_loopback():
    assert configured_bind_host({}) == "127.0.0.1"


def test_external_bind_is_blocked_for_local_sessions_even_when_explicit():
    auth = LocalSessionAuthProvider(
        bootstrap_secret="s" * 40, bootstrap_user_id="owner-a")
    with pytest.raises(AuthConfigurationError):
        validate_bind_security("0.0.0.0", auth, allow_external=True)


def test_explicit_external_bind_accepts_only_configured_strong_provider():
    class StrongProvider:
        configured = True
        supports_external_bind = True

    assert validate_bind_security("0.0.0.0", StrongProvider(), allow_external=True) == "0.0.0.0"
    with pytest.raises(AuthConfigurationError):
        validate_bind_security("0.0.0.0", StrongProvider(), allow_external=False)


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_external_wildcard_bind_is_blocked_for_local_provider(host):
    auth = LocalSessionAuthProvider(
        bootstrap_secret="s" * 40, bootstrap_user_id="owner-a")
    with pytest.raises(AuthConfigurationError):
        validate_bind_security(host, auth, allow_external=True)


def test_runner_independent_middleware_rejects_external_peer(harness):
    remote = TestClient(harness.app, client=("192.0.2.20", 50000))
    try:
        response = remote.get(f"/api/community/posts/{harness.public_id}/pdf")
        assert response.status_code == 503
        assert response.json()["detail"] == "external_bind_not_authorized"
        assert harness.spy.builds == 0
    finally:
        remote.close()


def test_middleware_requires_explicit_opt_in_even_for_strong_provider(monkeypatch):
    monkeypatch.delenv("TRADUTOR_ALLOW_EXTERNAL_BIND", raising=False)
    class StrongProvider:
        configured = True
        supports_external_bind = True

    app = FastAPI()
    app.add_middleware(
        CommunityNetworkBoundaryMiddleware,
        auth=StrongProvider(),
    )

    @app.get("/ping")
    def ping():
        return {"ok": True}

    remote = TestClient(app, client=("192.0.2.20", 50000))
    try:
        assert remote.get("/ping").status_code == 503
    finally:
        remote.close()


def test_middleware_allows_explicit_external_bind_with_strong_provider(monkeypatch):
    monkeypatch.setenv("TRADUTOR_ALLOW_EXTERNAL_BIND", "1")
    class StrongProvider:
        configured = True
        supports_external_bind = True

    app = FastAPI()
    app.add_middleware(
        CommunityNetworkBoundaryMiddleware,
        auth=StrongProvider(),
    )

    @app.get("/ping")
    def ping():
        return {"ok": True}

    remote = TestClient(app, client=("192.0.2.20", 50000))
    try:
        assert remote.get("/ping").status_code == 200
    finally:
        remote.close()


def test_middleware_rejects_external_websocket_for_local_provider(harness):
    @harness.app.websocket("/security-test-websocket")
    async def security_test_websocket(websocket: WebSocket):
        await websocket.accept()
        await websocket.close()

    remote = TestClient(harness.app, client=("192.0.2.20", 50000))
    try:
        with pytest.raises(WebSocketDisconnect) as exc:
            with remote.websocket_connect("/security-test-websocket"):
                pass
        assert exc.value.code == 1008
    finally:
        remote.close()


@pytest.mark.parametrize("method,range_header", [
    ("GET", ""),
    ("HEAD", ""),
    ("GET", "bytes=0-9"),
    ("GET", "invalid"),
    ("GET", "bytes=0-1,4-5"),
])
def test_private_pdf_rejects_forged_other_user_before_storage(harness, method, range_header):
    headers = {"X-User-Id": "user-b"}
    if range_header:
        headers["Range"] = range_header
    response = harness.client.request(
        method,
        f"/api/community/posts/{harness.private_id}/pdf",
        headers=headers,
    )
    assert response.status_code == 401
    assert harness.spy.builds == harness.spy.open_calls == 0


@pytest.mark.parametrize("method,range_header", [
    ("GET", ""),
    ("HEAD", ""),
    ("GET", "bytes=0-9"),
    ("GET", "invalid"),
    ("GET", "bytes=0-1,4-5"),
])
def test_authenticated_other_user_gets_404_before_storage(harness, method, range_header):
    headers = harness.headers(harness.other)
    if range_header:
        headers["Range"] = range_header
    response = harness.client.request(
        method,
        f"/api/community/posts/{harness.private_id}/pdf",
        headers=headers,
    )
    assert response.status_code == 404
    assert harness.spy.builds == harness.spy.open_calls == 0


@pytest.mark.parametrize("method,extra", [
    ("GET", {}),
    ("HEAD", {}),
    ("GET", {"Range": "bytes=0-9"}),
])
def test_denied_response_leaks_no_private_storage_metadata(harness, method, extra):
    response = harness.client.request(
        method,
        f"/api/community/posts/{harness.private_id}/pdf",
        headers=harness.headers(harness.other, extra=extra),
    )
    lowered = response.text.lower()
    assert response.status_code == 404
    assert harness.private_file["storage_file_id"] not in response.text
    assert harness.private_file["filename"] not in response.text
    assert harness.private_file["sha256"] not in response.text
    assert "private title" not in lowered
    assert response.headers.get("content-length") != str(len(PRIVATE_BYTES))
    assert "content-range" not in response.headers
    assert "content-disposition" not in response.headers
    assert harness.api.store.get_post(harness.private_id)["views"] == 0


@pytest.mark.parametrize("header,value", [
    ("X-User-Id", "owner-a"),
    ("X-Role", "admin"),
    ("X-Admin", "true"),
    ("X-Owner", "owner-a"),
])
def test_forged_identity_headers_never_grant_private_access(harness, header, value):
    response = harness.client.get(
        f"/api/community/posts/{harness.private_id}/pdf",
        headers=harness.headers(harness.other, extra={header: value}),
    )
    assert response.status_code == 404
    assert harness.spy.builds == 0


def test_query_user_id_never_switches_private_principal(harness):
    response = harness.client.get(
        f"/api/community/posts/{harness.private_id}/pdf?user_id=owner-a",
        headers=harness.headers(harness.other),
    )
    assert response.status_code == 404
    assert harness.spy.builds == 0


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_private_denials_are_never_shared_by_caches(harness, method):
    response = harness.client.request(
        method,
        f"/api/community/posts/{harness.private_id}/pdf",
        headers=harness.headers(harness.other),
    )
    assert response.status_code == 404
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Cookie"
    assert harness.spy.builds == 0


def test_session_token_in_query_string_is_never_authentication(harness):
    response = harness.client.get(
        f"/api/community/posts/{harness.private_id}/pdf"
        "?session_token=attacker-supplied-token"
    )
    assert response.status_code == 401
    assert harness.spy.builds == 0


def test_anonymous_forged_owner_header_remains_anonymous(harness):
    response = harness.client.get(
        f"/api/community/posts/{harness.private_id}/pdf",
        headers={"X-User-Id": "owner-a"},
    )
    assert response.status_code == 401


def test_owner_reads_private_pdf(harness):
    response = harness.client.get(
        f"/api/community/posts/{harness.private_id}/pdf",
        headers=harness.headers(harness.owner),
    )
    assert response.status_code == 200 and response.content == PRIVATE_BYTES
    assert harness.spy.builds == harness.spy.open_calls == 1
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Cookie"


def test_trusted_admin_reads_private_pdf(harness):
    response = harness.client.get(
        f"/api/community/posts/{harness.private_id}/pdf",
        headers=harness.headers(harness.admin),
    )
    assert response.status_code == 200 and response.content == PRIVATE_BYTES


def test_moderator_does_not_inherit_private_read_access(harness):
    response = harness.client.get(
        f"/api/community/posts/{harness.private_id}/pdf",
        headers=harness.headers(harness.moderator),
    )
    assert response.status_code == 404
    assert harness.spy.builds == 0


def test_scenario_private_owner_reads_other_404_anonymous_401(harness):
    """Business rule 1 — private/draft is restricted to owner (and trusted admin)."""
    url = f"/api/community/posts/{harness.private_id}/pdf"
    owner = harness.client.get(url, headers=harness.headers(harness.owner))
    assert owner.status_code == 200 and owner.content == PRIVATE_BYTES
    other = harness.client.get(url, headers=harness.headers(harness.other))
    assert other.status_code == 404
    admin = harness.client.get(url, headers=harness.headers(harness.admin))
    assert admin.status_code == 200
    anonymous = harness.client.get(url)
    assert anonymous.status_code == 401


def test_scenario_community_owner_and_other_read_anonymous_401(harness):
    """Business rule 2 — a community post is readable by any signed-in member."""
    url = f"/api/community/posts/{harness.public_id}/pdf"
    owner = harness.client.get(url, headers=harness.headers(harness.owner))
    assert owner.status_code == 200 and owner.content == PUBLIC_BYTES
    other = harness.client.get(url, headers=harness.headers(harness.other))
    assert other.status_code == 200 and other.content == PUBLIC_BYTES
    rng = harness.client.get(url, headers={**harness.headers(harness.other),
                                           "Range": "bytes=0-3"})
    assert rng.status_code == 206
    anonymous = harness.client.get(url)
    assert anonymous.status_code == 401


def test_non_owner_member_cannot_manage_community_post(harness):
    """Rule 2 — a reader is only a reader: no unpublish/edit/delete on another's post."""
    unpublish = harness.client.post(
        f"/api/community/posts/{harness.public_id}/unpublish",
        headers=harness.headers(harness.other, csrf=True),
    )
    assert unpublish.status_code == 404
    # The post is still published and readable by the owner afterwards.
    still = harness.client.get(
        f"/api/community/posts/{harness.public_id}/pdf",
        headers=harness.headers(harness.owner))
    assert still.status_code == 200
    assert harness.api.store.get_post(harness.public_id)["status"] == PostStatus.PUBLISHED


def test_anonymous_community_read_is_401(harness):
    # A community (public) post is authenticated-only: anonymous is asked to sign in and
    # never receives bytes, size, filename or storage id.
    response = harness.client.get(f"/api/community/posts/{harness.public_id}/pdf")
    assert response.status_code == 401
    assert response.content != PUBLIC_BYTES
    assert "content-length" not in {k.lower(): v for k, v in response.headers.items()} \
        or response.headers.get("content-length") != str(len(PUBLIC_BYTES))
    assert harness.spy.builds == harness.spy.open_calls == 0


def test_authenticated_non_owner_reads_community_pdf(harness):
    response = harness.client.get(
        f"/api/community/posts/{harness.public_id}/pdf",
        headers=harness.headers(harness.other),
    )
    assert response.status_code == 200
    assert response.content == PUBLIC_BYTES
    assert "storage_file_id" not in response.text


def test_authenticated_non_owner_head_has_consistent_size_and_no_body(harness):
    response = harness.client.head(
        f"/api/community/posts/{harness.public_id}/pdf",
        headers=harness.headers(harness.other),
    )
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == str(len(PUBLIC_BYTES))
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Cookie"
    assert harness.spy.builds == harness.spy.open_calls == 0
    assert harness.api.store.get_post(harness.public_id)["views"] == 0


def test_anonymous_community_head_is_401(harness):
    response = harness.client.head(f"/api/community/posts/{harness.public_id}/pdf")
    assert response.status_code == 401
    assert harness.spy.builds == harness.spy.open_calls == 0


@pytest.mark.parametrize("range_header,start,end", [
    ("bytes=0-3", 0, 3),
    ("bytes=4-11", 4, 11),
    (f"bytes={len(PUBLIC_BYTES)-4}-", len(PUBLIC_BYTES) - 4, len(PUBLIC_BYTES) - 1),
    ("bytes=-4", len(PUBLIC_BYTES) - 4, len(PUBLIC_BYTES) - 1),
])
def test_community_single_ranges_stream_exact_bytes(harness, range_header, start, end):
    response = harness.client.get(
        f"/api/community/posts/{harness.public_id}/pdf",
        headers={**harness.headers(harness.other), "Range": range_header},
    )
    assert response.status_code == 206
    assert response.content == PUBLIC_BYTES[start:end + 1]
    assert response.headers["content-range"] == f"bytes {start}-{end}/{len(PUBLIC_BYTES)}"


def test_anonymous_community_range_is_401_before_provider(harness):
    # Authorization precedes range parsing: anonymous is 401, never a 416 that would
    # confirm the size.
    response = harness.client.get(
        f"/api/community/posts/{harness.public_id}/pdf",
        headers={"Range": "bytes=0-3"},
    )
    assert response.status_code == 401
    assert harness.spy.builds == 0


@pytest.mark.parametrize("range_header", ["invalid", "bytes=-", "bytes=99999-", "bytes=0-1,4-5"])
def test_invalid_or_multiple_community_range_is_416_before_provider(harness, range_header):
    response = harness.client.get(
        f"/api/community/posts/{harness.public_id}/pdf",
        headers={**harness.headers(harness.other), "Range": range_header},
    )
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(PUBLIC_BYTES)}"
    assert harness.spy.builds == 0


def test_feed_only_contains_public_published_approved_verified(harness):
    harness.seed_post(owner="owner-a", visibility=Visibility.PUBLIC, verified=False,
                      title="Unverified")
    harness.seed_post(owner="owner-a", visibility=Visibility.PUBLIC,
                      status=PostStatus.UNPUBLISHED, title="Unpublished")
    harness.seed_post(owner="owner-a", visibility=Visibility.PUBLIC,
                      status=PostStatus.BLOCKED, title="Blocked")
    harness.seed_post(owner="owner-a", visibility=Visibility.PUBLIC,
                      status=PostStatus.FAILED, title="Failed")
    harness.seed_post(owner="owner-a", visibility=Visibility.PUBLIC,
                      status=PostStatus.DELETED, title="Deleted")
    harness.seed_post(owner="owner-a", visibility=Visibility.PUBLIC,
                      moderation=Moderation.PENDING, title="Pending moderation")
    harness.seed_post(owner="owner-a", visibility=Visibility.UNLISTED,
                      title="Unlisted")
    superseded = harness.seed_post(
        owner="owner-a", visibility=Visibility.PUBLIC, title="Pending latest version")
    harness.api.store.create_file(
        post_id=superseded,
        filename="new-pending.pdf",
        mime_type="application/pdf",
        size_bytes=10,
        sha256="new-pending",
        storage_provider="filesystem",
    )
    response = harness.client.get(
        "/api/community/posts", headers=harness.headers(harness.other))
    ids = {post["post_id"] for post in response.json()["posts"]}
    assert ids == {harness.public_id}
    assert harness.private_id not in ids
    assert harness.spy.builds == 0


def test_feed_requires_authentication(harness):
    anonymous = harness.client.get("/api/community/posts")
    assert anonymous.status_code == 401
    member = harness.client.get(
        "/api/community/posts", headers=harness.headers(harness.other))
    assert member.status_code == 200
    assert harness.public_id in {p["post_id"] for p in member.json()["posts"]}
    assert harness.private_id not in {p["post_id"] for p in member.json()["posts"]}


def test_deleted_latest_file_never_falls_back_to_older_verified_version(harness):
    session = harness.spy.backing.create_resumable_session(
        filename="new-version.pdf",
        mime_type="application/pdf",
        size=16,
        parent_id="offline",
    )
    uploaded = harness.spy.backing.upload_chunk(session, 0, b"%PDF-new-version")
    latest_id = harness.api.store.create_file(
        post_id=harness.public_id,
        filename="new-version.pdf",
        mime_type="application/pdf",
        size_bytes=16,
        sha256="new-version-sha",
        storage_provider="filesystem",
    )
    harness.api.store.update_file(
        latest_id,
        upload_status=FileStatus.DELETED,
        storage_file_id=uploaded.file_id,
    )
    feed = harness.client.get(
        "/api/community/posts", headers=harness.headers(harness.other))
    assert harness.public_id not in {item["post_id"] for item in feed.json()["posts"]}
    denied = harness.client.get(
        f"/api/community/posts/{harness.public_id}/pdf",
        headers=harness.headers(harness.other))
    assert denied.status_code == 404
    assert harness.spy.builds == harness.spy.open_calls == 0


def test_my_posts_uses_session_principal_and_ignores_query_user_id(harness):
    other_post = harness.seed_post(
        owner="user-b", visibility=Visibility.PRIVATE, status=PostStatus.DRAFT,
        verified=False, title="Other only")
    response = harness.client.get(
        "/api/community/my-posts?user_id=owner-a",
        headers=harness.headers(harness.other),
    )
    ids = {post["post_id"] for post in response.json()["posts"]}
    assert other_post in ids
    assert harness.private_id not in ids and harness.public_id not in ids
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("field,value", [
    ("user_id", "owner-a"),
    ("role", "admin"),
    ("actor_id", "owner-a"),
    ("admin", True),
])
def test_publish_rejects_client_controlled_identity_fields(harness, field, value):
    response = harness.client.post(
        "/api/community/publish",
        json={"slug": "missing", field: value},
        headers=harness.headers(harness.other, csrf=True),
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "client_identity_not_allowed"


def test_non_admin_cannot_adopt_unowned_slug_output(harness):
    output_dir = harness.output_root / "legacy-output"
    output_dir.mkdir(parents=True)
    (output_dir / "chapter.pdf").write_bytes(b"%PDF-legacy")
    (output_dir / "run_manifest.json").write_text(
        '{"pdf_filename":"chapter.pdf"}', encoding="utf-8")
    before = len(harness.api.store.list_user_posts("user-b"))
    response = harness.client.post(
        "/api/community/publish",
        json={"slug": "legacy-output"},
        headers=harness.headers(harness.other, csrf=True),
    )
    assert response.status_code == 404
    assert len(harness.api.store.list_user_posts("user-b")) == before


def test_admin_falls_back_to_legacy_slug_when_history_job_id_is_stale(harness):
    output_dir = harness.output_root / "legacy_admin"
    output_dir.mkdir(parents=True)
    pdf_path = output_dir / "chapter.pdf"
    pdf_path.write_bytes(b"%PDF-legacy-admin")
    (output_dir / "run_manifest.json").write_text(
        '{"pdf_filename":"chapter.pdf"}', encoding="utf-8")
    response = harness.client.post(
        "/api/community/publish",
        json={"slug": "legacy_admin", "source_job_id": "f" * 32},
        headers=harness.headers(harness.admin, csrf=True),
    )
    assert response.status_code == 200, response.text
    publish_job = harness.jobs.get_job(response.json()["job_id"])
    assert publish_job["configuration"]["local_pdf_path"] == str(pdf_path.resolve())


def test_non_admin_stale_history_job_id_does_not_fall_back_to_slug(harness):
    output_dir = harness.output_root / "legacy_no_fallback"
    output_dir.mkdir(parents=True)
    (output_dir / "chapter.pdf").write_bytes(b"%PDF-legacy-no-fallback")
    (output_dir / "run_manifest.json").write_text(
        '{"pdf_filename":"chapter.pdf"}', encoding="utf-8")
    response = harness.client.post(
        "/api/community/publish",
        json={"slug": "legacy_no_fallback", "source_job_id": "f" * 32},
        headers=harness.headers(harness.other, csrf=True),
    )
    assert response.status_code == 404
    assert harness.api.store.list_user_posts("user-b") == []


def test_owned_source_job_publishes_as_session_principal(harness):
    source_job_id, pdf_path = harness.create_finished_translation_job(owner="user-b")
    response = harness.client.post(
        "/api/community/publish",
        json={
            "source_job_id": source_job_id,
            "source_run_id": "forged-client-run",
            "series_slug": "owned",
            "episode_number": "1",
        },
        headers=harness.headers(harness.other, csrf=True),
    )
    assert response.status_code == 200
    post = harness.api.store.get_post(response.json()["post_id"])
    assert post["user_id"] == "user-b"
    source_job = harness.jobs.get_job(source_job_id)
    assert post["source_run_id"] == source_job["run_id"]
    publish_job = harness.jobs.get_job(response.json()["job_id"])
    assert publish_job["configuration"]["local_pdf_path"] == pdf_path


def test_duplicate_publish_requests_are_idempotent_per_owner_and_source_job(harness):
    source_job_id, _ = harness.create_finished_translation_job(
        owner="user-b",
        name="idempotent-source",
    )
    principal = RequestPrincipal(
        "user-b",
        True,
        auth_source="test",
        session_id="idempotent-session",
    )
    payload = {"source_job_id": source_job_id, "series_slug": "idempotent"}
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda _index: harness.api.publish(payload, principal=principal),
            range(2),
        ))
    assert results[0] == results[1]
    posts = [
        post for post in harness.api.store.list_user_posts("user-b")
        if post["source_job_id"] == source_job_id
    ]
    assert len(posts) == 1
    publish_jobs = [
        job for job in harness.jobs.list_jobs(limit=None)
        if (job.get("configuration") or {}).get("job_type") == "community_publish"
    ]
    assert len(publish_jobs) == 1
    assert publish_jobs[0]["id"] == results[0]["job_id"]


def test_identical_pdf_from_different_source_job_is_blocked_while_first_is_active(harness):
    data = b"%PDF-identical-cross-source"
    first_source, _ = harness.create_finished_translation_job(
        owner="user-b", name="same-pdf-one", data=data)
    second_source, _ = harness.create_finished_translation_job(
        owner="user-b", name="same-pdf-two", data=data)
    first = harness.client.post(
        "/api/community/publish",
        json={"source_job_id": first_source},
        headers=harness.headers(harness.other, csrf=True),
    )
    second = harness.client.post(
        "/api/community/publish",
        json={"source_job_id": second_source},
        headers=harness.headers(harness.other, csrf=True),
    )
    assert first.status_code == 200
    assert second.status_code == 400
    assert second.json() == {"detail": "duplicate_pdf_already_published"}
    assert not any(
        post["source_job_id"] == second_source
        for post in harness.api.store.list_user_posts("user-b")
    )
    publish_jobs = [
        job for job in harness.jobs.list_jobs(limit=None)
        if (job.get("configuration") or {}).get("job_type") == "community_publish"
    ]
    assert [job["id"] for job in publish_jobs] == [first.json()["job_id"]]


def test_same_source_with_conflicting_visibility_is_not_silently_idempotent(harness):
    source_job_id, _ = harness.create_finished_translation_job(
        owner="user-b",
        name="idempotency-conflict",
    )
    principal = RequestPrincipal("user-b", True, auth_source="test")
    first = harness.api.publish(
        {"source_job_id": source_job_id, "visibility": "public"},
        principal=principal,
    )
    with pytest.raises(CommunityError, match="source_publish_conflict"):
        harness.api.publish(
            {"source_job_id": source_job_id, "visibility": "private"},
            principal=principal,
        )
    assert harness.api.store.get_post(first["post_id"])["visibility"] == "public"
    assert len([
        job for job in harness.jobs.list_jobs(limit=None)
        if (job.get("configuration") or {}).get("job_type") == "community_publish"
    ]) == 1


@pytest.mark.parametrize("invalid_force", ["false", 1, [], {}])
def test_force_new_version_requires_json_boolean(harness, invalid_force):
    source_job_id, _ = harness.create_finished_translation_job(
        owner="user-b",
        name=f"invalid-force-{type(invalid_force).__name__}",
    )
    response = harness.client.post(
        "/api/community/publish",
        json={"source_job_id": source_job_id, "force_new_version": invalid_force},
        headers=harness.headers(harness.other, csrf=True),
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "invalid_force_new_version"}
    assert not any(
        post["source_job_id"] == source_job_id
        for post in harness.api.store.list_user_posts("user-b")
    )


def test_repeated_completed_publish_and_republish_reuse_verified_file(harness):
    source_job_id, _ = harness.create_finished_translation_job(
        owner="user-b",
        name="completed-idempotent",
    )
    principal = RequestPrincipal("user-b", True, auth_source="test")
    payload = {"source_job_id": source_job_id}
    first = harness.api.publish(payload, principal=principal)
    claimed = harness.jobs.claim_next_job("publisher", 1)
    assert claimed and claimed["id"] == first["job_id"]
    harness.jobs.transition(first["job_id"], JobStatus.STARTING, expected_worker="publisher")
    harness.jobs.transition(first["job_id"], JobStatus.RUNNING, expected_worker="publisher")
    harness.api.store.update_file(
        first["file_id"],
        upload_status=FileStatus.VERIFYING,
        storage_file_id="verified-existing",
    )
    assert harness.api.store.complete_publish_attempt(
        post_id=first["post_id"],
        file_id=first["file_id"],
        upload_job_id=first["job_id"],
        provider_checksum="checksum",
        actor_id="user-b",
        size=harness.api.store.get_file(first["file_id"])["size_bytes"],
    )
    harness.jobs.transition(
        first["job_id"], JobStatus.FINISHED, expected_worker="publisher", exit_code=0)

    repeated = harness.api.publish(payload, principal=principal)
    assert repeated == first
    harness.api.unpublish(first["post_id"], principal=principal)
    assert harness.api.store.get_post(first["post_id"])["status"] == PostStatus.UNPUBLISHED
    republished = harness.api.publish(payload, principal=principal)
    assert republished == first
    assert harness.api.store.get_post(first["post_id"])["status"] == PostStatus.PUBLISHED
    publish_jobs = [
        job for job in harness.jobs.list_jobs(limit=None)
        if (job.get("configuration") or {}).get("job_type") == "community_publish"
    ]
    assert [job["id"] for job in publish_jobs] == [first["job_id"]]


def test_failed_publish_retries_same_file_with_new_linked_job(harness):
    source_job_id, _ = harness.create_finished_translation_job(
        owner="user-b",
        name="failed-retry",
    )
    principal = RequestPrincipal("user-b", True, auth_source="test")
    payload = {"source_job_id": source_job_id}
    first = harness.api.publish(payload, principal=principal)
    claimed = harness.jobs.claim_next_job("publisher", 1)
    assert claimed and claimed["id"] == first["job_id"]
    harness.jobs.transition(first["job_id"], JobStatus.STARTING, expected_worker="publisher")
    harness.jobs.transition(first["job_id"], JobStatus.RUNNING, expected_worker="publisher")
    assert harness.api.store.fail_publish_attempt(
        post_id=first["post_id"],
        file_id=first["file_id"],
        upload_job_id=first["job_id"],
        actor_id="user-b",
        reason="offline-test",
    )
    harness.jobs.transition(
        first["job_id"], JobStatus.FAILED, expected_worker="publisher")

    retried = harness.api.publish(payload, principal=principal)
    assert retried["post_id"] == first["post_id"]
    assert retried["file_id"] == first["file_id"]
    assert retried["job_id"] != first["job_id"]
    file = harness.api.store.get_file(first["file_id"])
    assert file["upload_status"] == FileStatus.PENDING
    assert file["upload_job_id"] == retried["job_id"]
    assert harness.jobs.get_job(retried["job_id"])["status"] == JobStatus.QUEUED


def test_invalid_visibility_is_controlled_400_without_creating_post(harness):
    source_job_id, _ = harness.create_finished_translation_job(
        owner="user-b",
        name="invalid-visibility",
    )
    response = harness.client.post(
        "/api/community/publish",
        json={"source_job_id": source_job_id, "visibility": "everyone"},
        headers=harness.headers(harness.other, csrf=True),
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "invalid_visibility"}
    assert not any(
        post["source_job_id"] == source_job_id
        for post in harness.api.store.list_user_posts("user-b")
    )


def test_real_discovered_history_record_maps_to_owned_job_and_publishes(
    harness,
    monkeypatch,
):
    source_job_id, pdf_path = harness.create_finished_translation_job(
        owner="user-b",
        name="history-owned",
    )
    output_dir = Path(pdf_path).parent
    (output_dir / "timing_report.json").write_text(
        json.dumps({
            "ocr_engine": "rapidocr",
            "pdf_path": pdf_path,
            "quality_validation": {"passed": True},
        }),
        encoding="utf-8",
    )
    import ui_history
    monkeypatch.setattr(ui_history, "OUTPUT_ROOT", harness.output_root)
    records = UIHistoryStore(harness.output_root.parent / "history.json").discover_outputs()
    record = next(item for item in records if item["slug"] == "history-owned")
    assert record["id"] == "discovered-history-owned"
    assert record["job_id"] == source_job_id

    response = harness.client.post(
        "/api/community/publish",
        json={
            "slug": record["slug"],
            "source_job_id": record["job_id"],
            "series_title": record["series_name"],
            "series_slug": record["series_slug"],
        },
        headers=harness.headers(harness.other, csrf=True),
    )
    assert response.status_code == 200
    publish_job = harness.jobs.get_job(response.json()["job_id"])
    assert publish_job["configuration"]["local_pdf_path"] == pdf_path


def test_publish_uses_only_runner_recorded_pdf_when_directory_has_multiple(harness):
    source_job_id, pdf_path = harness.create_finished_translation_job(
        owner="user-b", name="multiple-pdfs", data=b"%PDF-recorded")
    (harness.output_root / "multiple-pdfs" / "000-decoy.pdf").write_bytes(
        b"%PDF-decoy")
    response = harness.client.post(
        "/api/community/publish",
        json={"source_job_id": source_job_id},
        headers=harness.headers(harness.other, csrf=True),
    )
    assert response.status_code == 200
    publish_job = harness.jobs.get_job(response.json()["job_id"])
    assert publish_job["configuration"]["local_pdf_path"] == pdf_path


def test_queued_owned_job_cannot_adopt_preexisting_pdf(harness):
    output_dir = harness.output_root / "queued-owned"
    output_dir.mkdir(parents=True)
    (output_dir / "chapter.pdf").write_bytes(b"%PDF-preexisting")
    source_job_id = harness.jobs.create_job(
        source_url="https://example.invalid/offline",
        output_dir=str(output_dir),
        command=["offline"],
        configuration={"job_type": "translation", "community_owner_id": "user-b"},
    )
    response = harness.client.post(
        "/api/community/publish",
        json={"source_job_id": source_job_id},
        headers=harness.headers(harness.other, csrf=True),
    )
    assert response.status_code == 404
    assert harness.api.store.list_user_posts("user-b") == []


def test_missing_and_other_users_job_are_indistinguishable(harness):
    source_job_id, _ = harness.create_finished_translation_job(
        owner="owner-a", name="other-owner-job")
    responses = [
        harness.client.post(
            "/api/community/publish",
            json={"source_job_id": candidate},
            headers=harness.headers(harness.other, csrf=True),
        )
        for candidate in (source_job_id, "0" * 32)
    ]
    assert [r.status_code for r in responses] == [404, 404]
    details = [r.json()["detail"] for r in responses]
    assert all(isinstance(detail, dict) for detail in details)
    assert all(detail["code"] == "output_not_found" for detail in details)
    assert all(detail["message"] != "not_found" for detail in details)
    assert harness.api.store.list_user_posts("user-b") == []


@pytest.mark.parametrize("failure", ["wrong_type", "missing_pdf", "manifest_mismatch"])
def test_ineligible_source_jobs_fail_closed_without_creating_posts(harness, failure):
    job_id, pdf_path = harness.create_finished_translation_job(
        owner="user-b",
        name=f"ineligible-{failure}",
        job_type="community_publish" if failure == "wrong_type" else "translation",
    )
    if failure == "missing_pdf":
        from pathlib import Path
        Path(pdf_path).unlink()
    elif failure == "manifest_mismatch":
        manifest_path = harness.output_root / f"ineligible-{failure}" / "job_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["job_id"] = "different-job"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    response = harness.client.post(
        "/api/community/publish",
        json={"source_job_id": job_id},
        headers=harness.headers(harness.other, csrf=True),
    )
    assert response.status_code == 404
    assert harness.api.store.list_user_posts("user-b") == []


def test_other_user_cannot_unpublish_owner_post(harness):
    response = harness.client.post(
        f"/api/community/posts/{harness.private_id}/unpublish",
        headers=harness.headers(harness.other, csrf=True),
    )
    assert response.status_code == 404
    assert harness.api.store.get_post(harness.private_id)["status"] == PostStatus.PUBLISHED


@pytest.mark.parametrize("terminal_status", [PostStatus.BLOCKED, PostStatus.DELETED])
def test_owner_cannot_revive_blocked_or_deleted_post_via_unpublish(
    harness,
    terminal_status,
):
    harness.api.store.set_post_status(
        harness.private_id,
        terminal_status,
        actor_id="moderator-a",
    )
    response = harness.client.post(
        f"/api/community/posts/{harness.private_id}/unpublish",
        headers=harness.headers(harness.owner, csrf=True),
    )
    assert response.status_code == 404
    assert harness.api.store.get_post(harness.private_id)["status"] == terminal_status


def test_other_user_cannot_obtain_metadata_edit_authority(harness):
    post = harness.api.store.get_post(harness.private_id)
    with pytest.raises(ResourceNotFound):
        authorize_manage_post(harness.other.principal, post)


def test_other_user_cannot_delete_owner_file_and_provider_is_untouched(harness):
    with pytest.raises(ResourceNotFound):
        harness.api.service.delete_remote_file(
            harness.private_id,
            provider=harness.spy,
            principal=harness.other.principal,
        )
    assert harness.spy.trash_calls == harness.spy.delete_calls == 0


@pytest.mark.parametrize("to_trash", [True, False])
def test_owner_can_explicitly_remove_own_file_with_audit(harness, to_trash):
    harness.api.service.delete_remote_file(
        harness.private_id,
        provider=harness.spy,
        principal=harness.owner.principal,
        to_trash=to_trash,
    )
    assert harness.spy.trash_calls == int(to_trash)
    assert harness.spy.delete_calls == int(not to_trash)
    event = harness.api.store.events_for_post(harness.private_id)[-1]
    assert event["actor_id"] == "owner-a" and event["event_type"] == "file_deleted"


def test_admin_audit_event_uses_real_principal_not_owner(harness):
    response = harness.client.post(
        f"/api/community/posts/{harness.private_id}/unpublish",
        headers=harness.headers(harness.admin, csrf=True),
    )
    assert response.status_code == 200
    event = harness.api.store.events_for_post(harness.private_id)[-1]
    assert event["actor_id"] == "admin-a"
    assert event["event_type"] == "status_unpublished"


@pytest.mark.parametrize("status", [
    PostStatus.DRAFT,
    PostStatus.PUBLISHING,
    PostStatus.UNPUBLISHED,
    PostStatus.BLOCKED,
    PostStatus.FAILED,
    PostStatus.DELETED,
])
def test_non_published_states_never_return_pdf_even_to_owner(harness, status):
    post_id = harness.seed_post(
        owner="owner-a", visibility=Visibility.PRIVATE, status=status, title=status)
    response = harness.client.get(
        f"/api/community/posts/{post_id}/pdf",
        headers=harness.headers(harness.owner),
    )
    assert response.status_code == 404


def test_unlisted_requires_owner_or_trusted_admin(harness):
    post_id = harness.seed_post(owner="owner-a", visibility=Visibility.UNLISTED)
    assert harness.client.get(f"/api/community/posts/{post_id}/pdf").status_code == 401
    assert harness.client.get(
        f"/api/community/posts/{post_id}/pdf",
        headers=harness.headers(harness.other),
    ).status_code == 404
    assert harness.client.get(
        f"/api/community/posts/{post_id}/pdf",
        headers=harness.headers(harness.owner),
    ).status_code == 200
    assert harness.client.get(
        f"/api/community/posts/{post_id}/pdf",
        headers=harness.headers(harness.admin),
    ).status_code == 200


def test_policy_is_fail_closed_for_unknown_visibility(harness):
    post = harness.api.store.get_post(harness.public_id)
    post["visibility"] = "future-unknown"
    assert not can_read_post(RequestPrincipal.anonymous(), post)
    assert not can_read_post(harness.owner.principal, post)


def test_admin_and_moderator_helpers_use_only_trusted_principal_roles(harness):
    authorize_admin_operation(harness.admin.principal)
    authorize_moderation(harness.admin.principal)
    authorize_moderation(harness.moderator.principal)
    with pytest.raises(AuthorizationDenied):
        authorize_admin_operation(harness.moderator.principal)
    with pytest.raises(AuthorizationDenied):
        authorize_moderation(harness.other.principal)


def test_logout_revokes_server_session_and_clears_cookies(harness):
    response = harness.client.post(
        "/api/community/auth/logout",
        headers=harness.headers(harness.owner, csrf=True),
    )
    assert response.status_code == 200
    assert len(response.headers.get_list("set-cookie")) == 2
    denied = harness.client.get(
        f"/api/community/posts/{harness.private_id}/pdf",
        headers=harness.headers(harness.owner),
    )
    assert denied.status_code == 401


def test_logout_clears_stale_cookie_after_server_session_loss(harness):
    stale = harness.auth.issue_session(user_id="stale-user")
    harness.auth.revoke_session(stale.principal.session_id)
    response = harness.client.post(
        "/api/community/auth/logout",
        headers=harness.headers(stale, csrf=True),
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    cookies = response.headers.get_list("set-cookie")
    assert any(
        value.startswith(f"{SESSION_COOKIE_NAME}=") and "Max-Age=0" in value
        for value in cookies
    )
    assert any(
        value.startswith(f"{CSRF_COOKIE_NAME}=") and "Max-Age=0" in value
        for value in cookies
    )
    assert response.headers["cache-control"] == "private, no-store"


def test_logout_rejects_stale_cookie_without_csrf_proof(harness):
    stale = harness.auth.issue_session(user_id="stale-user")
    harness.auth.revoke_session(stale.principal.session_id)
    response = harness.client.post(
        "/api/community/auth/logout",
        headers=harness.headers(stale),
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "csrf_rejected"}
    assert response.headers.get_list("set-cookie") == []


def test_anonymous_logout_without_csrf_is_rejected(harness):
    response = harness.client.post("/api/community/auth/logout")
    assert response.status_code == 403
    assert response.json() == {"detail": "csrf_rejected"}
