"""End-to-end authorization with real Supabase JWTs (offline keys, fake JWKS, fs storage).

Proves the existing PDF authorization boundary is unchanged when the principal comes from
a verified Supabase token instead of a local session: owner 200/206, other user 404,
anonymous 401, forged headers ignored, and denied requests never touch storage.
"""

import json
import socket
import time
import unittest
from pathlib import Path
import tempfile

import jwt as pyjwt
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm
from fastapi import FastAPI
from fastapi.testclient import TestClient

from community_api import CommunityApi
from community_http import CommunityNetworkBoundaryMiddleware, create_community_router
from community_storage import FilesystemStorageProvider
from community_store import FileStatus, Moderation, PostStatus, Visibility
from job_store import JobStore
from supabase_auth import SupabaseAuthConfig, SupabaseAuthProvider
from offline_test_guard import (
    _loopback_test_connect,
    _loopback_test_connect_ex,
    _loopback_test_create_connection,
)

ISSUER = "https://proj.supabase.co/auth/v1"
JWKS_URL = "https://proj.supabase.co/auth/v1/.well-known/jwks.json"
AUDIENCE = "authenticated"
CONFIG = SupabaseAuthConfig(url="https://proj.supabase.co", jwks_url=JWKS_URL,
                            issuer=ISSUER, audience=AUDIENCE, publishable_key="sb_publishable_x")
PUBLIC_BYTES = b"%PDF-1.4 public payload\n%%EOF\n"
PRIVATE_BYTES = b"%PDF-1.4 private payload bytes\n%%EOF\n"


def _make_key(kid="kid-1"):
    private = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(ECAlgorithm.to_jwk(private.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "ES256"})
    return private, jwk


class _Resp:
    def __init__(self, status, doc):
        self.status = status
        self.content = json.dumps(doc).encode()

    def json(self):
        return json.loads(self.content.decode())


class _FakeJwksTransport:
    def __init__(self, keys):
        self.doc = {"keys": keys}
        self.calls = 0

    def request(self, method, url, *, headers=None, data=None, stream=False):
        self.calls += 1
        return _Resp(200, self.doc)


class ProviderFactorySpy:
    def __init__(self, root):
        self.backing = FilesystemStorageProvider(root)
        self.factory_calls = 0
        self.open_calls = 0

    def factory(self):
        self.factory_calls += 1
        return self

    def open_stream(self, file_id, *, start=None, end=None):
        self.open_calls += 1
        return self.backing.open_stream(file_id, start=start, end=end)

    def reset(self):
        self.factory_calls = 0
        self.open_calls = 0


def _token(private, kid="kid-1", *, sub, exp_delta=3600):
    now = int(time.time())
    return pyjwt.encode(
        {"sub": sub, "iss": ISSUER, "aud": AUDIENCE, "exp": now + exp_delta, "iat": now},
        private, algorithm="ES256", headers={"kid": kid})


