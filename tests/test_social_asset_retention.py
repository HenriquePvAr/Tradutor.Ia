"""Retention / restore / trash / reconcile — offline, fake clock, no Drive, no Supabase."""

import _test_bootstrap  # noqa: F401

import tempfile
import unittest
from pathlib import Path

from chapter_asset_repository import AssetNotFound, ChapterAssetRepository
from community_store import FileStatus, PostStatus
from social_asset_reconcile import (
    RECONCILE_REQUIRED, SAFE_FOR_TRASH, SocialAssetReconciliationService,
    SocialAssetRetentionSweep,
)
from social_asset_retention import (
    DAY_SECONDS, DEFAULT_RETENTION_DAYS, RetentionConflict, RetentionError,
    SocialAssetRetentionService, retention_days, sweep_enabled,
)

OWNER = "owner-A"


class FakeStore:
    def __init__(self):
        self.posts, self.files = {}, {}

    def get_post(self, pid):
        return self.posts.get(pid)

    def file_for_post(self, pid):
        return self.files.get(pid)

    def verify(self, pid, sfid, owner=OWNER):
        self.posts[pid] = {"user_id": owner, "status": PostStatus.PUBLISHED}
        self.files[pid] = {"upload_status": FileStatus.VERIFIED, "storage_file_id": sfid,
                           "size_bytes": 10, "mime_type": "application/pdf"}


class FakeProvider:
    def __init__(self, *, fail=False):
        self.trashed, self.deleted, self.fail = [], [], fail

    def move_to_trash(self, fid):
        if self.fail:
            raise RuntimeError("boom")
        self.trashed.append(fid)

    def stat_file(self, fid):
        class M:
            trashed = fid in self.trashed
        return M()

    def delete_file(self, fid):           # must never be called by this phase
        self.deleted.append(fid)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance_days(self, d):
        self.t += d * DAY_SECONDS


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = FakeStore()
        self.assets = ChapterAssetRepository(self.tmp / "a.sqlite3", community_store=self.store)
        self.clock = Clock()
        self.provider = FakeProvider()
        self.svc = SocialAssetRetentionService(
            self.assets, provider_factory=lambda: self.provider, clock=self.clock, env={})

    def tearDown(self):
        self.assets.close()

    def link(self, chapter, pid, sfid):
        self.store.verify(pid, sfid)
        self.assets.link_asset(chapter, pid, OWNER)

    def retain(self, chapter="c1", pid="pub-1", sfid="drive-1"):
        self.link(chapter, pid, sfid)
        self.assets.unlink_asset(chapter, OWNER)
        rid = self.svc.retain_unlinked_asset(chapter, pid, OWNER)
        return self.assets.get_retention(rid)


class ConfigTests(Base):
    def test_defaults_and_clamping(self):
        self.assertEqual(retention_days({}), DEFAULT_RETENTION_DAYS)
        self.assertEqual(retention_days({"COMMUNITY_ASSET_RETENTION_DAYS": "0"}), 1)
        self.assertEqual(retention_days({"COMMUNITY_ASSET_RETENTION_DAYS": "9999"}), 365)
        self.assertEqual(retention_days({"COMMUNITY_ASSET_RETENTION_DAYS": "abc"}),
                         DEFAULT_RETENTION_DAYS)

    def test_sweep_disabled_unless_explicitly_enabled(self):
        self.assertFalse(sweep_enabled({}))
        self.assertFalse(sweep_enabled({"COMMUNITY_ASSET_RETENTION_SWEEP_ENABLED": "true"}))
        self.assertTrue(sweep_enabled({"COMMUNITY_ASSET_RETENTION_SWEEP_ENABLED": "1"}))


class RetentionTests(Base):
    def test_unlink_retains_instead_of_deleting(self):
        row = self.retain()
        self.assertEqual(row["state"], "retained")
        self.assertEqual(row["reason"], "unlinked")
        self.assertEqual(self.provider.trashed, [])   # nothing touched the Drive
        self.assertEqual(self.provider.deleted, [])

    def test_retention_is_idempotent(self):
        row = self.retain()
        again = self.svc.retain_unlinked_asset("c1", "pub-1", OWNER)
        self.assertEqual(again, row["id"])

    def test_dto_never_leaks_storage_identifiers(self):
        self.retain()
        dto = self.svc.get_retention_status("c1", OWNER)
        blob = repr(dto).lower()
        for bad in ("drive", "storage", "pub-1", "path", "checksum", "url", "http"):
            self.assertNotIn(bad, blob, bad)
        self.assertTrue(dto["restorable"])
        self.assertEqual(dto["days_remaining"], DEFAULT_RETENTION_DAYS)

    def test_status_is_scoped_to_owner(self):
        self.retain()
        self.assertEqual(self.svc.get_retention_status("c1", "user-B")["state"], "none")
        self.assertEqual(self.svc.list_owner_retained_assets("user-B")["items"], [])


