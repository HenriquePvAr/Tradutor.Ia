"""Unit tests for the Supabase social layer — fake HTTP transport, zero real network.

Covers header/JWT forwarding, no secret usage, error mapping, ownership fields derived
from the principal, client identity fields rejected, DTO shaping, pagination/cursor, and
fail-closed provider selection.
"""

import _test_bootstrap  # noqa: F401

import json
import socket
import unittest
from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.testclient import TestClient

from community_auth import AuthenticationRequired, RequestPrincipal
from supabase_social import (
    SocialAuthRequired, SocialConfig, SocialConfigError, SocialConflict, SocialNotFound,
    SocialRateLimited, SocialUnavailable, SocialValidationError,
)
from social_repository import (
    SupabaseSocialRepository, build_social_repository, decode_cursor, encode_cursor,
)
from social_http import create_social_router
from offline_test_guard import (
    _loopback_test_connect, _loopback_test_connect_ex, _loopback_test_create_connection,
)

CONFIG = SocialConfig(url="https://proj.supabase.co", publishable_key="sb_publishable_pub")
TOKEN = "aaa.bbb.ccc"
USER = "11111111-1111-1111-1111-111111111111"
UUID2 = "22222222-2222-2222-2222-222222222222"


@dataclass
class _Resp:
    status: int
    content: bytes
    headers: dict


class FakeTransport:
    """Records each request and replays a programmed queue of responses."""

    def __init__(self):
        self.calls = []
        self.queue = []

    def push(self, status, body):
        payload = json.dumps(body).encode() if body is not None else b""
        self.queue.append(_Resp(status, payload, {}))

    def request(self, method, url, *, headers=None, data=None, stream=False):
        self.calls.append({"method": method, "url": url, "headers": headers or {},
                           "data": json.loads(data) if data else None})
        if not self.queue:
            return _Resp(200, b"[]", {})
        return self.queue.pop(0)


def _repo(transport):
    return SupabaseSocialRepository(CONFIG, transport=transport)


class HeaderAndAuthTests(unittest.TestCase):
    def test_headers_carry_publishable_key_and_user_bearer_never_secret(self):
        t = FakeTransport(); t.push(200, [{"id": USER, "username": "me"}])
        _repo(t).get_my_profile(TOKEN, USER)
        h = t.calls[0]["headers"]
        self.assertEqual(h["apikey"], "sb_publishable_pub")
        self.assertEqual(h["Authorization"], f"Bearer {TOKEN}")
        blob = json.dumps(h)
        self.assertNotIn("sb_secret", blob)
        self.assertNotIn("service_role", blob)

    def test_config_repr_redacts(self):
        self.assertNotIn("sb_publishable_pub", repr(CONFIG))
        self.assertNotIn("proj.supabase", repr(CONFIG))

    def test_missing_token_is_auth_required(self):
        from supabase_social import SupabaseDataClient
        with self.assertRaises(SocialAuthRequired):
            SupabaseDataClient(CONFIG, "", transport=FakeTransport())

    def test_url_is_built_from_trusted_base(self):
        t = FakeTransport(); t.push(200, [])
        _repo(t).feed(TOKEN)
        self.assertTrue(t.calls[0]["url"].startswith("https://proj.supabase.co/rest/v1/works"))


class ErrorMappingTests(unittest.TestCase):
    def _run_status(self, status, body=None):
        t = FakeTransport(); t.push(status, body)
        return _repo(t)

    def test_401_maps_auth(self):
        with self.assertRaises(SocialAuthRequired):
            self._run_status(401).feed(TOKEN)

    def test_403_rls_denial_maps_not_found(self):
        # An RLS denial must not reveal the row's existence.
        with self.assertRaises(SocialNotFound):
            self._run_status(403).feed(TOKEN)

    def test_404_maps_not_found(self):
        with self.assertRaises(SocialNotFound):
            self._run_status(404).get_work(TOKEN, USER)

    def test_409_maps_conflict(self):
        t = FakeTransport(); t.push(409, {"code": "23505"})
        with self.assertRaises(SocialConflict):
            SupabaseSocialRepository(CONFIG, transport=t).create_work(
                TOKEN, USER, {"title": "T", "slug": "s"})

    def test_23514_check_maps_validation(self):
        t = FakeTransport(); t.push(400, {"code": "23514"})
        with self.assertRaises(SocialValidationError):
            SupabaseSocialRepository(CONFIG, transport=t).create_work(
                TOKEN, USER, {"title": "T", "slug": "s"})

    def test_429_maps_rate_limited(self):
        with self.assertRaises(SocialRateLimited):
            self._run_status(429).feed(TOKEN)

    def test_503_maps_unavailable(self):
        with self.assertRaises(SocialUnavailable):
            self._run_status(503).feed(TOKEN)

    def test_transport_exception_maps_unavailable(self):
        class Boom:
            def request(self, *a, **k):
                raise OSError("down")
        with self.assertRaises(SocialUnavailable):
            _repo(Boom()).feed(TOKEN)

    def test_oversized_response_is_unavailable(self):
        class Big:
            def request(self, *a, **k):
                return _Resp(200, b"[" + b"0" * (5 * 1024 * 1024) + b"]", {})
        with self.assertRaises(SocialUnavailable):
            _repo(Big()).feed(TOKEN)


