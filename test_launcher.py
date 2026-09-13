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

    def test_background_launch_uses_pythonw_when_available(self):
        with patch.object(start_tradutor, "background_python_executable", return_value="pythonw.exe"):
            with patch.object(start_tradutor.subprocess, "Popen") as popen, \
                    patch.object(start_tradutor.time, "sleep", lambda *_: None):
                start_tradutor.start_worker()
        self.assertEqual(popen.call_args.args[0][0], "pythonw.exe")

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

    def test_child_commands_preserve_dev_interpreter_contract(self):
        worker = start_tradutor.build_child_command("worker", frozen=False)
        ui = start_tradutor.build_child_command("ui", frozen=False)
        self.assertIn("-u", worker)
        self.assertTrue(worker[-1].endswith("worker_service.py"))
        self.assertIn("-u", ui)
        self.assertTrue(ui[-1].endswith("app_ui.py"))

    def test_child_commands_use_internal_roles_when_frozen(self):
        with patch.object(start_tradutor.sys, "executable", "TradutorIA.exe"):
            worker = start_tradutor.build_child_command("worker", frozen=True)
            ui = start_tradutor.build_child_command("ui", frozen=True)
        self.assertEqual(worker, ["TradutorIA.exe", "--internal-child", "worker"])
        self.assertEqual(ui, ["TradutorIA.exe", "--internal-child", "ui"])
        self.assertNotIn("-u", worker + ui)
        self.assertFalse(any(arg.endswith(".py") for arg in worker + ui))

    def test_internal_child_dispatches_exactly_one_entrypoint(self):
        with patch.object(start_tradutor, "load_local_environment_for_entrypoint") as load, \
                patch("worker_service.main", return_value=7) as worker:
            self.assertEqual(start_tradutor.main(["--internal-child", "worker", "--once"]), 7)
        load.assert_not_called()
        worker.assert_called_once_with(["--once"])

    def test_invalid_internal_child_role_fails_without_falling_through(self):
        with patch.object(start_tradutor, "load_local_environment_for_entrypoint") as load:
            self.assertEqual(start_tradutor.main(["--internal-child", "nonsense"]), 2)
        load.assert_not_called()

    def test_internal_rapidocr_selftest_dispatches_without_launcher(self):
        with patch.object(start_tradutor, "load_local_environment_for_entrypoint") as load, \
                patch.object(start_tradutor, "internal_selftest", return_value=0) as smoke:
            self.assertEqual(start_tradutor.main(["--internal-selftest", "rapidocr"]), 0)
        load.assert_not_called()
        smoke.assert_called_once_with("rapidocr")

    def test_start_ui_uses_hidden_background_options_and_captured_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app_ui.py").write_text("", encoding="utf-8")
            with patch.object(start_tradutor, "REPO_ROOT", root), \
                    patch.object(start_tradutor.subprocess, "Popen") as popen:
                popen.return_value.wait.return_value = 0
                self.assertEqual(start_tradutor.start_ui(), 0)
            popen.assert_called_once()
            self.assertEqual(popen.call_args.args[0][0], start_tradutor.background_python_executable())
            kwargs = popen.call_args.kwargs
            self.assertEqual(kwargs["cwd"], str(root))
            self.assertIs(kwargs["stdin"], start_tradutor.subprocess.DEVNULL)
            self.assertIs(kwargs["stderr"], start_tradutor.subprocess.STDOUT)
            if start_tradutor.os.name == "nt":
                self.assertTrue(kwargs["creationflags"] & start_tradutor.subprocess.CREATE_NO_WINDOW)


if __name__ == "__main__":
    unittest.main()