class RestoreTests(Base):
    def test_owner_restores_within_window(self):
        self.retain()
        self.clock.advance_days(5)
        self.assertEqual(self.svc.restore_asset("c1", OWNER)["restored"], True)
        self.assertEqual(self.assets.get_asset_for_read("c1"), "drive-1")
        self.assertEqual(self.svc.get_retention_status("c1", OWNER)["state"], "none")

    def test_other_user_cannot_restore(self):
        self.retain()
        with self.assertRaises(AssetNotFound):
            self.svc.restore_asset("c1", "user-B")

    def test_restore_after_expiry_is_refused(self):
        self.retain()
        self.clock.advance_days(DEFAULT_RETENTION_DAYS + 1)
        with self.assertRaises(RetentionError):
            self.svc.restore_asset("c1", OWNER)

    def test_restore_conflicts_with_a_new_active_asset(self):
        self.retain()
        self.link("c1", "pub-2", "drive-2")          # owner published a replacement
        with self.assertRaises(RetentionConflict):
            self.svc.restore_asset("c1", OWNER)
        self.assertEqual(self.assets.get_asset_for_read("c1"), "drive-2")  # untouched

    def test_restore_after_trash_is_refused_not_faked(self):
        row = self.retain()
        self.clock.advance_days(DEFAULT_RETENTION_DAYS + 1)
        self.svc.move_to_trash(row)
        self.clock.advance_days(-DEFAULT_RETENTION_DAYS)   # even inside a window
        with self.assertRaises(RetentionError):
            self.svc.restore_asset("c1", OWNER)


class TrashTests(Base):
    def test_never_trashed_within_retention(self):
        row = self.retain()
        ok, why = self.svc.evaluate_for_trash(row)
        self.assertFalse(ok)
        self.assertEqual(why, "within_retention")
        self.assertEqual(self.svc.move_to_trash(row), "blocked")
        self.assertEqual(self.provider.trashed, [])

    def test_never_trashed_while_still_referenced(self):
        row = self.retain()
        self.assets.link_asset("c1", "pub-1", OWNER)      # re-linked elsewhere
        self.clock.advance_days(DEFAULT_RETENTION_DAYS + 1)
        ok, why = self.svc.evaluate_for_trash(row)
        self.assertFalse(ok)
        self.assertEqual(why, "still_referenced")
        self.assertEqual(self.provider.trashed, [])

    def test_trash_after_expiry_moves_to_trash_only(self):
        row = self.retain()
        self.clock.advance_days(DEFAULT_RETENTION_DAYS + 1)
        self.assertEqual(self.svc.move_to_trash(row), "trashed")
        self.assertEqual(self.provider.trashed, ["drive-1"])
        self.assertEqual(self.provider.deleted, [])      # NEVER a permanent delete
        self.assertEqual(self.assets.get_retention(row["id"])["state"], "trashed")

    def test_provider_failure_is_recorded_sanitized_and_retryable(self):
        row = self.retain()
        self.clock.advance_days(DEFAULT_RETENTION_DAYS + 1)
        self.provider.fail = True
        self.assertEqual(self.svc.move_to_trash(row), "failed")
        after = self.assets.get_retention(row["id"])
        self.assertEqual(after["state"], "failed")
        self.assertEqual(after["last_error_code"], "RuntimeError")   # no message, no ids
        self.assertNotIn("drive-1", repr(after["last_error_code"]))
        self.provider.fail = False
        self.assertEqual(self.svc.retry_pending()["trashed"], 1)

    def test_stale_writer_loses_optimistic_lock(self):
        row = self.retain()
        self.assertTrue(self.assets.transition_retention(
            row["id"], to_state="reconcile_required", expected_version=row["version"]))
        self.assertFalse(self.assets.transition_retention(
            row["id"], to_state="pending_trash", expected_version=row["version"]))

    def test_transitions_are_fail_closed(self):
        row = self.retain()
        self.assertFalse(self.assets.transition_retention(
            row["id"], to_state="ignored", expected_version=row["version"]))

    def test_audit_trail_recorded(self):
        row = self.retain()
        self.clock.advance_days(DEFAULT_RETENTION_DAYS + 1)
        self.svc.move_to_trash(row)
        actions = [a["action"] for a in self.assets.list_audit(row["id"])]
        self.assertIn("transition:trashed", actions)


