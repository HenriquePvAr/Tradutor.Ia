"""Bounded rotation for the ``*_current.jsonl`` diagnostic streams.

Before the bound, ``_append_diagnostic_log`` appended forever; one beta-17 session left
app_current.jsonl at ~297 MB.  Rotation keeps a bounded current file plus one
``.previous`` copy, and a rotation failure must never break a diagnostic write.
"""
import pathlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import app_ui


class DiagnosticLogRotationTests(unittest.TestCase):
    def setUp(self):
        self._old_root = app_ui.DIAGNOSTICS_ROOT
        self._old_max = app_ui._DIAGNOSTIC_LOG_MAX_BYTES

    def tearDown(self):
        app_ui.DIAGNOSTICS_ROOT = self._old_root
        app_ui._DIAGNOSTIC_LOG_MAX_BYTES = self._old_max

    def _use(self, tmp, max_bytes):
        app_ui.DIAGNOSTICS_ROOT = Path(tmp)
        app_ui._DIAGNOSTIC_LOG_MAX_BYTES = max_bytes

    def test_below_limit_appends_without_rotation(self):
        with TemporaryDirectory() as tmp:
            self._use(tmp, 10_000_000)
            for _ in range(5):
                app_ui._append_diagnostic_log("app_current.jsonl", "E", k=1)
            current = Path(tmp) / "app_current.jsonl"
            self.assertTrue(current.exists())
            self.assertEqual(list(Path(tmp).glob("*.previous.jsonl")), [])
            self.assertEqual(sum(1 for _ in current.open(encoding="utf-8")), 5)

    def test_above_limit_rotates_and_keeps_writing(self):
        with TemporaryDirectory() as tmp:
            self._use(tmp, 200)  # tiny ceiling forces rotation
            for i in range(60):
                app_ui._append_diagnostic_log("app_current.jsonl", "E", i=i, pad="x" * 24)
            previous = list(Path(tmp).glob("app_current.previous.jsonl"))
            current = Path(tmp) / "app_current.jsonl"
            self.assertEqual(len(previous), 1)
            self.assertTrue(current.exists() and current.stat().st_size > 0)
            # Current stays bounded near the limit (never the 297 MB runaway).
            self.assertLess(current.stat().st_size, 200 + 1024)

    def test_rotation_failure_never_breaks_the_write(self):
        with TemporaryDirectory() as tmp:
            self._use(tmp, 1)
            app_ui._append_diagnostic_log("app_current.jsonl", "E", k=0)  # create the file
            with mock.patch.object(pathlib.Path, "replace", side_effect=OSError("locked")):
                app_ui._append_diagnostic_log("app_current.jsonl", "E", k=1)  # must not raise
            self.assertTrue((Path(tmp) / "app_current.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
