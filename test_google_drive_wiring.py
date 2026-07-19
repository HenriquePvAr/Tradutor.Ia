"""Offline tests for the production Google Drive wiring: transport, factories, full flow.

Everything runs without a real network, browser or Google endpoint: a fake requests
session drives the transport, a fake HTTP transport drives the provider through the real
factories, and OAuth is never invoked interactively. Credentials never appear in output.
"""

import _test_bootstrap  # noqa: F401

import io
import tempfile
import unittest
import contextlib
import os
from pathlib import Path
from unittest.mock import patch

from community_storage import StorageError, build_storage_provider
from community_store import CommunityStore, FileStatus, PostStatus
from community_service import CommunityService
from google_drive_factory import GoogleDriveConfig, build_google_drive_provider
from google_drive_storage import HttpResponse
from job_store import JobStatus, JobStore
import community_publish_runner
from community_auth import RequestPrincipal


# ---- a fake requests.Session for the concrete transport ---------------------
class _FakeResp:
    def __init__(self, status, headers=None, content=b"", chunks=None):
        self.status_code = status
        self.headers = headers or {}
        self._content = content
        self._chunks = chunks
        self.closed = False

    @property
    def content(self):
        return self._content

    def iter_content(self, chunk_size=65536):
        if self._chunks is not None:
            yield from self._chunks
        else:
            for i in range(0, len(self._content), chunk_size):
                yield self._content[i:i + chunk_size]

    def close(self):
        self.closed = True


class _FakeSession:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []
        self.closed = False

    def request(self, method, url, *, headers=None, data=None, stream=False, timeout=None,
                allow_redirects=True):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "stream": stream, "timeout": timeout})
        return self.responder(method, url, headers, data, stream)

    def close(self):
        self.closed = True


class RequestsTransportTests(unittest.TestCase):
    def _transport(self, responder):
        from google_drive_transport import RequestsHttpTransport
        return RequestsHttpTransport(session=_FakeSession(responder)), None

    def test_get_returns_status_headers_body(self):
        from google_drive_transport import RequestsHttpTransport
        session = _FakeSession(lambda *a: _FakeResp(200, {"X": "1"}, b"hello"))
        t = RequestsHttpTransport(session=session)
        resp = t.request("GET", "https://x/y")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers["X"], "1")
        self.assertEqual(resp.content, b"hello")
        self.assertEqual(session.calls[0]["timeout"], (10.0, 60.0))

    def test_stream_iterates_without_loading_all(self):
        from google_drive_transport import RequestsHttpTransport
        chunks = [b"a" * 10, b"b" * 10]
        session = _FakeSession(lambda *a: _FakeResp(206, {"Content-Range": "bytes 0-19/20"},
                                                    chunks=chunks))
        t = RequestsHttpTransport(session=session)
        resp = t.request("GET", "https://x/y", stream=True)
        got = list(resp.iter_content())
        self.assertEqual(got, chunks)

    def test_connection_error_becomes_transient_storage_error(self):
        import requests
        from google_drive_transport import RequestsHttpTransport

        def boom(*a):
            raise requests.exceptions.ConnectionError("reset")

        t = RequestsHttpTransport(session=_FakeSession(boom))
        with self.assertRaises(StorageError) as ctx:
            t.request("GET", "https://x")
        self.assertTrue(ctx.exception.transient)

    def test_tls_error_is_fatal(self):
        import requests
        from google_drive_transport import RequestsHttpTransport

        def boom(*a):
            raise requests.exceptions.SSLError("bad cert")

        t = RequestsHttpTransport(session=_FakeSession(boom))
        with self.assertRaises(StorageError) as ctx:
            t.request("GET", "https://x")
        self.assertFalse(ctx.exception.transient)


