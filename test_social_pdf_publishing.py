"""Explicit publishing orchestration + endpoints — offline, no Drive, no remote Supabase."""

import _test_bootstrap  # noqa: F401

import socket
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from community_auth import AuthenticationRequired, RequestPrincipal
from community_store import FileStatus, PostStatus
from chapter_asset_repository import ChapterAssetRepository
from social_pdf_publishing import SocialPdfPublishingService
from social_pdf_http import create_social_pdf_router
from supabase_social import SocialNotFound, SocialValidationError
from offline_test_guard import (
    _loopback_test_connect, _loopback_test_connect_ex, _loopback_test_create_connection,
)

OWNER = RequestPrincipal("owner-A", True, auth_source="supabase", session_id=None)
OTHER = RequestPrincipal("user-B", True, auth_source="supabase", session_id=None)
JOB = "a" * 32  # 32-hex opaque job id


class FakeCommunityStore:
    def __init__(self):
        self.posts = {}
        self.files = {}

    def get_post(self, pid):
        return self.posts.get(pid)

    def file_for_post(self, pid):
        return self.files.get(pid)

    def verify(self, pid, sfid="drive-xyz", size=1000):
        self.posts[pid] = {"user_id": "owner-A", "status": PostStatus.PUBLISHED}
        self.files[pid] = {"upload_status": FileStatus.VERIFIED, "storage_file_id": sfid, "size_bytes": size}


class FakeCommunityApi:
    def __init__(self):
        self.store = FakeCommunityStore()
        self.publish_calls = 0

    def publish(self, payload, *, principal):
        self.publish_calls += 1
        # A fresh (pending) publication; the test 'verifies' it later.
        pid = f"pub-{self.publish_calls}"
        self.store.posts[pid] = {"user_id": principal.user_id, "status": PostStatus.PUBLISHING}
        self.store.files[pid] = {"upload_status": FileStatus.UPLOADING, "storage_file_id": "", "size_bytes": 0}
        return {"post_id": pid, "file_id": "f", "job_id": "j"}


class FakeSocial:
    def __init__(self, owner="owner-A"):
        self.owner = owner
        self.updates = []

    def get_chapter(self, token, chapter_id):
        if chapter_id == "ghost":
            raise SocialNotFound()
        return {"id": chapter_id, "work_id": "workA"}

    def get_work(self, token, work_id):
        return {"id": work_id, "owner_id": self.owner}

    def update_chapter(self, token, chapter_id, fields):
        self.updates.append((chapter_id, fields))
        return {"id": chapter_id, **fields}


class FakeJobs:
    def __init__(self, jobs):
        self._jobs = jobs

    def list_jobs(self, *, statuses=None, limit=200):
        return self._jobs


class PublishingServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.capi = FakeCommunityApi()
        self.assets = ChapterAssetRepository(self.tmp / "a.sqlite3", community_store=self.capi.store)
        self.social = FakeSocial(owner="owner-A")
        self.jobs = FakeJobs([
            {"id": JOB, "status": "finished", "exit_code": 0,
             "configuration": {"job_type": "translation", "community_owner_id": "owner-A",
                               "chapter_name": "Cap 1"}, "created_at": 1.0, "series_title": "W"},
            {"id": "b" * 32, "status": "running", "exit_code": None,
             "configuration": {"job_type": "translation", "community_owner_id": "owner-A"}},
            {"id": "c" * 32, "status": "finished", "exit_code": 0,
             "configuration": {"job_type": "translation", "community_owner_id": "someone-else"}},
        ])
        self.svc = SocialPdfPublishingService(self.capi, self.assets, self.social, self.jobs)

    def tearDown(self):
        self.assets.close()

    def test_list_only_owned_finished_results_no_path(self):
        out = self.svc.list_local_results(OWNER)
        ids = [i["source_job_id"] for i in out["items"]]
        self.assertEqual(ids, [JOB])  # not the running one, not someone-else's
        for item in out["items"]:
            for bad in ("path", "output_dir", "storage_file_id", "drive"):
                self.assertNotIn(bad, item)

    def test_nothing_publishes_without_explicit_call(self):
        # Merely listing / constructing the service uploads nothing.
        self.assertEqual(self.capi.publish_calls, 0)

    def test_publish_requires_valid_target_status(self):
        with self.assertRaises(SocialValidationError):
            self.svc.publish_pdf("owner-A", OWNER, "chapA", JOB, "public")

    def test_publish_rejects_bad_job_id_shape(self):
        with self.assertRaises(SocialNotFound):
            self.svc.publish_pdf("owner-A", OWNER, "chapA", "not-a-job", "private")

    def test_other_user_cannot_publish(self):
        self.social.owner = "owner-A"
        with self.assertRaises(SocialNotFound):
            self.svc.publish_pdf("user-B", OTHER, "chapA", JOB, "private")
        self.assertEqual(self.capi.publish_calls, 0)

    def test_publish_then_status_links_and_sets_status_only_after_verified(self):
        r = self.svc.publish_pdf("owner-A", OWNER, "chapA", JOB, "community")
        self.assertEqual(r["status"], "pending")
        self.assertEqual(self.capi.publish_calls, 1)
        # Not yet verified → still pending, chapter status unchanged, no asset.
        self.assertEqual(self.svc.publish_status("owner-A", OWNER, "chapA")["status"], "pending")
        self.assertEqual(self.social.updates, [])
        # Verify the upload; now status flips to community and the asset links.
        pid = [k for k in self.capi.store.posts if k.startswith("pub-")][0]
        self.capi.store.verify(pid)
        done = self.svc.publish_status("owner-A", OWNER, "chapA")
        self.assertEqual(done["status"], "published")
        self.assertEqual(done["target_status"], "community")
        self.assertEqual(self.social.updates, [("chapA", {"status": "community"})])
        self.assertEqual(self.assets.get_asset_for_read("chapA"), "drive-xyz")

    def test_double_publish_is_idempotent_single_upload(self):
        self.svc.publish_pdf("owner-A", OWNER, "chapA", JOB, "private")
        self.svc.publish_pdf("owner-A", OWNER, "chapA", JOB, "private")  # duplicate click
        self.assertEqual(self.capi.publish_calls, 1)

    def test_failed_upload_keeps_chapter_invisible(self):
        self.svc.publish_pdf("owner-A", OWNER, "chapA", JOB, "community")
        pid = [k for k in self.capi.store.posts if k.startswith("pub-")][0]
        self.capi.store.posts[pid]["status"] = PostStatus.FAILED
        self.capi.store.files[pid]["upload_status"] = FileStatus.FAILED
        self.assertEqual(self.svc.publish_status("owner-A", OWNER, "chapA")["status"], "failed")
        self.assertEqual(self.social.updates, [])  # never became community

    def test_unlink_removes_link_only(self):
        self.svc.publish_pdf("owner-A", OWNER, "chapA", JOB, "private")
        pid = [k for k in self.capi.store.posts if k.startswith("pub-")][0]
        self.capi.store.verify(pid)
        self.svc.publish_status("owner-A", OWNER, "chapA")
        self.svc.unlink_asset("owner-A", OWNER, "chapA")
        from chapter_asset_repository import AssetNotFound
        with self.assertRaises(AssetNotFound):
            self.assets.get_asset_for_read("chapA")

    def test_asset_status_owner_vs_reader(self):
        self.svc.publish_pdf("owner-A", OWNER, "chapA", JOB, "community")
        pid = [k for k in self.capi.store.posts if k.startswith("pub-")][0]
        self.capi.store.verify(pid)
        self.svc.publish_status("owner-A", OWNER, "chapA")
        owner_view = self.svc.get_asset("owner-A", OWNER, "chapA")
        self.assertTrue(owner_view["available"])
        self.assertIn("mime_type", owner_view)
        reader_view = self.svc.get_asset("user-B", OTHER, "chapA")
        self.assertEqual(set(reader_view), {"linked", "available"})


class _StubAuth:
    def require_authenticated(self, request):
        raw = str(request.headers.get("authorization", ""))
        if not raw.lower().startswith("bearer ") or not raw[7:].strip():
            raise AuthenticationRequired("authentication_required")
        return OWNER


class _StubPublishing:
    def __init__(self):
        self.last = None

    def publish_pdf(self, t, p, chapter_id, job, status, *, idempotency_key=""):
        self.last = {"job": job, "status": status}
        return {"status": "pending"}

    def list_local_results(self, p):
        return {"items": []}

    def require_owner(self, t, p, chapter_id):
        if chapter_id == "not-mine":
            raise SocialNotFound()
        return {"id": chapter_id}

    def get_asset(self, t, p, chapter_id):
        return {"linked": True, "available": True}


class _Stream:
    def __init__(self, n): self.start, self.end, self.content_length = 0, n - 1, n
    def iter_chunks(self): yield b"%PDF-" + b"x" * (self.content_length - 5)


class _StubContent:
    def head_content(self, t, p, cid):
        return {"mime_type": "application/pdf", "total_size": 500, "filename": "capitulo.pdf"}

    def open_content(self, t, p, cid, *, range_header=""):
        partial = bool(range_header)
        s = _Stream(100 if partial else 500)
        return ({"total_size": 500, "start": 0, "end": s.end, "content_length": s.content_length,
                 "partial": partial, "mime_type": "application/pdf", "filename": "capitulo.pdf"}, s)


