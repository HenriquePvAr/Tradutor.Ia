"""Hermetic integration tests for the snapshot-to-pipeline local input bridge."""

import _test_bootstrap  # noqa: F401

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import local_folder_input
from local_folder_input import materialize_snapshot
from local_folder_source import LocalFolderChapterAdapter, LocalFolderError, LocalFolderPolicy


def image_bytes(color):
    buffer = io.BytesIO()
    Image.new("RGB", (800, 1200), color).save(buffer, "PNG")
    return buffer.getvalue()


class LocalFolderInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.allowed = self.root / "allowed"
        self.chapter = self.allowed / "chapter"
        self.snapshots = self.root / "snapshots"
        self.output_root = self.root / "output"
        self.chapter.mkdir(parents=True)
        self.snapshots.mkdir()
        self.output_root.mkdir()
        (self.chapter / "page_1.png").write_bytes(image_bytes((10, 20, 30)))
        (self.chapter / "page_2.png").write_bytes(image_bytes((40, 50, 60)))
        self.adapter = LocalFolderChapterAdapter(LocalFolderPolicy([self.allowed]))

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self):
        return self.adapter.snapshot(self.chapter, self.snapshots, snapshot_id="chapter_one")

    def test_materializes_generated_pages_and_normal_pipeline_report_without_source_path(self):
        snap = self.snapshot()
        target = self.output_root / "chapter" / "input"
        paths, report = materialize_snapshot(
            snap.manifest_path, target, snapshot_root=self.snapshots, output_root=self.output_root)

        self.assertEqual([Path(path).name for path in paths], ["001.png", "002.png"])
        self.assertTrue(report["logical_pages"])
        self.assertFalse(report["requires_smart_split"])
        self.assertEqual(report["source_type"], "local_folder")
        self.assertEqual(report["transport_name"], "local_snapshot")
        self.assertTrue(report["download_gate"]["passed"])
        self.assertTrue(all(item["is_chapter_candidate"] for item in report["downloaded"]))
        serialised = json.dumps(report)
        self.assertNotIn(str(self.chapter), serialised)
        self.assertNotIn("page_1.png", serialised)
        self.assertNotIn(str(snap.workspace), serialised)

    def test_manifest_outside_owned_workspace_and_tampering_fail_closed(self):
        snap = self.snapshot()
        target = self.output_root / "chapter" / "input"
        outside = self.root / "outside.json"
        outside.write_text(snap.manifest_path.read_text(encoding="utf-8"), encoding="utf-8")
        with self.assertRaises(LocalFolderError) as raised:
            materialize_snapshot(outside, target, snapshot_root=self.snapshots, output_root=self.output_root)
        self.assertEqual(raised.exception.detail, "manifest_outside_workspace")

        snapshot_page = snap.workspace / "0001.png"
        snapshot_page.write_bytes(b"tampered")
        with self.assertRaises(LocalFolderError) as raised:
            materialize_snapshot(
                snap.manifest_path, target, snapshot_root=self.snapshots, output_root=self.output_root)
        self.assertEqual(raised.exception.detail, "snapshot_size_mismatch")

    def test_resumed_materialization_enforces_the_aggregate_byte_budget_before_copying(self):
        snap = self.snapshot()
        first = (snap.workspace / "0001.png").stat().st_size
        second = (snap.workspace / "0002.png").stat().st_size
        target = self.output_root / "chapter" / "input"
        with mock.patch.object(local_folder_input, "HARD_MAX_TOTAL_BYTES", first + second - 1):
            with self.assertRaises(LocalFolderError) as raised:
                materialize_snapshot(
                    snap.manifest_path, target, snapshot_root=self.snapshots,
                    output_root=self.output_root)
        self.assertEqual(raised.exception.detail, "max_total_bytes")
        self.assertFalse((target / "002.png").exists())

    def test_existing_output_is_never_deleted_without_an_owned_marker_and_explicit_clear(self):
        snap = self.snapshot()
        target = self.output_root / "chapter" / "input"
        target.mkdir(parents=True)
        sentinel = target / "keep.txt"
        sentinel.write_text("do not delete", encoding="utf-8")
        with self.assertRaises(LocalFolderError) as raised:
            materialize_snapshot(
                snap.manifest_path, target, snapshot_root=self.snapshots, output_root=self.output_root)
        self.assertEqual(raised.exception.detail, "output_target_not_owned")
        self.assertTrue(sentinel.exists())

        sentinel.unlink()
        materialize_snapshot(snap.manifest_path, target, snapshot_root=self.snapshots, output_root=self.output_root)
        paths, _ = materialize_snapshot(
            snap.manifest_path, target, snapshot_root=self.snapshots, output_root=self.output_root,
            clear_existing=True)
        self.assertEqual(len(paths), 2)

    def test_output_must_stay_under_the_explicit_output_root(self):
        snap = self.snapshot()
        with self.assertRaises(LocalFolderError) as raised:
            materialize_snapshot(
                snap.manifest_path, self.root / "elsewhere", snapshot_root=self.snapshots,
                output_root=self.output_root)
        self.assertEqual(raised.exception.detail, "output_outside_root")


if __name__ == "__main__":
    unittest.main()
