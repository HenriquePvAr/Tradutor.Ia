"""Launcher dispatch and the single-worker guard, without spawning real processes."""

import _test_bootstrap  # noqa: F401

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import start_tradutor
from job_store import JobStore


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "jobs.sqlite3"
        self._p = patch.object(start_tradutor, "DB_PATH", self.db)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_start_worker_skips_when_one_is_healthy(self):
        store = JobStore(self.db)
        store.register_worker("existing", 4321)
        store.close()
        with patch.object(start_tradutor.subprocess, "Popen") as popen:
            started = start_tradutor.start_worker()
        self.assertFalse(started)
        popen.assert_not_called()

    def test_start_worker_spawns_when_none(self):
        with patch.object(start_tradutor.subprocess, "Popen") as popen, \
                patch.object(start_tradutor.time, "sleep", lambda *_: None):
            started = start_tradutor.start_worker()
        self.assertTrue(started)
        popen.assert_called_once()
        argv = popen.call_args.args[0]
        self.assertIn("worker_service.py", " ".join(argv))
        self.assertEqual(popen.call_args.kwargs["env"]["PYTHONUNBUFFERED"], "1")

    def test_start_worker_hides_the_detached_console_on_windows(self):
        if start_tradutor.os.name != "nt":
            self.skipTest("Windows-only creation flag")
        with patch.object(start_tradutor.subprocess, "Popen") as popen, \
                patch.object(start_tradutor.time, "sleep", lambda *_: None):
            self.assertTrue(start_tradutor.start_worker())
        flags = popen.call_args.kwargs["creationflags"]
        self.assertTrue(flags & start_tradutor.subprocess.CREATE_NO_WINDOW)
        self.assertTrue(flags & start_tradutor.subprocess.DETACHED_PROCESS)

    def test_main_dispatches_status(self):
        with patch.object(start_tradutor, "print_status") as status:
            code = start_tradutor.main(["status"])
        self.assertEqual(code, 0)
        status.assert_called_once()

    def test_main_unknown_command(self):
        self.assertEqual(start_tradutor.main(["bogus"]), 2)


if __name__ == "__main__":
    unittest.main()
