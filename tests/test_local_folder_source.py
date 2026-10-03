"""Hermetic tests for the isolated LocalFolderChapterAdapter intake boundary."""

import _test_bootstrap  # noqa: F401

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import local_folder_source as local_source
from local_folder_source import (
    HARD_MAX_BYTES_PER_FILE,
    LOCAL_FOLDER_ROOT_NOT_SELECTABLE,
    LOCAL_INPUT_DUPLICATE,
    LOCAL_INPUT_LIMIT,
    LOCAL_INPUT_NOT_CONFIGURED,
    LOCAL_INVALID_IMAGE,
    LOCAL_PATH_NOT_ALLOWED,
    LOCAL_PATH_TRAVERSAL,
    LOCAL_PATH_UNSUPPORTED,
    LOCAL_REPARSE_POINT,
    LOCAL_UNSUPPORTED_EXTENSION,
    LOCAL_WORKSPACE_OVERLAP,
    LocalFolderChapterAdapter,
    LocalFolderError,
    LocalFolderLimits,
    LocalFolderPolicy,
)


def image_bytes(fmt: str = "PNG", *, color=(24, 48, 96), size=(800, 1200)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format=fmt)
    return buffer.getvalue()


class LocalFolderSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.allowed = self.base / "allowed"
        self.chapter = self.allowed / "chapter"
        self.allowed.mkdir()
        self.chapter.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def adapter(self, *, limits: LocalFolderLimits | None = None) -> LocalFolderChapterAdapter:
        return LocalFolderChapterAdapter(LocalFolderPolicy([self.allowed]), limits=limits)

    def write_image(self, name: str, data: bytes | None = None) -> Path:
        path = self.chapter / name
        path.write_bytes(image_bytes() if data is None else data)
        return path

    def assert_code(self, code: str, callback) -> None:
        with self.assertRaises(LocalFolderError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)

    def test_default_root_is_not_created_when_missing(self) -> None:
        missing = self.base / "missing_default_input"
        original_getenv = local_source.os.getenv

        def getenv_without_local_roots(name, default=None):
            return None if name == "LOCAL_INPUT_ROOTS" else original_getenv(name, default)

        with (
            mock.patch.object(local_source, "DEFAULT_ALLOWED_ROOT", missing),
            mock.patch.object(local_source.os, "getenv", side_effect=getenv_without_local_roots),
        ):
            policy = LocalFolderPolicy()
        self.assertEqual(policy.allowed_roots, ())
        self.assert_code(LOCAL_INPUT_NOT_CONFIGURED, lambda: policy.validate_folder(self.chapter))
        self.assertFalse(missing.exists())

    def test_only_a_direct_folder_inside_an_allowed_root_is_accepted(self) -> None:
        self.write_image("page_10.png", image_bytes(color=(10, 10, 10)))
        self.write_image("page_2.png", image_bytes(color=(20, 20, 20)))
        self.write_image("page_1.png", image_bytes(color=(30, 30, 30)))
        self.write_image("page_3.png", image_bytes(color=(40, 40, 40)))
        (self.chapter / "notes.txt").write_text("not a page", encoding="utf-8")
        nested = self.chapter / "nested"
        nested.mkdir()
        Image.new("RGB", (800, 1200), "red").save(nested / "page_0.png")

        analysis = self.adapter().analyze(self.chapter)

        self.assertEqual(
            [page.source_name for page in analysis.pages],
            ["page_1.png", "page_2.png", "page_3.png", "page_10.png"],
        )
        self.assertEqual([page.order for page in analysis.pages], [1, 2, 3, 4])
        self.assertEqual(analysis.total_bytes, sum(page.byte_size for page in analysis.pages))
        self.assertNotIn(str(self.chapter), json.dumps(analysis.public()))

    def test_root_itself_outside_root_and_parent_traversal_fail_closed(self) -> None:
        self.write_image("page_1.png")
        outside = self.base / "outside"
        outside.mkdir()
        self.assert_code(LOCAL_FOLDER_ROOT_NOT_SELECTABLE, lambda: self.adapter().analyze(self.allowed))
        self.assert_code(LOCAL_PATH_NOT_ALLOWED, lambda: self.adapter().analyze(outside))
        self.assert_code(
            LOCAL_PATH_TRAVERSAL,
            lambda: self.adapter().analyze(self.chapter / ".." / "chapter"),
        )

    def test_unc_device_file_scheme_and_relative_paths_are_rejected_before_access(self) -> None:
        adapter = self.adapter()
        self.assert_code(LOCAL_PATH_UNSUPPORTED, lambda: adapter.analyze(r"\\server\share\chapter"))
        self.assert_code(LOCAL_PATH_UNSUPPORTED, lambda: adapter.analyze(r"\\?\C:\chapter"))
        self.assert_code(LOCAL_PATH_UNSUPPORTED, lambda: adapter.analyze("file:///C:/chapter"))
        self.assert_code(LOCAL_PATH_UNSUPPORTED, lambda: adapter.analyze("chapter"))
        self.assertTrue(adapter.supports(r"C:\chapter"))
        self.assertFalse(adapter.supports("https://reader.example/chapter"))

    def test_allowed_extensions_must_match_validated_byte_format(self) -> None:
        self.write_image("renamed.jpg", image_bytes("PNG"))
        self.assert_code(LOCAL_INVALID_IMAGE, lambda: self.adapter().analyze(self.chapter))

    def test_known_but_disallowed_image_extension_is_not_silently_skipped(self) -> None:
        self.write_image("page.gif", image_bytes("PNG"))
        self.assert_code(LOCAL_UNSUPPORTED_EXTENSION, lambda: self.adapter().analyze(self.chapter))

    def test_opencv_decode_is_required_after_pillow_and_signature_validation(self) -> None:
        self.write_image("page_1.png")
        with mock.patch.object(local_source.cv2, "imdecode", return_value=None):
            self.assert_code(LOCAL_INVALID_IMAGE, lambda: self.adapter().analyze(self.chapter))

    def test_duplicate_bytes_fail_the_complete_input_gate(self) -> None:
        payload = image_bytes()
        self.write_image("page_1.png", payload)
        self.write_image("page_2.png", payload)
        self.assert_code(LOCAL_INPUT_DUPLICATE, lambda: self.adapter().analyze(self.chapter))

    def test_file_count_and_byte_budgets_are_enforced(self) -> None:
        self.write_image("page_1.png")
        self.write_image("page_2.png", image_bytes(color=(1, 2, 3)))
        self.assert_code(
            LOCAL_INPUT_LIMIT,
            lambda: self.adapter(limits=LocalFolderLimits(max_files=1)).analyze(self.chapter),
        )

        payload = image_bytes()
        (self.chapter / "page_2.png").unlink()
        (self.chapter / "page_1.png").write_bytes(payload)
        self.assertGreater(len(payload), 64)
        self.assert_code(
            LOCAL_INPUT_LIMIT,
            lambda: self.adapter(
                limits=LocalFolderLimits(
                    max_bytes_per_file=len(payload) - 1,
                    max_total_bytes=len(payload) - 1,
                    max_files=1,
                )
            ).analyze(self.chapter),
        )
        self.assertLess(len(payload), HARD_MAX_BYTES_PER_FILE)

    def test_total_chapter_budget_is_enforced_while_reading(self) -> None:
        first = image_bytes(color=(10, 20, 30))
        second = image_bytes(color=(40, 50, 60))
        self.write_image("page_1.png", first)
        self.write_image("page_2.png", second)
        self.assert_code(
            LOCAL_INPUT_LIMIT,
            lambda: self.adapter(
                limits=LocalFolderLimits(
                    max_bytes_per_file=max(len(first), len(second)),
                    max_total_bytes=len(first) + len(second) - 1,
                    max_files=2,
                )
            ).analyze(self.chapter),
        )

    def test_directory_enumeration_is_bounded_before_an_untrusted_folder_walks_forever(self) -> None:
        self.write_image("page_1.png", image_bytes(color=(10, 20, 30)))
        self.write_image("page_2.png", image_bytes(color=(40, 50, 60)))
        with mock.patch.object(local_source, "HARD_MAX_DIRECTORY_ENTRIES", 1):
            self.assert_code(LOCAL_INPUT_LIMIT, lambda: self.adapter().analyze(self.chapter))

    def test_direct_reparse_or_symlink_entry_is_rejected(self) -> None:
        page = self.write_image("linked_page.png")
        original = local_source._is_reparse_point

        def is_reparse(path):
            return Path(path).name == page.name or original(Path(path))

        with mock.patch.object(local_source, "_is_reparse_point", side_effect=is_reparse):
            self.assert_code(LOCAL_REPARSE_POINT, lambda: self.adapter().analyze(self.chapter))

    def test_snapshot_uses_generated_names_preserves_source_and_never_exposes_paths(self) -> None:
        source = self.write_image("private_original_name_page_1.png")
        original = source.read_bytes()
        workspace_root = self.base / "workspace"
        workspace_root.mkdir()

        snapshot = self.adapter().snapshot(
            self.chapter,
            workspace_root,
            snapshot_id="chapter_snapshot_1",
        )

        self.assertEqual(source.read_bytes(), original)
        snapshot_page = snapshot.workspace / "0001.png"
        self.assertTrue(snapshot_page.is_file())
        self.assertEqual(snapshot_page.read_bytes(), original)
        source.write_bytes(image_bytes(color=(99, 1, 4)))
        self.assertEqual(snapshot_page.read_bytes(), original)

        payload = json.loads(snapshot.manifest_path.read_text(encoding="utf-8"))
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(str(self.chapter), serialized)
        self.assertNotIn(source.name, serialized)
        self.assertTrue(payload["logical_pages"])
        self.assertFalse(payload["requires_smart_split"])
        self.assertEqual(payload["accepted_page_count"], 1)
        self.assertEqual(payload["pages"][0]["filename"], "0001.png")
        self.assertTrue(payload["download_gate"]["passed"])

    def test_workspace_may_not_overlap_the_original_folder(self) -> None:
        self.write_image("page_1.png")
        self.assert_code(
            LOCAL_WORKSPACE_OVERLAP,
            lambda: self.adapter().snapshot(self.chapter, self.chapter),
        )

    def test_jpeg_extension_and_format_are_accepted(self) -> None:
        self.write_image("page_1.jpeg", image_bytes("JPEG"))
        analysis = self.adapter().analyze(self.chapter)
        self.assertEqual(analysis.pages[0].fmt, "jpeg")
        self.assertEqual(analysis.pages[0].mime_type, "image/jpeg")


if __name__ == "__main__":
    unittest.main()