class SupabaseAuthorizationTests(unittest.TestCase):
    def setUp(self):
        # TestClient needs a Windows loopback self-pipe; non-loopback stays denied.
        self._orig_connect = socket.socket.connect
        self._orig_connect_ex = socket.socket.connect_ex
        self._orig_create = socket.create_connection
        socket.socket.connect = _loopback_test_connect
        socket.socket.connect_ex = _loopback_test_connect_ex
        socket.create_connection = _loopback_test_create_connection
        self.tmp = Path(tempfile.mkdtemp())
        self.private, self.jwk = _make_key()
        self.transport = _FakeJwksTransport([self.jwk])
        self.jobs = JobStore(self.tmp / "jobs.sqlite3")
        self.spy = ProviderFactorySpy(self.tmp / "storage")
        self.api = CommunityApi(
            self.jobs,
            community_db_path=self.tmp / "community.sqlite3",
            output_root=self.tmp / "output",
            read_provider_factory=self.spy.factory,
        )
        self.auth = SupabaseAuthProvider(CONFIG, transport=self.transport)
        app = FastAPI()
        app.add_middleware(CommunityNetworkBoundaryMiddleware, auth=self.auth)
        app.include_router(create_community_router(self.api, self.auth))
        self.client = TestClient(app, client=("127.0.0.1", 50000))
        self.public_id = self._seed("owner-sub", Visibility.PUBLIC, PUBLIC_BYTES, "Public post")
        self.private_id = self._seed("owner-sub", Visibility.PRIVATE, PRIVATE_BYTES, "Private title")
        self.private_file = self.api.store.file_for_post(self.private_id)
        self.spy.reset()
        self.owner_token = _token(self.private, sub="owner-sub")
        self.other_token = _token(self.private, sub="other-sub")

    def tearDown(self):
        self.client.close()
        self.api.close()
        self.jobs.close()
        socket.socket.connect = self._orig_connect
        socket.socket.connect_ex = self._orig_connect_ex
        socket.create_connection = self._orig_create

    def _seed(self, owner, visibility, data, title):
        post_id = self.api.store.create_post(user_id=owner, visibility=visibility,
                                             title=title, series_title=title)
        session = self.spy.backing.create_resumable_session(
            filename=f"{post_id}.pdf", mime_type="application/pdf",
            size=len(data), parent_id="offline")
        uploaded = self.spy.backing.upload_chunk(session, 0, data)
        file_id = self.api.store.create_file(
            post_id=post_id, filename=f"{post_id}-private-name.pdf",
            mime_type="application/pdf", size_bytes=len(data),
            sha256=f"sha-{post_id}", storage_provider="filesystem")
        self.api.store.update_file(file_id, upload_status=FileStatus.VERIFIED,
                                   storage_file_id=uploaded.file_id)
        self.api.store.set_post_status(post_id, PostStatus.PUBLISHED,
                                       moderation_status=Moderation.APPROVED)
        return post_id

    def _bearer(self, token):
        return {"Authorization": f"Bearer {token}"}

    def _pdf_url(self, post_id):
        return f"/api/community/posts/{post_id}/pdf"

    def _assert_no_private_leak(self, response):
        headers = {k.lower(): v for k, v in response.headers.items()}
        body = response.text or ""
        self.assertNotEqual(headers.get("content-length"), str(len(PRIVATE_BYTES)))
        self.assertNotIn("content-range", headers)
        self.assertNotIn("content-disposition", headers)
        self.assertNotIn(self.private_file["storage_file_id"], body)
        self.assertNotIn("Private title", body)
        self.assertNotIn("private-name", body)

    # ---- owner allowed ----
    def test_owner_head_get_range(self):
        url = self._pdf_url(self.private_id)
        head = self.client.head(url, headers=self._bearer(self.owner_token))
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.headers["Content-Length"], str(len(PRIVATE_BYTES)))
        self.assertEqual(head.headers["X-Content-Type-Options"], "nosniff")
        get = self.client.get(url, headers=self._bearer(self.owner_token))
        self.assertEqual(get.status_code, 200)
        self.assertEqual(get.content, PRIVATE_BYTES)
        rng = self.client.get(url, headers={**self._bearer(self.owner_token),
                                            "Range": "bytes=0-4"})
        self.assertEqual(rng.status_code, 206)
        self.assertEqual(rng.headers["Content-Range"], f"bytes 0-4/{len(PRIVATE_BYTES)}")

    def test_owner_my_posts_contains_private(self):
        r = self.client.get("/api/community/my-posts", headers=self._bearer(self.owner_token))
        self.assertEqual(r.status_code, 200)
        ids = [p.get("post_id") for p in r.json()["posts"]]
        self.assertIn(self.private_id, ids)

    def test_public_readable_by_anyone(self):
        r = self.client.get(self._pdf_url(self.public_id))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, PUBLIC_BYTES)

    # ---- other user denied uniformly ----
    def test_other_user_gets_404_and_no_storage(self):
        url = self._pdf_url(self.private_id)
        for call in (
            lambda: self.client.head(url, headers=self._bearer(self.other_token)),
            lambda: self.client.get(url, headers=self._bearer(self.other_token)),
            lambda: self.client.get(url, headers={**self._bearer(self.other_token),
                                                  "Range": "bytes=0-4"}),
        ):
            r = call()
            self.assertEqual(r.status_code, 404)
            self._assert_no_private_leak(r)
        self.assertEqual(self.spy.factory_calls, 0)
        self.assertEqual(self.spy.open_calls, 0)

    def test_other_user_my_posts_excludes_private(self):
        r = self.client.get("/api/community/my-posts", headers=self._bearer(self.other_token))
        ids = [p.get("post_id") for p in r.json()["posts"]]
        self.assertNotIn(self.private_id, ids)

    # ---- anonymous denied ----
    def test_anonymous_gets_401_and_no_storage(self):
        url = self._pdf_url(self.private_id)
        for call in (
            lambda: self.client.head(url),
            lambda: self.client.get(url),
            lambda: self.client.get(url, headers={"Range": "bytes=0-4"}),
        ):
            r = call()
            self.assertEqual(r.status_code, 401)
            self._assert_no_private_leak(r)
        self.assertEqual(self.spy.factory_calls, 0)
        self.assertEqual(self.spy.open_calls, 0)

    # ---- forged identity ignored ----
    def test_forged_headers_do_not_grant_access(self):
        url = self._pdf_url(self.private_id)
        for headers in (
            {"X-User-Id": "owner-sub"},
            {"X-Role": "admin"},
            {"X-Admin": "true"},
            {"X-Owner": "true"},
        ):
            r = self.client.get(url, headers=headers)
            self.assertEqual(r.status_code, 401)
        r = self.client.get(url, params={"user_id": "owner-sub"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.spy.open_calls, 0)

    def test_invalid_bearer_is_401_not_owner(self):
        url = self._pdf_url(self.private_id)
        r = self.client.get(url, headers={"Authorization": "Bearer not.a.jwt"})
        self.assertEqual(r.status_code, 401)

    def test_feed_hides_private(self):
        r = self.client.get("/api/community/posts")
        titles = [p.get("title") for p in r.json()["posts"]]
        self.assertNotIn("Private title", titles)
        self.assertEqual(self.spy.open_calls, 0)


if __name__ == "__main__":
    unittest.main()