class _StubRetention:
    def __init__(self):
        self.restored = []

    def list_owner_retained_assets(self, owner_id, **kw):
        return {"items": [{"chapter_id": "c1", "state": "retained", "reason": "replaced",
                           "retained_at": 1.0, "retain_until": 2.0, "days_remaining": 7,
                           "restorable": True}]}

    def get_retention_status(self, chapter_id, owner_id):
        return {"state": "retained", "restorable": True, "days_remaining": 7}

    def restore_asset(self, chapter_id, owner_id):
        self.restored.append((chapter_id, owner_id))
        return {"restored": True, "chapter_id": chapter_id}


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self._orig = (socket.socket.connect, socket.socket.connect_ex, socket.create_connection)
        socket.socket.connect = _loopback_test_connect
        socket.socket.connect_ex = _loopback_test_connect_ex
        socket.create_connection = _loopback_test_create_connection
        self.pub = _StubPublishing()
        app = FastAPI()
        self.retention = _StubRetention()
        app.include_router(create_social_pdf_router(self.pub, _StubContent(), _StubAuth(),
                                                    retention=self.retention))
        self.client = TestClient(app, client=("127.0.0.1", 50000))

    def tearDown(self):
        self.client.close()
        socket.socket.connect, socket.socket.connect_ex, socket.create_connection = self._orig

    def h(self):
        return {"Authorization": "Bearer tok"}

    def test_anonymous_denied(self):
        self.assertEqual(self.client.post("/api/community/social/chapters/c1/publish-pdf",
                                          json={"source_job_id": JOB, "target_status": "private"}).status_code, 401)
        self.assertEqual(self.client.get("/api/community/social/chapters/c1/content").status_code, 401)

    def test_publish_forbidden_fields_rejected(self):
        for bad in ({"path": "C:/x.pdf"}, {"owner_id": "x"}, {"storage_file_id": "y"}, {"role": "admin"}):
            body = {"source_job_id": JOB, "target_status": "private", **bad}
            r = self.client.post("/api/community/social/chapters/c1/publish-pdf", headers=self.h(), json=body)
            self.assertEqual(r.status_code, 422, bad)

    def test_publish_forwards_only_allowed_fields(self):
        r = self.client.post("/api/community/social/chapters/c1/publish-pdf", headers=self.h(),
                             json={"source_job_id": JOB, "target_status": "community"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.pub.last, {"job": JOB, "status": "community"})

    def test_content_head_and_get_and_range(self):
        head = self.client.head("/api/community/social/chapters/c1/content", headers=self.h())
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.headers["Content-Length"], "500")
        self.assertEqual(head.headers["Accept-Ranges"], "bytes")
        self.assertEqual(head.content, b"")
        get = self.client.get("/api/community/social/chapters/c1/content", headers=self.h())
        self.assertEqual(get.status_code, 200)
        self.assertEqual(get.headers["Content-Type"], "application/pdf")
        rng = self.client.get("/api/community/social/chapters/c1/content",
                              headers={**self.h(), "Range": "bytes=0-99"})
        self.assertEqual(rng.status_code, 206)
        self.assertIn("Content-Range", rng.headers)

    def test_content_never_leaks_drive_or_path(self):
        get = self.client.get("/api/community/social/chapters/c1/content", headers=self.h())
        blob = " ".join(f"{k}:{v}" for k, v in get.headers.items()).lower()
        for bad in ("drive", "storage_file_id", "webcontentlink", "googleapis", "x-google"):
            self.assertNotIn(bad, blob)


    def test_retention_endpoints_require_auth(self):
        for method, path in (("get", "/api/community/social/retained-assets"),
                             ("get", "/api/community/social/chapters/c1/asset/retention"),
                             ("post", "/api/community/social/chapters/c1/asset/restore")):
            self.assertEqual(getattr(self.client, method)(path).status_code, 401, path)

    def test_retained_assets_dto_has_no_storage_identifiers(self):
        r = self.client.get("/api/community/social/retained-assets", headers=self.h())
        self.assertEqual(r.status_code, 200)
        blob = r.text.lower()
        for bad in ("storage_file_id", "drive", "publication_id", "path", "googleapis"):
            self.assertNotIn(bad, blob, bad)

    def test_restore_is_owner_gated(self):
        r = self.client.post("/api/community/social/chapters/not-mine/asset/restore",
                             headers=self.h(), json={})
        self.assertEqual(r.status_code, 404)          # anti-enumeration, not 403
        self.assertEqual(self.retention.restored, [])

    def test_restore_rejects_any_client_supplied_field(self):
        for bad in ({"state": "restored"}, {"retain_until": 0}, {"owner_id": "x"},
                    {"publication_id": "p"}, {"force": True}):
            r = self.client.post("/api/community/social/chapters/c1/asset/restore",
                                 headers=self.h(), json=bad)
            self.assertEqual(r.status_code, 422, bad)
        self.assertEqual(self.retention.restored, [])

    def test_restore_happy_path_derives_owner_server_side(self):
        r = self.client.post("/api/community/social/chapters/c1/asset/restore",
                             headers=self.h(), json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.retention.restored, [("c1", "owner-A")])

    def test_no_endpoint_can_force_trash_or_delete(self):
        for path in ("/api/community/social/chapters/c1/asset/trash",
                     "/api/community/social/chapters/c1/asset/hard-delete",
                     "/api/community/social/reconcile"):
            self.assertIn(self.client.post(path, headers=self.h(), json={}).status_code, (404, 405))


if __name__ == "__main__":
    unittest.main()
