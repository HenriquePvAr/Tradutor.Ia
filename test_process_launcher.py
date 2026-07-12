import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from process_launcher import (
    LAUNCH_FAILURE_EXIT_CODE,
    atomic_write_text,
    read_exit_code,
    run_process,
)


class ProcessLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="tradutor launcher tests ",
        )
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def _runtime(self, name):
        return self.root / name

    def _python_command(self, code):
        return [sys.executable, "-c", code]

    def test_exit_zero_is_persisted_after_completion(self):
        runtime = self._runtime("exit zero")

        returncode = run_process(
            self._python_command("raise SystemExit(0)"),
            runtime,
        )

        self.assertEqual(returncode, 0)
        self.assertEqual(read_exit_code(runtime / "exit_code.txt"), 0)
        self.assertEqual((runtime / "exit_code.txt").read_text().strip(), "0")
        self.assertGreater((runtime / "exit_code.txt").stat().st_size, 0)

    def test_nonzero_exit_code_is_preserved(self):
        runtime = self._runtime("exit seven")

        returncode = run_process(
            self._python_command("raise SystemExit(7)"),
            runtime,
        )

        self.assertEqual(returncode, 7)
        self.assertEqual(read_exit_code(runtime / "exit_code.txt"), 7)

    def test_stdout_and_stderr_do_not_block_exit_persistence(self):
        runtime = self._runtime("stdout stderr")
        code = (
            "import sys; "
            "sys.stdout.write('OUT-' + 'x' * 200000); "
            "sys.stderr.write('ERR-' + 'y' * 100000); "
            "raise SystemExit(3)"
        )

        returncode = run_process(self._python_command(code), runtime)

        self.assertEqual(returncode, 3)
        self.assertEqual(read_exit_code(runtime / "exit_code.txt"), 3)
        self.assertTrue((runtime / "stdout.log").read_text().startswith("OUT-"))
        self.assertTrue((runtime / "stderr.log").read_text().startswith("ERR-"))

    def test_custom_paths_with_spaces_are_supported(self):
        runtime = self._runtime("runtime with spaces")
        logs = self.root / "logs with spaces"
        stdout = logs / "child stdout.log"
        stderr = logs / "child stderr.log"

        returncode = run_process(
            self._python_command("print('space-safe')"),
            runtime,
            cwd=self.root,
            stdout_path=stdout,
            stderr_path=stderr,
        )

        self.assertEqual(returncode, 0)
        self.assertEqual(stdout.read_text().strip(), "space-safe")
        self.assertEqual(stderr.read_text(), "")
        self.assertEqual(read_exit_code(runtime / "exit_code.txt"), 0)

    def test_launcher_waits_for_child_before_writing_exit_code(self):
        runtime = self._runtime("waits for child")
        marker = self.root / "child finished.txt"
        code = (
            "import pathlib, time; "
            "time.sleep(0.25); "
            f"pathlib.Path({str(marker)!r}).write_text('done')"
        )
        started = time.monotonic()

        returncode = run_process(self._python_command(code), runtime)

        self.assertGreaterEqual(time.monotonic() - started, 0.20)
        self.assertEqual(returncode, 0)
        self.assertEqual(marker.read_text(), "done")
        self.assertEqual(read_exit_code(runtime / "exit_code.txt"), 0)

    def test_running_process_has_no_completion_code(self):
        runtime = self._runtime("running state")
        runtime.mkdir(parents=True)
        atomic_write_text(runtime / "exit_code.txt", "99\n")
        result = {}

        thread = threading.Thread(
            target=lambda: result.setdefault(
                "returncode",
                run_process(
                    self._python_command("import time; time.sleep(0.4)"),
                    runtime,
                ),
            ),
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 3
        while not (runtime / "child_pid.txt").exists() and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertTrue((runtime / "child_pid.txt").exists())
        self.assertFalse(runtime.joinpath("exit_code.txt").exists())
        self.assertIsNone(read_exit_code(runtime / "exit_code.txt"))

        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(read_exit_code(runtime / "exit_code.txt"), 0)

    def test_review_required_status_does_not_change_process_exit_code(self):
        runtime = self._runtime("review required")
        progress = self.root / "progress.json"
        code = (
            "import pathlib; "
            f"pathlib.Path({str(progress)!r}).write_text"
            "('{\"status\": \"review_required\", \"quality_gate\": false}'); "
            "raise SystemExit(0)"
        )

        returncode = run_process(self._python_command(code), runtime)

        self.assertEqual(json.loads(progress.read_text())["status"], "review_required")
        self.assertEqual(returncode, 0)
        self.assertEqual(read_exit_code(runtime / "exit_code.txt"), 0)

    def test_launch_failure_is_persisted_and_observable(self):
        runtime = self._runtime("launch failure")
        missing = self.root / "missing executable.exe"

        returncode = run_process([str(missing)], runtime)

        self.assertEqual(returncode, LAUNCH_FAILURE_EXIT_CODE)
        self.assertEqual(
            read_exit_code(runtime / "exit_code.txt"),
            LAUNCH_FAILURE_EXIT_CODE,
        )
        self.assertFalse((runtime / "child_pid.txt").exists())
        self.assertTrue((runtime / "launcher_error.txt").read_text().strip())
        events = [
            json.loads(line)
            for line in (runtime / "launcher_events.jsonl").read_text().splitlines()
        ]
        self.assertIn("launch_failed", [event["event"] for event in events])

    def test_empty_and_missing_legacy_exit_files_are_unknown(self):
        missing = self.root / "missing.txt"
        empty = self.root / "empty.txt"
        empty.write_bytes(b"")

        self.assertIsNone(read_exit_code(missing))
        self.assertIsNone(read_exit_code(empty))

    def test_atomic_rewrite_never_leaves_partial_exit_code(self):
        runtime = self._runtime("atomic rewrite")
        target = runtime / "exit_code.txt"

        atomic_write_text(target, "0\n")
        atomic_write_text(target, "7\n")

        self.assertEqual(target.read_text(), "7\n")
        self.assertEqual(read_exit_code(target), 7)
        self.assertEqual(list(runtime.glob("*.tmp")), [])

    def test_completion_events_include_pid_code_and_write_success(self):
        runtime = self._runtime("events")

        returncode = run_process(
            self._python_command("raise SystemExit(5)"),
            runtime,
        )

        events = [
            json.loads(line)
            for line in (runtime / "launcher_events.jsonl").read_text().splitlines()
        ]
        by_name = {event["event"]: event for event in events}
        self.assertEqual(returncode, 5)
        self.assertGreater(by_name["child_started"]["child_pid"], 0)
        self.assertEqual(by_name["child_exited"]["exit_code"], 5)
        self.assertTrue(by_name["exit_code_persisted"]["success"])
        self.assertNotIn("command", json.dumps(events).lower())

    def test_exit_code_write_failure_is_not_hidden(self):
        runtime = self._runtime("write failure")
        real_atomic_write = atomic_write_text

        def fail_exit_code(path, value):
            if Path(path).name == "exit_code.txt":
                raise OSError("controlled exit-code write failure")
            return real_atomic_write(path, value)

        launcher_stderr = io.StringIO()
        with patch("process_launcher.atomic_write_text", side_effect=fail_exit_code):
            with patch("sys.stderr", launcher_stderr):
                with self.assertRaisesRegex(
                    OSError,
                    "controlled exit-code write failure",
                ):
                    run_process(
                        self._python_command("raise SystemExit(0)"),
                        runtime,
                    )

        events = [
            json.loads(line)
            for line in (runtime / "launcher_events.jsonl").read_text().splitlines()
        ]
        by_name = {event["event"]: event for event in events}
        self.assertEqual(by_name["child_exited"]["exit_code"], 0)
        self.assertFalse(by_name["exit_code_persist_failed"]["success"])
        self.assertIn("Unable to persist child exit code 0", launcher_stderr.getvalue())
        self.assertFalse((runtime / "exit_code.txt").exists())


if __name__ == "__main__":
    unittest.main()