class ConfigTests(unittest.TestCase):
    def test_from_env_fails_closed_when_incomplete(self):
        with self.assertRaises(StorageError) as ctx:
            GoogleDriveConfig.from_env(env={})
        self.assertIn("not configured", str(ctx.exception))

    def test_repr_redacts_secrets(self):
        cfg = GoogleDriveConfig(root_folder_id="folderXYZ", token_path="/t",
                                client_id="CID", client_secret="SECRET")
        text = repr(cfg)
        self.assertNotIn("CID", text)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("/t", text)
        self.assertIn("redacted", text)

    def test_default_scope_is_drive_file_only(self):
        cfg = GoogleDriveConfig(root_folder_id="f", token_path="/t", client_id="c",
                                client_secret="s")
        self.assertEqual(cfg.scopes, ("https://www.googleapis.com/auth/drive.file",))


# ---- a fake HTTP transport that speaks the Drive protocol -------------------
class FakeDriveTransport:
    """Simulates the Drive endpoints used by the provider, plus a one-shot 401 to exercise
    token refresh. Records Authorization headers only to assert they are Bearer tokens."""

    def __init__(self, *, fail_first_auth_on=None):
        self.files = {}
        self.sessions = {}
        self.counter = 0
        self.fail_first_auth_on = fail_first_auth_on  # a method that 401s once
        self._did_401 = False
        self.refresh_calls = 0

    def _id(self, prefix):
        self.counter += 1
        return f"{prefix}{self.counter}"

    def request(self, method, url, *, headers=None, data=None, stream=False):
        import json
        if url.endswith("oauth2.googleapis.com/token") or "oauth2.googleapis.com/token" in url:
            self.refresh_calls += 1
            return HttpResponse(200, {}, json.dumps(
                {"access_token": "fresh", "expires_in": 3600}).encode())
        # one-shot 401 to trigger refresh
        if self.fail_first_auth_on and method == self.fail_first_auth_on and not self._did_401:
            self._did_401 = True
            return HttpResponse(401, {}, b"")
        if method == "POST" and url.startswith("https://www.googleapis.com/drive/v3/files?fields=id"):
            return HttpResponse(200, {}, json.dumps({"id": self._id("folder")}).encode())
        if method == "POST" and "uploadType=resumable" in url:
            sid = "https://upload/" + self._id("s")
            fid = self._id("file")
            self.sessions[sid] = {"file_id": fid, "uploaded": 0}
            self.files[fid] = {"name": json.loads(data)["name"], "data": b"",
                               "mime": "application/pdf", "trashed": False,
                               "parents": [json.loads(data)["parents"][0]]}
            return HttpResponse(200, {"Location": sid}, b"")
        if method == "PUT" and url in self.sessions:
            s = self.sessions[url]
            crange = headers["Content-Range"]
            total = int(crange.split("/")[1])
            s["uploaded"] += len(data)
            self.files[s["file_id"]]["data"] += data
            if s["uploaded"] >= total:
                return HttpResponse(200, {}, json.dumps({"id": s["file_id"]}).encode())
            return HttpResponse(308, {"Range": f"bytes=0-{s['uploaded']-1}"}, b"")
        if method == "GET" and "/files/" in url and "alt=media" in url:
            fid = url.split("/files/")[1].split("?")[0]
            payload = self.files[fid]["data"]
            rng = (headers or {}).get("Range")
            status = 200
            if rng:
                lo, hi = rng.replace("bytes=", "").split("-")
                lo, hi = int(lo), int(hi)
                payload = payload[lo:hi + 1]
                status = 206
            return HttpResponse(status, {}, payload)
        if method == "GET" and "/files/" in url:
            fid = url.split("/files/")[1].split("?")[0]
            f = self.files.get(fid)
            if not f:
                return HttpResponse(404, {}, b"")
            return HttpResponse(200, {}, json.dumps(
                {"id": fid, "name": f["name"], "mimeType": f["mime"], "size": str(len(f["data"])),
                 "parents": f["parents"], "trashed": f["trashed"], "md5Checksum": "abc"}).encode())
        if method == "GET" and url.startswith("https://www.googleapis.com/drive/v3/files?q="):
            return HttpResponse(200, {}, json.dumps({"files": []}).encode())
        if method == "PATCH" and "/files/" in url:
            fid = url.split("/files/")[1]
            if fid in self.files:
                self.files[fid]["trashed"] = True
            return HttpResponse(200, {}, json.dumps({"id": fid}).encode())
        return HttpResponse(400, {}, b"unexpected")


