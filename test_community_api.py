"""Community API layer: publish, feed, read streaming, range, authorization — offline."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import community_api
from community_api import CommunityApi
from community_store import FileStatus, Moderation, PostStatus, Visibility
from job_store import JobStatus, JobStore


def _write_pdf(path: Path, size=2000):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n" + b"x" * size + b"\n%%EOF\n")


class CommunityApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.output_root = self.tmp / "output"
        (self.output_root / "chap").mkdir(parents=True)
        _write_pdf(self.output_root / "chap" / "chapter.pdf")
        (self.output_root / "chap" / "run_manifest.json").write_text(
            '{"pdf_filename":"chapter.pdf"}', encoding="utf-8")
        self.storage_root = self.tmp / "drive"
        self._patches = [
            patch.object(community_api, "OUTPUT_ROOT", self.output_root),
            patch.object(community_api, "COMMUNITY_STORAGE_ROOT", self.storage_root),
            patch.object(community_api, "storage_provider_name", lambda: "filesystem"),
        ]
        for p in self._patches:
            p.start()
        self.jobs = JobStore(self.tmp / "jobs.sqlite3")
        self.api = CommunityApi(self.jobs, community_db_path=self.tmp / "community.sqlite3",
                                output_root=self.output_root)

    def tearDown(self):
        self.api.close()
        self.jobs.close()
        for p in self._patches:
            p.stop()

    def _publish_and_run(self, slug="chap"):
        result = self.api.publish({"slug": slug, "series_slug": slug, "episode_number": "1",
                                   "series_title": "Chap", "title": "T"})
        # Run the publish job to completion via the community runner.
        import community_publish_runner
        self.jobs.claim_next_job("w1", 1)
        self.jobs.transition(result["job_id"], JobStatus.STARTING, expected_worker="w1")
        community_publish_runner.run_job(result["job_id"], str(self.tmp / "jobs.sqlite3"))
        return result

    def test_publish_creates_job_and_publishing_state(self):
        result = self.api.publish({"slug": "chap", "series_slug": "chap", "episode_number": "1"})
        self.assertTrue(result["job_id"])
        self.assertEqual(self.api.store.get_post(result["post_id"])["status"], PostStatus.PUBLISHING)

    def test_publish_rejects_missing_identifier(self):
        with self.assertRaises(community_api.CommunityError):
            self.api.publish({"series_slug": "x"})

    def test_feed_does_not_call_provider(self):
        result = self._publish_and_run()
        self.api.store.set_post_status(result["post_id"], PostStatus.PUBLISHED,
                                       moderation_status=Moderation.APPROVED)
        # If the feed touched the provider, a broken provider would raise. Patch it to blow up.
        with patch.object(community_api, "build_read_provider",
                          side_effect=AssertionError("feed must not call provider")):
            feed = self.api.feed()
        self.assertEqual(feed["count"], 1)
        self.assertNotIn("storage_file_id", feed["posts"][0])

    def test_read_streams_full_and_range(self):
        result = self._publish_and_run()
        self.api.store.set_post_status(result["post_id"], PostStatus.PUBLISHED,
                                       moderation_status=Moderation.APPROVED)
        meta, stream = self.api.open_pdf(result["post_id"])
        body = b"".join(stream.iter_chunks())
        self.assertEqual(len(body), meta["total_size"])
        self.assertEqual(meta["mime_type"], "application/pdf")
        # Range request returns a partial slice.
        meta2, stream2 = self.api.open_pdf(result["post_id"], range_header="bytes=0-99")
        self.assertTrue(meta2["partial"])
        self.assertEqual(meta2["content_length"], 100)
        self.assertEqual(len(b"".join(stream2.iter_chunks())), 100)

    def test_read_blocked_before_published(self):
        result = self.api.publish({"slug": "chap", "series_slug": "chap", "episode_number": "1"})
        with self.assertRaises(community_api.CommunityError):
            self.api.open_pdf(result["post_id"])

    def test_read_private_requires_owner(self):
        result = self._publish_and_run()
        self.api.store.set_post_status(result["post_id"], PostStatus.PUBLISHED,
                                       moderation_status=Moderation.APPROVED)
        self.api.store._conn.execute(  # make it private, other user
            "UPDATE community_posts SET visibility=?, user_id=? WHERE id=?",
            (Visibility.PRIVATE, "someone_else", result["post_id"]))
        with self.assertRaises(community_api.CommunityError):
            self.api.open_pdf(result["post_id"])

    def test_views_increment_on_read(self):
        result = self._publish_and_run()
        self.api.store.set_post_status(result["post_id"], PostStatus.PUBLISHED,
                                       moderation_status=Moderation.APPROVED)
        self.api.open_pdf(result["post_id"])
        self.assertEqual(self.api.store.get_post(result["post_id"])["views"], 1)

    def test_safe_disposition_name(self):
        self.assertEqual(community_api._safe_disposition_name('a"b;c.pdf'), "a_b_c.pdf")
        self.assertEqual(community_api._safe_disposition_name("../../etc/passwd"), "passwd")

    def test_unpublish_removes_from_feed(self):
        result = self._publish_and_run()
        self.api.store.set_post_status(result["post_id"], PostStatus.PUBLISHED,
                                       moderation_status=Moderation.APPROVED)
        self.assertEqual(self.api.feed()["count"], 1)
        self.api.unpublish(result["post_id"])
        self.assertEqual(self.api.feed()["count"], 0)


if __name__ == "__main__":
    unittest.main()
