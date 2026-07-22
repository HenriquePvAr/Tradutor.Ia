"""Community API layer: publish, feed, read streaming, range, authorization — offline."""

import _test_bootstrap  # noqa: F401

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import community_api
from community_auth import RequestPrincipal, ResourceNotFound
from community_api import CommunityApi
from community_store import FileStatus, Moderation, PostStatus, Visibility
from job_store import JobStatus, JobStore


OWNER = RequestPrincipal(
    "local", True, roles=frozenset({"admin"}), auth_source="test", session_id="test-owner")
NON_ADMIN = RequestPrincipal(
    "local", True, auth_source="test", session_id="test-non-admin")
# A different signed-in community member (not the owner): the community is
# authenticated-read, so a member may read a published post but not manage it.
MEMBER = RequestPrincipal(
    "member-x", True, auth_source="test", session_id="test-member")
ANONYMOUS = RequestPrincipal.anonymous()


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
                                   "series_title": "Chap", "title": "T"}, principal=OWNER)
        # Run the publish job to completion via the community runner.
        import community_publish_runner
        self.jobs.claim_next_job("w1", 1)
        self.jobs.transition(result["job_id"], JobStatus.STARTING, expected_worker="w1")
        community_publish_runner.run_job(result["job_id"], str(self.tmp / "jobs.sqlite3"))
        return result

    def test_publish_creates_job_and_publishing_state(self):
        result = self.api.publish({"slug": "chap", "series_slug": "chap", "episode_number": "1"},
                                  principal=OWNER)
        self.assertTrue(result["job_id"])
        self.assertEqual(self.api.store.get_post(result["post_id"])["status"], PostStatus.PUBLISHING)

    def test_publish_rejects_missing_identifier(self):
        with self.assertRaises(community_api.CommunityError):
            self.api.publish({"series_slug": "x"}, principal=OWNER)

    def test_feed_does_not_call_provider(self):
        result = self._publish_and_run()
        self.api.store.set_post_status(result["post_id"], PostStatus.PUBLISHED,
                                       moderation_status=Moderation.APPROVED)
        # If the feed touched the provider, a broken provider would raise. Patch it to blow up.
        with patch.object(community_api, "build_read_provider",
                          side_effect=AssertionError("feed must not call provider")):
            feed = self.api.feed(principal=MEMBER)
        self.assertEqual(feed["count"], 1)
        self.assertNotIn("storage_file_id", feed["posts"][0])

    def test_public_profile_name_is_normalized_and_atomically_unique(self):
        saved = self.api.store.upsert_profile("user-a", {
            "display_name": "  Kayden   Rivers  ", "public_role": "Translator",
        })
        self.assertEqual(saved["display_name"], "Kayden Rivers")
        normalized = self.api.store._conn.execute(
            "SELECT display_name_normalized FROM community_profiles WHERE user_id='user-a'"
        ).fetchone()[0]
        self.assertEqual(normalized, "kayden rivers")
        with self.assertRaisesRegex(ValueError, "display_name_taken"):
            self.api.store.upsert_profile("user-b", {"display_name": "KAYDEN RIVERS"})
        self.assertEqual(self.api.store.profile_public("user-a")["display_name"], "Kayden Rivers")

    def test_feed_card_joins_public_author_without_private_fields(self):
        result = self._publish_and_run()
        self.api.store.set_post_status(result["post_id"], PostStatus.PUBLISHED,
                                       moderation_status=Moderation.APPROVED)
        self.api.store.upsert_profile(OWNER.user_id, {
            "display_name": "Kayden", "public_role": "Translator",
            "avatar_object_key": "local:avatar",
        })
        card = self.api.feed(principal=MEMBER)["posts"][0]
        self.assertEqual(card["author"]["display_name"], "Kayden")
        self.assertEqual(card["author"]["public_role"], "Translator")
        self.assertTrue(card["author"]["avatar_url"].endswith(f"/{OWNER.user_id}/avatar"))
        self.assertNotIn("email", str(card).lower())
        self.assertNotIn("object_key", card["author"])

    def test_read_streams_full_and_range(self):
        result = self._publish_and_run()
        self.api.store.set_post_status(result["post_id"], PostStatus.PUBLISHED,
                                       moderation_status=Moderation.APPROVED)
        meta, stream = self.api.open_pdf(result["post_id"], principal=MEMBER)
        body = b"".join(stream.iter_chunks())
        self.assertEqual(len(body), meta["total_size"])
        self.assertEqual(meta["mime_type"], "application/pdf")
        # Range request returns a partial slice.
        meta2, stream2 = self.api.open_pdf(result["post_id"], principal=MEMBER,
                                           range_header="bytes=0-99")
        self.assertTrue(meta2["partial"])
        self.assertEqual(meta2["content_length"], 100)
        self.assertEqual(len(b"".join(stream2.iter_chunks())), 100)

    def test_read_blocked_before_published(self):
        result = self.api.publish({"slug": "chap", "series_slug": "chap", "episode_number": "1"},
                                  principal=OWNER)
        with self.assertRaises(ResourceNotFound):
            self.api.open_pdf(result["post_id"], principal=MEMBER)

    def test_community_read_and_feed_require_authentication(self):
        from community_auth import AuthenticationRequired
        result = self._publish_and_run()
        self.api.store.set_post_status(result["post_id"], PostStatus.PUBLISHED,
                                       moderation_status=Moderation.APPROVED)
        # Anonymous cannot read a community post nor list the feed.
        with self.assertRaises(AuthenticationRequired):
            self.api.open_pdf(result["post_id"], principal=ANONYMOUS)
        with self.assertRaises(AuthenticationRequired):
            self.api.feed(principal=ANONYMOUS)
        # Any authenticated member can.
        self.assertEqual(self.api.feed(principal=MEMBER)["count"], 1)

    def test_read_private_requires_owner(self):
        result = self._publish_and_run()
        self.api.store.set_post_status(result["post_id"], PostStatus.PUBLISHED,
                                       moderation_status=Moderation.APPROVED)
        self.api.store._conn.execute(  # make it private, other user
            "UPDATE community_posts SET visibility=?, user_id=? WHERE id=?",
            (Visibility.PRIVATE, "someone_else", result["post_id"]))
        with self.assertRaises(ResourceNotFound):
            self.api.open_pdf(result["post_id"], principal=NON_ADMIN)

    def test_views_increment_on_read(self):
        result = self._publish_and_run()
        self.api.store.set_post_status(result["post_id"], PostStatus.PUBLISHED,
                                       moderation_status=Moderation.APPROVED)
        self.api.open_pdf(result["post_id"], principal=MEMBER)
        self.assertEqual(self.api.store.get_post(result["post_id"])["views"], 1)

    def test_open_pdf_releases_community_lock_before_slow_provider_factory(self):
        result = self._publish_and_run()
        self.api.store.set_post_status(
            result["post_id"],
            PostStatus.PUBLISHED,
            moderation_status=Moderation.APPROVED,
        )
        provider_factory_entered = threading.Event()
        release_provider_factory = threading.Event()
        original_provider_factory = self.api._read_provider_factory
        read_errors = []

        def slow_provider_factory():
            provider_factory_entered.set()
            if not release_provider_factory.wait(timeout=5):
                raise AssertionError("provider factory was not released by the test")
            return original_provider_factory()

        def open_pdf():
            try:
                self.api.open_pdf(result["post_id"], principal=MEMBER)
            except BaseException as exc:  # surface failures from the helper thread
                read_errors.append(exc)

        self.api._read_provider_factory = slow_provider_factory
        read_thread = threading.Thread(target=open_pdf, daemon=True)
        read_thread.start()
        self.assertTrue(provider_factory_entered.wait(timeout=5))

        lock_was_free = self.api._community_lock.acquire(blocking=False)
        feed = None
        if lock_was_free:
            try:
                feed = self.api.feed(principal=MEMBER)
            finally:
                self.api._community_lock.release()

        release_provider_factory.set()
        read_thread.join(timeout=5)

        self.assertTrue(
            lock_was_free,
            "open_pdf held the community lock while initializing the storage provider",
        )
        self.assertFalse(read_thread.is_alive())
        self.assertEqual(read_errors, [])
        self.assertEqual(feed["count"], 1)

    def test_safe_disposition_name(self):
        self.assertEqual(community_api._safe_disposition_name('a"b;c.pdf'), "a_b_c.pdf")
        self.assertEqual(community_api._safe_disposition_name("../../etc/passwd"), "passwd")

    def test_unpublish_removes_from_feed(self):
        result = self._publish_and_run()
        self.api.store.set_post_status(result["post_id"], PostStatus.PUBLISHED,
                                       moderation_status=Moderation.APPROVED)
        self.assertEqual(self.api.feed(principal=MEMBER)["count"], 1)
        self.api.unpublish(result["post_id"], principal=OWNER)
        self.assertEqual(self.api.feed(principal=MEMBER)["count"], 0)


if __name__ == "__main__":
    unittest.main()