class _FakeTokens:
    def __init__(self, transport):
        self._t = transport
        self.refreshed = 0

    def access_token(self):
        return "tok"

    def refresh(self):
        self.refreshed += 1
        return "tok2"


class FullFlowThroughFactoriesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.output_root = self.tmp / "output"
        (self.output_root / "chap").mkdir(parents=True)
        (self.output_root / "chap" / "chapter.pdf").write_bytes(b"%PDF-1.4\n" + b"x" * 900 + b"\n%%EOF\n")
        (self.output_root / "chap" / "run_manifest.json").write_text(
            '{"pdf_filename":"chapter.pdf"}', encoding="utf-8")
        self.community_db = self.tmp / "community.sqlite3"
        self.jobs_db = self.tmp / "jobs.sqlite3"
        self.store = CommunityStore(self.community_db)
        self.jobs = JobStore(self.jobs_db)
        self.svc = CommunityService(self.store, self.jobs, output_root=self.output_root,
                                    provider_name="google_drive",
                                    community_db_path=str(self.community_db),
                                    storage_config={"root_folder_id": "ROOT"})

    def tearDown(self):
        self.store.close()
        self.jobs.close()

    def _publish(self):
        principal = RequestPrincipal(
            "local", True, auth_source="test", session_id="test-owner")
        draft = self.svc.create_draft(principal=principal, output_dir=str(self.output_root / "chap"),
                                      series_slug="chap", episode_number="1", series_title="Chap")
        return self.svc.request_publish(draft["post_id"], principal=principal)

    def _run_with_fake_transport(self, job_id, transport):
        # Build the real provider through the real factory, but inject the fake transport
        # and token source instead of the requests/OAuth ones.
        real_build = build_google_drive_provider

        def patched(config, *, transport=None, tokens=None):
            return real_build(config, transport=transport_impl[0], tokens=_FakeTokens(transport_impl[0]))

        transport_impl = [transport]
        env = {"COMMUNITY_DRIVE_ROOT_FOLDER_ID": "ROOT", "GOOGLE_OAUTH_TOKEN_PATH": str(self.tmp / "t.json"),
               "GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "sec"}
        with patch.dict(os.environ, env), \
                patch("google_drive_factory.build_google_drive_provider", patched):
            self.jobs.claim_next_job("w1", 1)
            self.jobs.transition(job_id, JobStatus.STARTING, expected_worker="w1")
            return community_publish_runner.run_job(job_id, str(self.jobs_db))

    def test_full_publish_through_real_wiring(self):
        pub = self._publish()
        transport = FakeDriveTransport()
        rc = self._run_with_fake_transport(pub["job_id"], transport)
        self.assertEqual(rc, 0)
        self.assertEqual(self.jobs.get_job(pub["job_id"])["status"], JobStatus.FINISHED)
        self.assertEqual(self.store.get_post(pub["post_id"])["status"], PostStatus.PUBLISHED)
        file = self.store.get_file(pub["file_id"])
        self.assertEqual(file["upload_status"], FileStatus.VERIFIED)
        self.assertTrue(file["storage_file_id"])

    def test_refresh_during_upload(self):
        pub = self._publish()
        transport = FakeDriveTransport(fail_first_auth_on="PUT")  # first chunk 401s
        rc = self._run_with_fake_transport(pub["job_id"], transport)
        self.assertEqual(rc, 0)
        self.assertEqual(self.jobs.get_job(pub["job_id"])["status"], JobStatus.FINISHED)

    def test_read_range_through_provider(self):
        pub = self._publish()
        transport = FakeDriveTransport()
        self._run_with_fake_transport(pub["job_id"], transport)
        file = self.store.get_file(pub["file_id"])
        provider = build_google_drive_provider(
            GoogleDriveConfig(root_folder_id="ROOT", token_path="/t", client_id="c",
                              client_secret="s"),
            transport=transport, tokens=_FakeTokens(transport))
        stream = provider.open_stream(file["storage_file_id"], start=2, end=5)
        self.assertEqual(stream.content_length, 4)
        self.assertEqual(len(b"".join(stream.iter_chunks())), 4)


if __name__ == "__main__":
    unittest.main()
