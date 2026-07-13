from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import gc
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import psutil

from process_launcher import (
    CANCELLED_EXIT_CODE,
    CLEANUP_FAILURE_EXIT_CODE,
    LAUNCH_FAILURE_EXIT_CODE,
    ProcessTreeError,
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
        self.doCleanups()
        self.temporary.cleanup()

    def _runtime(self, name):
        return self.root / name

    def _python_command(self, code):
        return [sys.executable, "-c", code]

    def _wait_for_file(self, path, timeout=5):
        deadline = time.monotonic() + timeout
        while not Path(path).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(Path(path).exists(), f"Timed out waiting for {path}")

    def _cleanup_owned_process(self, pid, marker):
        if not pid or not psutil.pid_exists(pid):
            return
        try:
            process = psutil.Process(pid)
            command = process.cmdline()
        except psutil.NoSuchProcess:
            return
        self.assertIn(marker, command, f"Refusing to terminate unowned PID {pid}")
        process.terminate()
        try:
            process.wait(timeout=5)
        except psutil.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _register_owned_process(self, pid, marker):
        self.addCleanup(self._cleanup_owned_process, pid, marker)

    def _register_owned_popen(self, process, marker):
        def cleanup():
            self._cleanup_owned_process(process.pid, marker)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

        self.addCleanup(cleanup)

    def _wait_for_owned_exit(self, pid, marker, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not psutil.pid_exists(pid):
                return True
            try:
                if marker not in psutil.Process(pid).cmdline():
                    return True
            except psutil.NoSuchProcess:
                return True
            time.sleep(0.01)
        return False

    def _run_with_keyboard_interrupt(self, command, runtime, ready_path):
        original_wait = subprocess.Popen.wait
        interrupted = False

        def interrupting_wait(process, timeout=None):
            nonlocal interrupted
            if not interrupted:
                self._wait_for_file(ready_path)
                interrupted = True
                raise KeyboardInterrupt
            return original_wait(process, timeout=timeout)

        with patch("process_launcher.subprocess.Popen.wait", new=interrupting_wait):
            return run_process(command, runtime, cwd=self.root)

    def _one_descendant_command(self, marker, pid_path, ready_path, *, exit_parent=False):
        code = (
            "import pathlib, subprocess, sys, time; "
            "grand = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(60)', sys.argv[1]]); "
            "pathlib.Path(sys.argv[2]).write_text(str(grand.pid)); "
            "pathlib.Path(sys.argv[3]).write_text('ready'); "
            + ("raise SystemExit(0)" if exit_parent else "time.sleep(60)")
        )
        return [
            sys.executable,
            "-c",
            code,
            marker,
            str(pid_path),
            str(ready_path),
        ]

    def _two_descendant_command(
        self,
        marker,
        grandchild_pid_path,
        great_grandchild_pid_path,
        ready_path,
    ):
        grandchild_script = self.root / "controlled grandchild.py"
        child_script = self.root / "controlled child.py"
        grandchild_script.write_text(
            "import pathlib, subprocess, sys, time\n"
            "great = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(60)', sys.argv[1]])\n"
            "pathlib.Path(sys.argv[2]).write_text(str(great.pid))\n"
            "pathlib.Path(sys.argv[3]).write_text('ready')\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        child_script.write_text(
            "import pathlib, subprocess, sys, time\n"
            "grand = subprocess.Popen([sys.executable, sys.argv[2], "
            "sys.argv[1], sys.argv[4], sys.argv[5]])\n"
            "pathlib.Path(sys.argv[3]).write_text(str(grand.pid))\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        return [
            sys.executable,
            str(child_script),
            marker,
            str(grandchild_script),
            str(grandchild_pid_path),
            str(great_grandchild_pid_path),
            str(ready_path),
        ]

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

    def test_cancellation_terminates_child_without_descendants(self):
        runtime = self._runtime("cancel direct child")
        ready = self.root / "direct child ready.txt"
        marker = "launcher-direct-" + uuid.uuid4().hex
        code = (
            "import pathlib, sys, time; "
            "pathlib.Path(sys.argv[2]).write_text('ready'); "
            "time.sleep(60)"
        )
        command = [sys.executable, "-c", code, marker, str(ready)]

        returncode = self._run_with_keyboard_interrupt(command, runtime, ready)
        child_pid = int((runtime / "child_pid.txt").read_text())
        self._register_owned_process(child_pid, marker)

        self.assertEqual(returncode, 130)
        self.assertTrue(self._wait_for_owned_exit(child_pid, marker))
        self.assertEqual(read_exit_code(runtime / "exit_code.txt"), 130)

    def test_cancellation_terminates_child_and_grandchild(self):
        runtime = self._runtime("cancel child and grandchild")
        marker = "launcher-grandchild-" + uuid.uuid4().hex
        grandchild_pid_path = self.root / "grandchild pid.txt"
        ready = self.root / "grandchild ready.txt"
        command = self._one_descendant_command(
            marker,
            grandchild_pid_path,
            ready,
        )

        returncode = self._run_with_keyboard_interrupt(command, runtime, ready)
        child_pid = int((runtime / "child_pid.txt").read_text())
        grandchild_pid = int(grandchild_pid_path.read_text())
        self._register_owned_process(child_pid, marker)
        self._register_owned_process(grandchild_pid, marker)

        self.assertEqual(returncode, 130)
        self.assertTrue(self._wait_for_owned_exit(child_pid, marker))
        self.assertTrue(self._wait_for_owned_exit(grandchild_pid, marker))

    def test_cancellation_terminates_three_generation_tree(self):
        runtime = self._runtime("cancel three generations")
        marker = "launcher-three-level-" + uuid.uuid4().hex
        grandchild_pid_path = self.root / "three level grandchild pid.txt"
        great_pid_path = self.root / "great grandchild pid.txt"
        ready = self.root / "great grandchild ready.txt"
        command = self._two_descendant_command(
            marker,
            grandchild_pid_path,
            great_pid_path,
            ready,
        )

        returncode = self._run_with_keyboard_interrupt(command, runtime, ready)
        child_pid = int((runtime / "child_pid.txt").read_text())
        grandchild_pid = int(grandchild_pid_path.read_text())
        great_pid = int(great_pid_path.read_text())
        for pid in (child_pid, grandchild_pid, great_pid):
            self._register_owned_process(pid, marker)

        self.assertEqual(returncode, 130)
        self.assertTrue(self._wait_for_owned_exit(child_pid, marker))
        self.assertTrue(self._wait_for_owned_exit(grandchild_pid, marker))
        self.assertTrue(self._wait_for_owned_exit(great_pid, marker))

    def test_normal_child_exit_closes_lingering_descendant_tree(self):
        runtime = self._runtime("normal child leaves grandchild")
        marker = "launcher-normal-cleanup-" + uuid.uuid4().hex
        grandchild_pid_path = self.root / "lingering grandchild pid.txt"
        ready = self.root / "lingering grandchild ready.txt"
        command = self._one_descendant_command(
            marker,
            grandchild_pid_path,
            ready,
            exit_parent=True,
        )

        returncode = run_process(command, runtime, cwd=self.root)
        self._wait_for_file(grandchild_pid_path)
        grandchild_pid = int(grandchild_pid_path.read_text())
        self._register_owned_process(grandchild_pid, marker)

        self.assertEqual(returncode, 0)
        self.assertTrue(self._wait_for_owned_exit(grandchild_pid, marker))

    def test_cancellation_preserves_unrelated_external_process(self):
        runtime = self._runtime("preserve external process")
        marker = "launcher-owned-" + uuid.uuid4().hex
        external_marker = "launcher-external-" + uuid.uuid4().hex
        grandchild_pid_path = self.root / "owned grandchild pid.txt"
        ready = self.root / "owned grandchild ready.txt"
        external = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", external_marker]
        )
        self._register_owned_popen(external, external_marker)
        command = self._one_descendant_command(
            marker,
            grandchild_pid_path,
            ready,
        )

        returncode = self._run_with_keyboard_interrupt(command, runtime, ready)
        grandchild_pid = int(grandchild_pid_path.read_text())
        self._register_owned_process(grandchild_pid, marker)

        self.assertEqual(returncode, 130)
        self.assertTrue(self._wait_for_owned_exit(grandchild_pid, marker))
        self.assertTrue(psutil.pid_exists(external.pid))

    @unittest.skipUnless(os.name == "nt", "Windows kill-on-close regression")
    def test_abrupt_launcher_exit_kills_its_job_tree(self):
        runtime = self._runtime("abrupt launcher exit")
        marker = "launcher-abrupt-exit-" + uuid.uuid4().hex
        grandchild_pid_path = self.root / "abrupt grandchild pid.txt"
        ready = self.root / "abrupt grandchild ready.txt"
        child_command = self._one_descendant_command(
            marker,
            grandchild_pid_path,
            ready,
        )
        launcher = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve().with_name("process_launcher.py")),
                "--runtime-directory",
                str(runtime),
                "--cwd",
                str(self.root),
                "--",
                *child_command,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._register_owned_popen(launcher, marker)
        self._wait_for_file(ready)
        child_pid = int((runtime / "child_pid.txt").read_text())
        grandchild_pid = int(grandchild_pid_path.read_text())
        self._register_owned_process(child_pid, marker)
        self._register_owned_process(grandchild_pid, marker)

        launcher.kill()
        launcher.wait(timeout=5)

        self.assertTrue(self._wait_for_owned_exit(child_pid, marker))
        self.assertTrue(self._wait_for_owned_exit(grandchild_pid, marker))
        self.assertFalse((runtime / "exit_code.txt").exists())

    @unittest.skipUnless(os.name == "nt", "Windows Job Object regression")
    def test_job_assignment_failure_is_fail_closed_before_command_runs(self):
        runtime = self._runtime("assignment failure")
        marker = "launcher-assignment-failure-" + uuid.uuid4().hex
        command_marker = self.root / "command started.txt"
        command = self._python_command(
            "import pathlib, sys; pathlib.Path(sys.argv[2]).write_text('ran')"
        ) + [marker, str(command_marker)]

        with patch(
            "process_launcher._WindowsJob.assign",
            side_effect=ProcessTreeError("controlled assignment failure"),
        ):
            returncode = run_process(command, runtime, cwd=self.root)

        child_pid = int((runtime / "child_pid.txt").read_text())
        self.assertEqual(returncode, LAUNCH_FAILURE_EXIT_CODE)
        self.assertFalse(command_marker.exists())
        self.assertTrue(self._wait_for_owned_exit(child_pid, marker))
        self.assertIn("controlled assignment failure", (runtime / "launcher_error.txt").read_text())

    @unittest.skipUnless(os.name == "nt", "Windows Job Object regression")
    def test_cancellation_before_resume_runs_no_child_code(self):
        runtime = self._runtime("cancel before resume")
        marker = "launcher-cancel-before-resume-" + uuid.uuid4().hex
        command_marker = self.root / "must not run.txt"
        command = self._python_command(
            "import pathlib, sys; pathlib.Path(sys.argv[2]).write_text('ran')"
        ) + [marker, str(command_marker)]

        with patch(
            "process_launcher._WindowsJob.assign",
            side_effect=KeyboardInterrupt,
        ):
            returncode = run_process(command, runtime, cwd=self.root)

        child_pid = int((runtime / "child_pid.txt").read_text())
        self.assertEqual(returncode, CANCELLED_EXIT_CODE)
        self.assertFalse(command_marker.exists())
        self.assertTrue(self._wait_for_owned_exit(child_pid, marker))

    @unittest.skipUnless(os.name == "nt", "Windows Job Object regression")
    def test_job_termination_failure_is_observable_and_not_success(self):
        runtime = self._runtime("termination failure")
        ready = self.root / "termination failure ready.txt"
        marker = "launcher-termination-failure-" + uuid.uuid4().hex
        command = self._python_command(
            "import pathlib, sys, time; "
            "pathlib.Path(sys.argv[2]).write_text('ready'); time.sleep(60)"
        ) + [marker, str(ready)]

        with patch(
            "process_launcher._WindowsJob.terminate",
            side_effect=OSError("controlled termination failure"),
        ):
            returncode = self._run_with_keyboard_interrupt(command, runtime, ready)

        child_pid = int((runtime / "child_pid.txt").read_text())
        self.assertEqual(returncode, CLEANUP_FAILURE_EXIT_CODE)
        self.assertTrue(self._wait_for_owned_exit(child_pid, marker))
        self.assertIn("controlled termination failure", (runtime / "launcher_error.txt").read_text())
        events = [
            json.loads(line)
            for line in (runtime / "launcher_events.jsonl").read_text().splitlines()
        ]
        self.assertIn("cleanup_error", [event["event"] for event in events])

    def test_monitoring_failure_terminates_owned_tree(self):
        runtime = self._runtime("monitoring failure")
        marker = "launcher-monitoring-failure-" + uuid.uuid4().hex
        grandchild_pid_path = self.root / "monitor failure grandchild pid.txt"
        ready = self.root / "monitor failure ready.txt"
        command = self._one_descendant_command(marker, grandchild_pid_path, ready)
        original_wait = subprocess.Popen.wait
        failed = False

        def failing_wait(process, timeout=None):
            nonlocal failed
            if not failed:
                self._wait_for_file(ready)
                failed = True
                raise RuntimeError("controlled monitoring failure")
            return original_wait(process, timeout=timeout)

        with patch("process_launcher.subprocess.Popen.wait", new=failing_wait):
            returncode = run_process(command, runtime, cwd=self.root)

        child_pid = int((runtime / "child_pid.txt").read_text())
        grandchild_pid = int(grandchild_pid_path.read_text())
        self._register_owned_process(child_pid, marker)
        self._register_owned_process(grandchild_pid, marker)
        self.assertEqual(returncode, CLEANUP_FAILURE_EXIT_CODE)
        self.assertTrue(self._wait_for_owned_exit(child_pid, marker))
        self.assertTrue(self._wait_for_owned_exit(grandchild_pid, marker))
        self.assertIn("controlled monitoring failure", (runtime / "launcher_error.txt").read_text())

    def test_cancellation_with_large_output_does_not_deadlock(self):
        runtime = self._runtime("cancel large output")
        ready = self.root / "large output ready.txt"
        marker = "launcher-large-output-" + uuid.uuid4().hex
        code = (
            "import pathlib, sys, time; "
            "sys.stdout.write('O' * 500000); sys.stdout.flush(); "
            "sys.stderr.write('E' * 300000); sys.stderr.flush(); "
            "pathlib.Path(sys.argv[2]).write_text('ready'); time.sleep(60)"
        )
        command = [sys.executable, "-c", code, marker, str(ready)]

        returncode = self._run_with_keyboard_interrupt(command, runtime, ready)
        child_pid = int((runtime / "child_pid.txt").read_text())
        self._register_owned_process(child_pid, marker)

        self.assertEqual(returncode, CANCELLED_EXIT_CODE)
        self.assertGreater((runtime / "stdout.log").stat().st_size, 400000)
        self.assertGreater((runtime / "stderr.log").stat().st_size, 200000)
        self.assertTrue(self._wait_for_owned_exit(child_pid, marker))

    def test_descendant_started_after_handshake_remains_in_owned_tree(self):
        runtime = self._runtime("delayed descendant")
        marker = "launcher-delayed-descendant-" + uuid.uuid4().hex
        grandchild_pid_path = self.root / "delayed grandchild pid.txt"
        ready = self.root / "delayed grandchild ready.txt"
        code = (
            "import pathlib, subprocess, sys, time; "
            "time.sleep(0.2); "
            "grand = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(60)', sys.argv[1]]); "
            "pathlib.Path(sys.argv[2]).write_text(str(grand.pid)); "
            "pathlib.Path(sys.argv[3]).write_text('ready'); time.sleep(60)"
        )
        command = [
            sys.executable,
            "-c",
            code,
            marker,
            str(grandchild_pid_path),
            str(ready),
        ]

        returncode = self._run_with_keyboard_interrupt(command, runtime, ready)
        child_pid = int((runtime / "child_pid.txt").read_text())
        grandchild_pid = int(grandchild_pid_path.read_text())
        self._register_owned_process(child_pid, marker)
        self._register_owned_process(grandchild_pid, marker)

        self.assertEqual(returncode, CANCELLED_EXIT_CODE)
        self.assertTrue(self._wait_for_owned_exit(child_pid, marker))
        self.assertTrue(self._wait_for_owned_exit(grandchild_pid, marker))

    def test_unicode_path_argument_and_environment_are_preserved(self):
        runtime = self._runtime("execução ç Unicode")
        result_path = self.root / "resultado ç Unicode.json"
        argument = "valor com espaços ç 日本語"
        environment = os.environ.copy()
        environment["TRADUTOR_LAUNCHER_UNICODE"] = "ambiente ç 日本語"
        code = (
            "import json, os, pathlib, sys; "
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
            "'argument': sys.argv[2], "
            "'environment': os.environ['TRADUTOR_LAUNCHER_UNICODE']}, "
            "ensure_ascii=False), encoding='utf-8')"
        )

        returncode = run_process(
            [sys.executable, "-c", code, str(result_path), argument],
            runtime,
            cwd=self.root,
            environment=environment,
        )

        self.assertEqual(returncode, 0)
        self.assertEqual(
            json.loads(result_path.read_text(encoding="utf-8")),
            {
                "argument": argument,
                "environment": "ambiente ç 日本語",
            },
        )

    @unittest.skipUnless(os.name == "nt", "Windows handle-count regression")
    def test_repeated_launches_do_not_leak_windows_handles(self):
        process = psutil.Process()
        gc.collect()
        before = process.num_handles()

        for index in range(20):
            returncode = run_process(
                self._python_command("raise SystemExit(0)"),
                self._runtime(f"handle iteration {index}"),
            )
            self.assertEqual(returncode, 0)

        gc.collect()
        after = process.num_handles()
        self.assertLessEqual(after, before + 4, (before, after))


if __name__ == "__main__":
    unittest.main()
