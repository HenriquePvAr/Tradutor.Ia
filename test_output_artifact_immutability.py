import _test_bootstrap  # noqa: F401
from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import tempfile
import unittest
from pathlib import Path

from benchmark_pipeline import ImmutableArtifactConflict, _finalize_immutable_file


class OutputArtifactImmutabilityTests(unittest.TestCase):
    def test_new_terminal_artifact_is_created_from_temp_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            temp = root / ".chapter.tmp"
            final = root / "chapter.pdf"
            temp.write_bytes(b"pdf-bytes")

            result = _finalize_immutable_file(temp, final)

            self.assertEqual(result["status"], "created")
            self.assertEqual(final.read_bytes(), b"pdf-bytes")
            self.assertFalse(temp.exists())

    def test_same_run_same_bytes_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            final = root / "chapter.pdf"
            temp = root / ".chapter.tmp"
            final.write_bytes(b"same-pdf")
            before = final.read_bytes()
            temp.write_bytes(b"same-pdf")

            result = _finalize_immutable_file(temp, final)

            self.assertEqual(result["status"], "idempotent")
            self.assertEqual(final.read_bytes(), before)
            self.assertFalse(temp.exists())

    def test_same_run_different_bytes_fails_closed_without_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            final = root / "chapter.pdf"
            temp = root / ".chapter.tmp"
            final.write_bytes(b"original-pdf")
            temp.write_bytes(b"different-pdf")

            with self.assertRaisesRegex(
                ImmutableArtifactConflict,
                "artifact_already_exists_with_different_bytes",
            ):
                _finalize_immutable_file(temp, final)

            self.assertEqual(final.read_bytes(), b"original-pdf")
            self.assertEqual(temp.read_bytes(), b"different-pdf")

    def test_temp_artifact_must_stay_inside_run_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = root / "run"
            outside = root / ".outside.tmp"
            run.mkdir()
            outside.write_bytes(b"pdf")

            with self.assertRaisesRegex(
                ImmutableArtifactConflict,
                "temporary_artifact_outside_run_folder",
            ):
                _finalize_immutable_file(outside, run / "chapter.pdf")

            self.assertTrue(outside.exists())


if __name__ == "__main__":
    unittest.main()