class SweepTests(Base):
    def test_dry_run_changes_nothing(self):
        self.retain()
        self.clock.advance_days(DEFAULT_RETENTION_DAYS + 1)
        out = SocialAssetRetentionSweep(self.assets, self.svc, clock=self.clock).run()
        self.assertEqual(out["mode"], "dry-run")
        self.assertEqual(out["counts"]["safe"], 1)
        self.assertEqual(out["counts"]["changed"], 0)
        self.assertEqual(self.provider.trashed, [])

    def test_apply_trashes_only_due_and_unreferenced(self):
        self.retain("c1", "pub-1", "drive-1")
        self.retain("c2", "pub-2", "drive-2")
        self.clock.advance_days(DEFAULT_RETENTION_DAYS + 1)
        self.retain("c3", "pub-3", "drive-3")            # fresh, not due
        out = SocialAssetRetentionSweep(self.assets, self.svc, clock=self.clock).run(apply=True)
        self.assertEqual(sorted(self.provider.trashed), ["drive-1", "drive-2"])
        self.assertEqual(out["counts"]["changed"], 2)
        self.assertEqual(self.provider.deleted, [])


class ReconcileTests(Base):
    def svc_reconcile(self):
        return SocialAssetReconciliationService(
            self.assets, provider_factory=lambda: self.provider, clock=self.clock)

    def test_expired_unreferenced_is_flagged_safe_for_trash(self):
        self.retain()
        self.clock.advance_days(DEFAULT_RETENTION_DAYS + 1)
        out = self.svc_reconcile().run()
        self.assertEqual(out["findings"][0]["category"], SAFE_FOR_TRASH)
        self.assertEqual(out["counts"]["changed"], 0)     # dry-run

    def test_missing_remote_file_requires_human_review(self):
        row = self.retain()
        self.store.files["pub-1"]["storage_file_id"] = ""   # publication lost its file
        out = self.svc_reconcile().run()
        self.assertEqual(out["findings"][0]["category"], RECONCILE_REQUIRED)
        self.assertEqual(self.assets.get_retention(row["id"])["state"], "retained")

    def test_apply_only_fixes_unambiguous_lag(self):
        row = self.retain()
        self.clock.advance_days(DEFAULT_RETENTION_DAYS + 1)
        self.assets.transition_retention(row["id"], to_state="pending_trash",
                                         expected_version=row["version"])
        self.provider.trashed.append("drive-1")            # Drive already confirms it
        out = self.svc_reconcile().run(apply=True)
        self.assertEqual(out["counts"]["changed"], 1)
        self.assertEqual(self.assets.get_retention(row["id"])["state"], "trashed")
        self.assertEqual(self.provider.deleted, [])

    def test_reconcile_run_is_recorded(self):
        self.retain()
        out = self.svc_reconcile().run()
        self.assertTrue(out["run_id"])


class SafetyTests(Base):
    def test_no_permanent_delete_anywhere_in_the_retention_code(self):
        root = Path(__file__).resolve().parent
        for name in ("social_asset_retention.py", "social_asset_reconcile.py",
                     "social_asset_maintenance_cli.py"):
            src = (root / name).read_text(encoding="utf-8")
            for bad in ("delete_file", "empty_trash", "emptyTrash", "files.delete"):
                self.assertNotIn(bad, src, f"{name}: {bad}")

    def test_no_broad_drive_listing(self):
        root = Path(__file__).resolve().parent
        for name in ("social_asset_retention.py", "social_asset_reconcile.py"):
            src = (root / name).read_text(encoding="utf-8")
            for bad in ("list_files", "files.list", "q=", "'*'"):
                self.assertNotIn(bad, src, f"{name}: {bad}")


if __name__ == "__main__":
    unittest.main()
