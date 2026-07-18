"""Chapter-asset linkage + authenticated PDF streaming — offline, Drive-spy proven.

Proves authorization precedes storage: a denied read never constructs or calls the Drive
provider, no file id/path/checksum leaks, and ownership always comes from the principal.
"""

import tempfile
import unittest
from pathlib import Path

from community_auth import AuthenticationRequired, RequestPrincipal
from community_store import FileStatus, PostStatus
from chapter_asset_repository import (
    AssetConflict, AssetNotFound, ChapterAssetRepository,
)
from social_content import SocialContentService
from supabase_social import SocialNotFound

OWNER = RequestPrincipal("owner-A", True, auth_source="supabase", session_id=None)
OTHER = RequestPrincipal("user-B", True, auth_source="supabase", session_id=None)
ANON = RequestPrincipal.anonymous()


class FakeCommunity:
    def __init__(self):
        self.posts = {}
        self.files = {}

    def get_post(self, pid):
        return self.posts.get(pid)

    def file_for_post(self, pid):
        return self.files.get(pid)

    def add(self, pid, owner, size=1000, sfid="drive-secret-xyz",
            status=PostStatus.PUBLISHED, upload=FileStatus.VERIFIED):
        self.posts[pid] = {"user_id": owner, "status": status}
        self.files[pid] = {"upload_status": upload, "storage_file_id": sfid, "size_bytes": size}


class _Stream:
    def __init__(self, start, end, total):
        self.start, self.end, self.total_size = start or 0, end if end is not None else total - 1, total
        self.content_length = self.end - self.start + 1

    def iter_chunks(self):
        yield b"%PDF-" + b"x" * (self.content_length - 5 if self.content_length > 5 else 0)


class DriveSpy:
    def __init__(self, total=1000):
        self.builds = 0
        self.opens = 0
        self.total = total

    def factory(self):
        self.builds += 1
        return self

    def open_stream(self, file_id, *, start=None, end=None):
        self.opens += 1
        return _Stream(start, end, self.total)


class FakeSocialRepo:
    """get_chapter mirrors Supabase RLS: visible → returns; hidden → SocialNotFound."""

    def __init__(self):
        self.visible = {}  # chapter_id -> set of user_ids who can read (owner + community)

    def allow(self, chapter_id, user_ids):
        self.visible[chapter_id] = set(user_ids)

    def get_chapter(self, token, chapter_id):
        user = token  # in these tests the "token" carries the user id
        if user in self.visible.get(chapter_id, set()):
            return {"id": chapter_id}
        raise SocialNotFound()


class AssetRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fc = FakeCommunity()
        self.fc.add("pubA", "owner-A")
        self.repo = ChapterAssetRepository(self.tmp / "a.sqlite3", community_store=self.fc)

    def tearDown(self):
        self.repo.close()

    def test_owner_links_own_publication(self):
        self.repo.link_asset("chapA", "pubA", "owner-A")
        self.assertEqual(self.repo.get_asset_for_read("chapA"), "drive-secret-xyz")

    def test_other_owner_cannot_link(self):
        with self.assertRaises(AssetNotFound):
            self.repo.link_asset("chapB", "pubA", "user-B")

    def test_relink_conflicts_without_replace(self):
        self.repo.link_asset("chapA", "pubA", "owner-A")
        with self.assertRaises(AssetConflict):
            self.repo.link_asset("chapA", "pubA", "owner-A")

    def test_replace_is_atomic_and_validates_new(self):
        self.fc.add("pubA2", "owner-A", sfid="drive-new")
        self.repo.link_asset("chapA", "pubA", "owner-A")
        self.repo.replace_asset("chapA", "pubA2", "owner-A")
        self.assertEqual(self.repo.get_asset_for_read("chapA"), "drive-new")

    def test_replace_failure_preserves_old(self):
        self.repo.link_asset("chapA", "pubA", "owner-A")
        with self.assertRaises(AssetNotFound):
            self.repo.replace_asset("chapA", "nonexistent", "owner-A")
        self.assertEqual(self.repo.get_asset_for_read("chapA"), "drive-secret-xyz")

    def test_unlink_blocks_read_and_is_idempotent(self):
        self.repo.link_asset("chapA", "pubA", "owner-A")
        self.repo.unlink_asset("chapA", "owner-A")
        self.repo.unlink_asset("chapA", "owner-A")  # idempotent
        with self.assertRaises(AssetNotFound):
            self.repo.get_asset_for_read("chapA")

    def test_deleted_publication_is_not_readable(self):
        self.fc.add("pubDel", "owner-A", status=PostStatus.DELETED)
        with self.assertRaises(AssetNotFound):
            self.repo.link_asset("chapDel", "pubDel", "owner-A")

    def test_unverified_file_is_not_readable(self):
        self.fc.add("pubUn", "owner-A", upload=FileStatus.UPLOADING)
        with self.assertRaises(AssetNotFound):
            self.repo.link_asset("chapUn", "pubUn", "owner-A")

    def test_status_exposes_only_booleans_for_reader(self):
        self.repo.link_asset("chapA", "pubA", "owner-A")
        reader = self.repo.get_asset_status("chapA", is_owner=False)
        self.assertEqual(set(reader), {"linked", "available"})
        self.assertNotIn("storage_file_id", reader)
        self.assertNotIn("publication_id", reader)


class ContentAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fc = FakeCommunity()
        self.fc.add("pubA", "owner-A", size=1000)
        self.assets = ChapterAssetRepository(self.tmp / "a.sqlite3", community_store=self.fc)
        self.social = FakeSocialRepo()
        self.spy = DriveSpy(total=1000)
        self.svc = SocialContentService(self.social, self.assets,
                                        read_provider_factory=self.spy.factory)

    def tearDown(self):
        self.assets.close()

    def _link(self, chapter="chapA"):
        self.assets.link_asset(chapter, "pubA", "owner-A")

    def test_owner_reads_linked_chapter(self):
        self._link()
        self.social.allow("chapA", {"owner-A"})  # private: only owner
        meta = self.svc.head_content("owner-A", OWNER, "chapA")
        self.assertEqual(meta["total_size"], 1000)
        m2, stream = self.svc.open_content("owner-A", OWNER, "chapA", range_header="bytes=0-99")
        self.assertTrue(m2["partial"])
        self.assertEqual(m2["content_length"], 100)
        self.assertEqual(self.spy.opens, 1)

    def test_anonymous_denied_zero_drive(self):
        self._link()
        self.social.allow("chapA", {"owner-A"})
        with self.assertRaises(AuthenticationRequired):
            self.svc.open_content("", ANON, "chapA")
        self.assertEqual(self.spy.builds, 0)
        self.assertEqual(self.spy.opens, 0)

    def test_other_user_on_private_denied_zero_drive(self):
        self._link()
        self.social.allow("chapA", {"owner-A"})  # not community: B can't see
        with self.assertRaises(SocialNotFound):
            self.svc.open_content("user-B", OTHER, "chapA")
        self.assertEqual(self.spy.builds, 0)

    def test_other_user_on_community_reads(self):
        self._link()
        self.social.allow("chapA", {"owner-A", "user-B"})  # community: both see
        m, stream = self.svc.open_content("user-B", OTHER, "chapA")
        self.assertEqual(m["total_size"], 1000)
        self.assertEqual(self.spy.opens, 1)

    def test_unlinked_asset_denied_zero_drive(self):
        self.social.allow("chapNolink", {"owner-A", "user-B"})  # visible, but no asset
        with self.assertRaises(AssetNotFound):
            self.svc.open_content("user-B", OTHER, "chapNolink")
        self.assertEqual(self.spy.builds, 0)

    def test_missing_chapter_denied_zero_drive(self):
        with self.assertRaises(SocialNotFound):
            self.svc.open_content("owner-A", OWNER, "ghost")
        self.assertEqual(self.spy.builds, 0)

    def test_head_never_opens_storage(self):
        self._link()
        self.social.allow("chapA", {"owner-A"})
        self.svc.head_content("owner-A", OWNER, "chapA")
        self.assertEqual(self.spy.opens, 0)  # HEAD answers from metadata, no Drive read

    def test_invalid_range_is_416_after_auth(self):
        from social_content import RangeNotSatisfiable
        self._link()
        self.social.allow("chapA", {"owner-A"})
        with self.assertRaises(RangeNotSatisfiable):
            self.svc.open_content("owner-A", OWNER, "chapA", range_header="bytes=99999-")


if __name__ == "__main__":
    unittest.main()
