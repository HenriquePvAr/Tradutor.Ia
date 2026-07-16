"""OAuth token source (fakes) and reconciliation — no real OAuth, no network."""

import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import drive_auth
from community_storage import FakeStorageProvider
from community_store import CommunityStore, FileStatus, PostStatus
from community_storage_reconcile import reconcile
from google_drive_storage import HttpResponse
from job_store import JobStatus, JobStore


class FakeTransport:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def request(self, method, url, *, headers=None, data=None, stream=False):
        self.calls.append({"method": method, "url": url, "data": data})
        return self.responder(method, url, data)


class TokenSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "sub" / "token.json"

    def test_refresh_writes_token_atomically_with_perms(self):
        def responder(method, url, data):
            return HttpResponse(200, {}, json.dumps(
                {"access_token": "AT", "expires_in": 3600}).encode())
        drive_auth.save_tokens(self.path, drive_auth.OAuthTokens(refresh_token="RT"))
        src = drive_auth.FileTokenSource(token_path=self.path, client_id="cid",
                                         client_secret="sec", transport=FakeTransport(responder))
        self.assertEqual(src.access_token(), "AT")
        self.assertTrue(self.path.is_file())
        if os.name != "nt":
            self.assertEqual(oct(self.path.stat().st_mode)[-3:], "600")

    def test_missing_refresh_token_raises(self):
        src = drive_auth.FileTokenSource(token_path=self.path, client_id="c", client_secret="s",
                                         transport=FakeTransport(lambda *a: HttpResponse(200, {}, b"{}")))
        with self.assertRaises(Exception):
            src.access_token()

    def test_token_file_never_contains_client_secret(self):
        drive_auth.save_tokens(self.path, drive_auth.OAuthTokens(access_token="AT", refresh_token="RT"))
        content = self.path.read_text(encoding="utf-8")
        self.assertNotIn("client_secret", content)
        self.assertNotIn("sec", content)

    def test_status_does_not_print_token_values(self):
        import io
        import contextlib
        drive_auth.save_tokens(self.path, drive_auth.OAuthTokens(access_token="SECRETVALUE",
                                                                 refresh_token="REFRESHVALUE",
                                                                 expiry=9e9))
        with unittest.mock.patch.dict(os.environ, {"GOOGLE_OAUTH_TOKEN_PATH": str(self.path)}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                drive_auth.main(["status"])
            out = buf.getvalue()
        self.assertNotIn("SECRETVALUE", out)
        self.assertNotIn("REFRESHVALUE", out)
        self.assertNotIn(str(self.path), out)
        self.assertIn("token_present: True", out)
        self.assertIn("token_valid: True", out)

    def test_authorize_runs_flow_and_saves_token_without_leaking(self):
        # The OAuth flow seam is mocked: no browser, no Google endpoint. The returned
        # token is saved and never printed.
        import io
        import contextlib
        fake_tokens = drive_auth.OAuthTokens(access_token="AT_SECRET", refresh_token="RT_SECRET",
                                             expiry=9e9)
        env = {"GOOGLE_OAUTH_CLIENT_ID": "cid", "GOOGLE_OAUTH_CLIENT_SECRET": "sec",
               "GOOGLE_OAUTH_TOKEN_PATH": str(self.path)}
        with unittest.mock.patch.dict(os.environ, env), \
                unittest.mock.patch.object(drive_auth, "run_installed_app_flow",
                                           return_value=fake_tokens) as flow:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = drive_auth.main(["authorize"])
            out = buf.getvalue()
        self.assertEqual(rc, 0)
        flow.assert_called_once()
        self.assertNotIn("AT_SECRET", out)
        self.assertNotIn("RT_SECRET", out)
        # Token was persisted for later refresh.
        saved = drive_auth.load_tokens(self.path)
        self.assertEqual(saved.refresh_token, "RT_SECRET")

    def test_authorize_requires_client_secret(self):
        with unittest.mock.patch.dict(os.environ, {"GOOGLE_OAUTH_CLIENT_ID": "cid"}, clear=False), \
                unittest.mock.patch.object(drive_auth, "load_local_environment_for_entrypoint",
                                           return_value=True):
            os.environ.pop("GOOGLE_OAUTH_CLIENT_SECRET", None)
            self.assertEqual(drive_auth.main(["authorize"]), 2)


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.community = CommunityStore(self.tmp / "c.sqlite3")
        self.jobs = JobStore(self.tmp / "j.sqlite3")
        self.provider = FakeStorageProvider()

    def tearDown(self):
        self.community.close()
        self.jobs.close()

    def _published_with_file(self, sha="abc", verified=True, storage_file_id="F1"):
        pid = self.community.create_post(user_id="local", series_slug="x", episode_number="1")
        fid = self.community.create_file(post_id=pid, filename="c.pdf", mime_type="application/pdf",
                                         size_bytes=3, sha256=sha, storage_provider="fake")
        if verified:
            self.community.update_file(fid, upload_status=FileStatus.VERIFIED,
                                       storage_file_id=storage_file_id)
        self.community.set_post_status(pid, PostStatus.PUBLISHED)
        return pid, fid

    def test_clean_state_no_findings(self):
        result = reconcile(self.community, self.jobs, None, mode="scan")
        self.assertEqual(result["findings"], [])

    def test_published_without_verified_file(self):
        pid = self.community.create_post(user_id="local")
        self.community.set_post_status(pid, PostStatus.PUBLISHED)
        result = reconcile(self.community, self.jobs, None, mode="scan")
        self.assertIn("published_without_verified_file", result["counts"])

    def test_publishing_without_job_repaired_safely(self):
        pid = self.community.create_post(user_id="local")
        self.community.set_post_status(pid, PostStatus.PUBLISHING)
        scan = reconcile(self.community, self.jobs, None, mode="scan")
        self.assertIn("publishing_without_active_job", scan["counts"])
        self.assertEqual(self.community.get_post(pid)["status"], PostStatus.PUBLISHING)  # scan doesn't change
        repair = reconcile(self.community, self.jobs, None, mode="repair-safe")
        self.assertEqual(repair["repaired"], 1)
        self.assertEqual(self.community.get_post(pid)["status"], PostStatus.FAILED)

    def test_size_mismatch_detected(self):
        # Upload 3 bytes to the provider, but the DB record claims a different size.
        s = self.provider.create_resumable_session(filename="c.pdf", mime_type="application/pdf",
                                                    size=3, parent_id="f")
        self.provider.upload_chunk(s, 0, b"abc")
        pid = self.community.create_post(user_id="local", series_slug="x", episode_number="1")
        fid = self.community.create_file(post_id=pid, filename="c.pdf", mime_type="application/pdf",
                                         size_bytes=999, sha256="zzz", storage_provider="fake")
        self.community.update_file(fid, upload_status=FileStatus.VERIFIED, storage_file_id=s.file_id)
        self.community.set_post_status(pid, PostStatus.PUBLISHED)
        result = reconcile(self.community, self.jobs, self.provider, mode="scan")
        self.assertIn("size_mismatch", result["counts"])

    def test_missing_remote_detected(self):
        pid, fid = self._published_with_file(storage_file_id="does-not-exist")
        result = reconcile(self.community, self.jobs, self.provider, mode="scan")
        self.assertIn("verified_file_missing_remote", result["counts"])


if __name__ == "__main__":
    import unittest.mock  # noqa: E402
    unittest.main()