class OwnershipAndBodyTests(unittest.TestCase):
    def test_create_work_sets_owner_from_principal(self):
        t = FakeTransport(); t.push(201, [{"id": UUID2, "owner_id": USER}])
        _repo(t).create_work(TOKEN, USER, {"title": "T", "slug": "s"})
        self.assertEqual(t.calls[0]["data"]["owner_id"], USER)

    def test_create_comment_sets_author_from_principal(self):
        t = FakeTransport(); t.push(201, [{"id": UUID2, "author_id": USER, "deleted_at": None}])
        _repo(t).create_comment(TOKEN, USER, UUID2, "hello", None)
        self.assertEqual(t.calls[0]["data"]["author_id"], USER)

    def test_create_report_sets_reporter_and_open_status(self):
        t = FakeTransport(); t.push(201, [{"id": UUID2, "reporter_id": USER, "status": "open"}])
        _repo(t).create_report(TOKEN, USER, {"target_type": "work", "target_id": UUID2, "reason": "x"})
        self.assertEqual(t.calls[0]["data"]["reporter_id"], USER)
        self.assertEqual(t.calls[0]["data"]["status"], "open")

    def test_soft_delete_work_patches_deleted_at(self):
        t = FakeTransport(); t.push(200, [{"id": UUID2}])
        _repo(t).soft_delete_work(TOKEN, UUID2)
        self.assertEqual(t.calls[0]["method"], "PATCH")
        self.assertIn("deleted_at", t.calls[0]["data"])

    def test_update_work_rejects_unknown_fields(self):
        with self.assertRaises(SocialValidationError):
            _repo(FakeTransport()).update_work(TOKEN, UUID2, {"owner_id": "x"})

    def test_deleted_comment_text_is_hidden(self):
        t = FakeTransport()
        t.push(200, [{"id": UUID2, "content": "secret", "deleted_at": "2020-01-01T00:00:00Z"}])
        out = _repo(t).list_comments(TOKEN, UUID2)
        self.assertEqual(out["items"][0]["content"], "")
        self.assertTrue(out["items"][0]["is_deleted"])

    def test_invalid_uuid_is_not_found(self):
        with self.assertRaises(SocialNotFound):
            _repo(FakeTransport()).get_work(TOKEN, "not-a-uuid")

    def test_progress_out_of_range_rejected(self):
        with self.assertRaises(SocialValidationError):
            _repo(FakeTransport()).upsert_history(TOKEN, USER, UUID2, {"progress_value": 150})


class PaginationTests(unittest.TestCase):
    def test_next_cursor_present_when_more(self):
        t = FakeTransport()
        rows = [{"id": f"{i:08d}-1111-1111-1111-111111111111",
                 "created_at": f"2026-01-0{i}T00:00:00Z"} for i in range(1, 3)]  # limit+1 = 2 for limit 1
        t.push(200, rows)
        out = _repo(t).feed(TOKEN, limit=1)
        self.assertEqual(len(out["items"]), 1)
        self.assertIsNotNone(out["next_cursor"])

    def test_cursor_round_trip(self):
        c = encode_cursor("2026-01-01T00:00:00Z", USER)
        ts, rid = decode_cursor(c)
        self.assertEqual((ts, rid), ("2026-01-01T00:00:00Z", USER))

    def test_invalid_cursor_rejected(self):
        with self.assertRaises(SocialValidationError):
            _repo(FakeTransport()).feed(TOKEN, cursor="!!!notbase64!!!")

    def test_limit_clamped(self):
        t = FakeTransport(); t.push(200, [])
        _repo(t).feed(TOKEN, limit=9999)
        self.assertIn("limit=101", t.calls[0]["url"])  # MAX_PAGE_LIMIT(100)+1


