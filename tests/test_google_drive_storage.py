"""Google Drive provider tests via a fake HTTP transport — no network, no OAuth."""

import _test_bootstrap  # noqa: F401

import json
import unittest

from community_storage import StorageError
from google_drive_storage import GoogleDriveStorageProvider, HttpResponse


class FakeTokens:
    def __init__(self):
        self.token = "access-1"
        self.refreshes = 0

    def access_token(self):
        return self.token

    def refresh(self):
        self.refreshes += 1
        self.token = f"access-{self.refreshes + 1}"
        return self.token


class FakeTransport:
    """Scriptable transport: a queue of responses per (method, url-substring) or a handler."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, method, url, *, headers=None, data=None, stream=False):
        self.calls.append({"method": method, "url": url, "headers": headers or {},
                           "data": data, "stream": stream})
        return self.handler(method, url, headers or {}, data)


def _provider(handler, *, tokens=None):
    return GoogleDriveStorageProvider(
        transport=FakeTransport(handler), tokens=tokens or FakeTokens(),
        root_folder_id="ROOT", chunk_size=4)


class GoogleDriveTests(unittest.TestCase):
    def test_ensure_folder_creates_and_caches(self):
        created = {"n": 0}

        def handler(method, url, headers, data):
            if method == "GET" and "q=" in url:
                return HttpResponse(200, {}, json.dumps({"files": []}).encode())
            if method == "POST" and url.startswith("https://www.googleapis.com/drive/v3/files?fields=id"):
                created["n"] += 1
                return HttpResponse(200, {}, json.dumps({"id": "FOLDER1"}).encode())
            raise AssertionError(f"unexpected {method} {url}")

        p = _provider(handler)
        self.assertEqual(p.ensure_folder("series_x", "ROOT"), "FOLDER1")
        # Second call is cached, no extra create.
        self.assertEqual(p.ensure_folder("series_x", "ROOT"), "FOLDER1")
        self.assertEqual(created["n"], 1)

    def test_ensure_folder_reuses_existing(self):
        def handler(method, url, headers, data):
            if method == "GET":
                return HttpResponse(200, {}, json.dumps({"files": [{"id": "EXIST", "name": "s"}]}).encode())
            raise AssertionError("should not create")
        p = _provider(handler)
        self.assertEqual(p.ensure_folder("s", "ROOT"), "EXIST")

    def test_resumable_upload_308_then_complete(self):
        state = {"received": 0}

        def handler(method, url, headers, data):
            if method == "POST" and "uploadType=resumable" in url:
                return HttpResponse(200, {"Location": "https://upload/session/abc"})
            if method == "PUT":
                state["received"] += len(data)
                if state["received"] >= 8:
                    return HttpResponse(200, {}, json.dumps({"id": "FILE1"}).encode())
                return HttpResponse(308, {"Range": f"bytes=0-{state['received']-1}"})
            raise AssertionError(f"unexpected {method} {url}")

        p = _provider(handler)
        session = p.create_resumable_session(filename="c.pdf", mime_type="application/pdf",
                                             size=8, parent_id="FOLDER1")
        self.assertEqual(session.session_id, "https://upload/session/abc")
        r1 = p.upload_chunk(session, 0, b"1234")
        self.assertFalse(r1.completed)
        self.assertEqual(r1.uploaded, 4)
        r2 = p.upload_chunk(session, 4, b"5678")
        self.assertTrue(r2.completed)
        self.assertEqual(r2.file_id, "FILE1")

    def test_401_triggers_refresh_then_succeeds(self):
        tokens = FakeTokens()
        seen = {"n": 0}

        def handler(method, url, headers, data):
            seen["n"] += 1
            if seen["n"] == 1:
                return HttpResponse(401, {})
            return HttpResponse(200, {}, json.dumps({"id": "ROOT"}).encode())

        p = _provider(handler, tokens=tokens)
        self.assertTrue(p.health_check())
        self.assertEqual(tokens.refreshes, 1)

    def test_429_retries_with_retry_after(self):
        calls = {"n": 0}

        def handler(method, url, headers, data):
            calls["n"] += 1
            if calls["n"] == 1:
                return HttpResponse(429, {"Retry-After": "0"})
            return HttpResponse(200, {}, json.dumps(
                {"id": "F", "name": "c.pdf", "mimeType": "application/pdf", "size": "3"}).encode())

        p = _provider(handler)
        meta = p.stat_file("F")
        self.assertEqual(meta.size, 3)
        self.assertEqual(calls["n"], 2)

    def test_permanent_403_raises_without_infinite_loop(self):
        def handler(method, url, headers, data):
            return HttpResponse(403, {})
        p = _provider(handler)
        with self.assertRaises(StorageError) as ctx:
            p.stat_file("F")
        self.assertEqual(ctx.exception.status, 403)
        self.assertFalse(ctx.exception.transient)

    def test_404_exists_false(self):
        def handler(method, url, headers, data):
            return HttpResponse(404, {})
        p = _provider(handler)
        self.assertFalse(p.exists("missing"))

    def test_stat_metadata(self):
        def handler(method, url, headers, data):
            return HttpResponse(200, {}, json.dumps({
                "id": "F", "name": "c.pdf", "mimeType": "application/pdf", "size": "1234",
                "parents": ["FOLDER1"], "trashed": False, "md5Checksum": "abc"}).encode())
        p = _provider(handler)
        meta = p.stat_file("F")
        self.assertEqual((meta.size, meta.parent_id, meta.checksum), (1234, "FOLDER1", "abc"))

    def test_range_stream(self):
        def handler(method, url, headers, data):
            if "alt=media" in url:
                self.assertIn("Range", headers)
                return HttpResponse(206, {}, b"2345")
            return HttpResponse(200, {}, json.dumps(
                {"id": "F", "name": "c.pdf", "mimeType": "application/pdf", "size": "10"}).encode())
        p = _provider(handler)
        stream = p.open_stream("F", start=2, end=5)
        self.assertEqual(b"".join(stream.iter_chunks()), b"2345")
        self.assertEqual(stream.content_length, 4)
        self.assertEqual(stream.total_size, 10)

    def test_trash_uses_patch_not_delete(self):
        seen = {"method": ""}

        def handler(method, url, headers, data):
            seen["method"] = method
            self.assertIn("trashed", (data or b"").decode())
            return HttpResponse(200, {}, b"{}")
        p = _provider(handler)
        p.move_to_trash("F")
        self.assertEqual(seen["method"], "PATCH")

    def test_never_sets_public_permission(self):
        # The provider must never call the permissions endpoint with "anyone".
        def handler(method, url, headers, data):
            self.assertNotIn("permissions", url)
            if "uploadType=resumable" in url:
                return HttpResponse(200, {"Location": "s"})
            return HttpResponse(200, {}, json.dumps({"id": "F"}).encode())
        p = _provider(handler)
        s = p.create_resumable_session(filename="c.pdf", mime_type="application/pdf", size=1, parent_id="F")
        p.upload_chunk(s, 0, b"x")


if __name__ == "__main__":
    unittest.main()
