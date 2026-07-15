"""Community model, storage abstraction, validation and publish flow — all offline."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from community_storage import (
    FakeStorageProvider, FilesystemStorageProvider, StorageError, build_storage_provider,
)
from community_store import CommunityStore, FileStatus, Moderation, PostStatus, Visibility
from community_service import CommunityService, CommunityError, safe_pdf_filename, sha256_of_file
from job_store import JobStore


def _write_pdf(path: Path, body: bytes = b"body", *, magic=b"%PDF-1.4\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(magic + body + b"\n%%EOF\n")
    return path


class StorageProviderTests(unittest.TestCase):
    def test_resumable_chunked_upload_and_stream(self):
        p = FakeStorageProvider()
        folder = p.ensure_folder("series_x", "root")
        session = p.create_resumable_session(filename="c.pdf", mime_type="application/pdf",
                                             size=10, parent_id=folder)
        r1 = p.upload_chunk(session, 0, b"12345")
        self.assertFalse(r1.completed)
        r2 = p.upload_chunk(session, 5, b"67890")
        self.assertTrue(r2.completed)
        self.assertTrue(r2.file_id)
        meta = p.stat_file(r2.file_id)
        self.assertEqual(meta.size, 10)
        self.assertEqual(meta.mime_type, "application/pdf")
        stream = p.open_stream(r2.file_id)
        self.assertEqual(b"".join(stream.iter_chunks()), b"1234567890")

    def test_range_read(self):
        p = FakeStorageProvider()
        s = p.create_resumable_session(filename="c.pdf", mime_type="application/pdf", size=10, parent_id="f")
        p.upload_chunk(s, 0, b"0123456789")
        stream = p.open_stream(s.file_id, start=2, end=5)
        self.assertEqual(b"".join(stream.iter_chunks()), b"2345")
        self.assertEqual(stream.content_length, 4)
        self.assertEqual(stream.total_size, 10)

    def test_transient_failure_then_success(self):
        p = FakeStorageProvider(transient_failures=1)
        s = p.create_resumable_session(filename="c.pdf", mime_type="application/pdf", size=3, parent_id="f")
        with self.assertRaises(StorageError) as ctx:
            p.upload_chunk(s, 0, b"abc")
        self.assertTrue(ctx.exception.transient)
        r = p.upload_chunk(s, 0, b"abc")  # retry succeeds
        self.assertTrue(r.completed)

    def test_offline_provider_raises_transient(self):
        p = FakeStorageProvider(online=False)
        with self.assertRaises(StorageError) as ctx:
            p.ensure_folder("x", "root")
        self.assertTrue(ctx.exception.transient)

    def test_trash_hides_file(self):
        p = FakeStorageProvider()
        s = p.create_resumable_session(filename="c.pdf", mime_type="application/pdf", size=1, parent_id="f")
        p.upload_chunk(s, 0, b"a")
        p.move_to_trash(s.file_id)
        self.assertFalse(p.exists(s.file_id))

    def test_filesystem_provider_persists(self):
        root = Path(tempfile.mkdtemp())
        p = FilesystemStorageProvider(root)
        s = p.create_resumable_session(filename="c.pdf", mime_type="application/pdf", size=4, parent_id="f")
        p.upload_chunk(s, 0, b"ab")
        # A fresh provider on the same dir (another process) resumes the same file.
        p2 = FilesystemStorageProvider(root)
        s.session_id = s.session_id  # same session id persisted on disk
        p2.upload_chunk(s, 2, b"cd")
        self.assertEqual(p2.stat_file(s.file_id).size, 4)

    def test_factory_rejects_unknown(self):
        with self.assertRaises(StorageError):
            build_storage_provider({"storage_provider": "s3"})


class CommunityStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = CommunityStore(self.tmp / "community.sqlite3")

    def tearDown(self):
        self.store.close()

    def test_create_post_is_draft(self):
        pid = self.store.create_post(user_id="local", series_slug="x", episode_number="1")
        post = self.store.get_post(pid)
        self.assertEqual(post["status"], PostStatus.DRAFT)
        self.assertEqual(post["moderation_status"], Moderation.PENDING)

    def test_feed_only_published_approved(self):
        pid = self.store.create_post(user_id="local", series_slug="x", episode_number="1", title="A")
        self.assertEqual(self.store.feed(), [])  # draft not shown
        self.store.set_post_status(pid, PostStatus.PUBLISHED, moderation_status=Moderation.APPROVED)
        self.assertEqual(len(self.store.feed()), 1)

    def test_feed_hides_unapproved(self):
        pid = self.store.create_post(user_id="local", series_slug="x", episode_number="1")
        self.store.set_post_status(pid, PostStatus.PUBLISHED)  # moderation still pending
        self.assertEqual(self.store.feed(require_moderation=True), [])

    def test_migration_idempotent(self):
        pid = self.store.create_post(user_id="local")
        self.store.close()
        store2 = CommunityStore(self.tmp / "community.sqlite3")
        self.assertIsNotNone(store2.get_post(pid))
        store2.close()

    def test_duplicate_sha_detection(self):
        pid = self.store.create_post(user_id="local", series_slug="x", episode_number="1")
        fid = self.store.create_file(post_id=pid, filename="c.pdf", mime_type="application/pdf",
                                     size_bytes=3, sha256="abc", storage_provider="fake")
        self.store.update_file(fid, upload_status=FileStatus.VERIFIED, storage_file_id="f1")
        self.store.set_post_status(pid, PostStatus.PUBLISHED)
        self.assertIsNotNone(self.store.published_sha_exists("abc", exclude_post="other"))


class CommunityServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.output_root = self.tmp / "output"
        self.output_root.mkdir()
        self.store = CommunityStore(self.tmp / "community.sqlite3")
        self.jobs = JobStore(self.tmp / "jobs.sqlite3")
        self.svc = CommunityService(self.store, self.jobs, output_root=self.output_root,
                                    provider_name="fake",
                                    community_db_path=str(self.tmp / "community.sqlite3"))

    def tearDown(self):
        self.store.close()
        self.jobs.close()

    def _chapter(self, slug="platform_zero_ep1"):
        out = self.output_root / slug
        _write_pdf(out / "chapter.pdf")
        (out / "run_manifest.json").write_text('{"pdf_filename":"chapter.pdf"}', encoding="utf-8")
        return out

    def test_validate_rejects_path_traversal(self):
        with self.assertRaises(CommunityError) as ctx:
            self.svc.create_draft(output_dir=str(self.tmp / "elsewhere"))
        self.assertIn("output_outside_root", str(ctx.exception))

    def test_validate_rejects_non_pdf(self):
        out = self.output_root / "bad"
        out.mkdir()
        (out / "chapter.pdf").write_bytes(b"NOTPDF")
        with self.assertRaises(CommunityError):
            self.svc.create_draft(output_dir=str(out))

    def test_create_draft_and_publish_creates_job(self):
        out = self._chapter()
        draft = self.svc.create_draft(output_dir=str(out), series_slug="platform_zero",
                                      episode_number="1", series_title="Platform Zero")
        result = self.svc.request_publish(draft["post_id"])
        self.assertTrue(result["job_id"])
        job = self.jobs.get_job(result["job_id"])
        self.assertEqual(job["configuration"]["job_type"], "community_publish")
        self.assertEqual(self.store.get_post(draft["post_id"])["status"], PostStatus.PUBLISHING)
        file = self.store.get_file(result["file_id"])
        self.assertEqual(file["upload_status"], FileStatus.PENDING)
        self.assertTrue(file["sha256"])

    def test_publish_blocks_duplicate_active(self):
        out = self._chapter()
        draft = self.svc.create_draft(output_dir=str(out), series_slug="x", episode_number="1")
        self.svc.request_publish(draft["post_id"])
        with self.assertRaises(CommunityError) as ctx:
            self.svc.request_publish(draft["post_id"])
        self.assertIn("publish_already_active", str(ctx.exception))

    def test_feed_does_not_expose_storage_file_id(self):
        out = self._chapter()
        draft = self.svc.create_draft(output_dir=str(out), series_slug="x", episode_number="1", title="T")
        self.store.set_post_status(draft["post_id"], PostStatus.PUBLISHED,
                                   moderation_status=Moderation.APPROVED)
        cards = self.svc.feed()
        self.assertEqual(len(cards), 1)
        self.assertNotIn("storage_file_id", cards[0])

    def test_read_requires_verified_file(self):
        out = self._chapter()
        draft = self.svc.create_draft(output_dir=str(out), series_slug="x", episode_number="1")
        self.store.set_post_status(draft["post_id"], PostStatus.PUBLISHED,
                                   moderation_status=Moderation.APPROVED)
        with self.assertRaises(CommunityError) as ctx:
            self.svc.resolve_readable_file(draft["post_id"])
        self.assertIn("file_not_available", str(ctx.exception))

    def test_sha256_streaming(self):
        out = self._chapter()
        pdf = out / "chapter.pdf"
        digest, size = sha256_of_file(pdf)
        self.assertEqual(digest, hashlib.sha256(pdf.read_bytes()).hexdigest())
        self.assertEqual(size, pdf.stat().st_size)

    def test_safe_filename(self):
        self.assertEqual(safe_pdf_filename("a/b c", "1", "x.pdf"), "a_b_c_capitulo_1.pdf")

    def test_unpublish_keeps_file(self):
        out = self._chapter()
        draft = self.svc.create_draft(output_dir=str(out), series_slug="x", episode_number="1")
        self.store.set_post_status(draft["post_id"], PostStatus.PUBLISHED)
        self.svc.unpublish(draft["post_id"])
        self.assertEqual(self.store.get_post(draft["post_id"])["status"], PostStatus.UNPUBLISHED)


if __name__ == "__main__":
    unittest.main()