class ProviderSelectionTests(unittest.TestCase):
    def test_unknown_provider_fails_closed(self):
        with self.assertRaises(SocialConfigError):
            build_social_repository({"COMMUNITY_SOCIAL_PROVIDER": "s3"})

    def test_local_provider_fails_closed(self):
        with self.assertRaises(SocialConfigError):
            build_social_repository({"COMMUNITY_SOCIAL_PROVIDER": "local"})

    def test_supabase_without_config_fails_closed(self):
        with self.assertRaises(SocialConfigError):
            build_social_repository({"COMMUNITY_SOCIAL_PROVIDER": "supabase"})

    def test_no_network_at_construction(self):
        # Building the repo must not touch the network; only a method call would.
        called = {"n": 0}

        class Spy:
            def request(self, *a, **k):
                called["n"] += 1
                return _Resp(200, b"[]", {})
        build_social_repository(
            {"COMMUNITY_SOCIAL_PROVIDER": "supabase", "SUPABASE_URL": "https://x.supabase.co",
             "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_x"}, transport=Spy())
        self.assertEqual(called["n"], 0)


# ---------------------------------------------------------------------------
# Router-level tests: identity fields rejected, JWT forwarded, anon denied.
# ---------------------------------------------------------------------------
class FakeAuth:
    auth_source = "supabase"

    def require_authenticated(self, request):
        raw = str(request.headers.get("authorization", ""))
        if not raw.lower().startswith("bearer ") or not raw[7:].strip():
            raise AuthenticationRequired("authentication_required")
        return RequestPrincipal(USER, True, auth_source="supabase", session_id=None)


class RecordingRepo:
    def __init__(self):
        self.last = {}

    def get_my_profile(self, token, user_id):
        self.last = {"token": token, "user_id": user_id}
        return {"id": user_id, "username": "me"}

    def create_work(self, token, user_id, fields):
        self.last = {"token": token, "user_id": user_id, "fields": fields}
        return {"id": UUID2, "owner_id": user_id, **fields}

    def create_report(self, token, user_id, body):
        self.last = {"token": token, "user_id": user_id, "body": body}
        return {"id": UUID2, "reporter_id": user_id, "status": "open"}


class RouterTests(unittest.TestCase):
    def setUp(self):
        self._c = socket.socket.connect
        self._ce = socket.socket.connect_ex
        self._cc = socket.create_connection
        socket.socket.connect = _loopback_test_connect
        socket.socket.connect_ex = _loopback_test_connect_ex
        socket.create_connection = _loopback_test_create_connection
        self.repo = RecordingRepo()
        app = FastAPI()
        app.include_router(create_social_router(self.repo, FakeAuth()))
        self.client = TestClient(app, client=("127.0.0.1", 50000))

    def tearDown(self):
        self.client.close()
        socket.socket.connect = self._c
        socket.socket.connect_ex = self._ce
        socket.create_connection = self._cc

    def test_anonymous_gets_401(self):
        self.assertEqual(self.client.get("/api/community/social/profile/me").status_code, 401)

    def test_authenticated_forwards_same_bearer_and_derives_user(self):
        r = self.client.get("/api/community/social/profile/me",
                            headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.repo.last["token"], TOKEN)
        self.assertEqual(self.repo.last["user_id"], USER)

    def test_client_owner_id_is_rejected(self):
        r = self.client.post("/api/community/social/works",
                             headers={"Authorization": f"Bearer {TOKEN}"},
                             json={"title": "T", "slug": "s", "owner_id": UUID2})
        self.assertEqual(r.status_code, 422)

    def test_client_role_is_rejected(self):
        r = self.client.post("/api/community/social/works",
                             headers={"Authorization": f"Bearer {TOKEN}"},
                             json={"title": "T", "slug": "s", "role": "admin"})
        self.assertEqual(r.status_code, 422)

    def test_report_status_is_rejected(self):
        r = self.client.post("/api/community/social/reports",
                             headers={"Authorization": f"Bearer {TOKEN}"},
                             json={"target_type": "work", "target_id": UUID2,
                                   "reason": "x", "status": "resolved"})
        self.assertEqual(r.status_code, 422)

    def test_work_created_uses_principal_owner(self):
        r = self.client.post("/api/community/social/works",
                             headers={"Authorization": f"Bearer {TOKEN}"},
                             json={"title": "T", "slug": "s"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.repo.last["user_id"], USER)
        self.assertEqual(r.headers["cache-control"], "private, no-store")


if __name__ == "__main__":
    unittest.main()
