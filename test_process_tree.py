"""Ownership validation and tree termination in the process helper."""

import subprocess
import sys
import time
import unittest

import process_tree


def _spawn_sleeper():
    # A python process that spawns a child sleeper, so we get a small tree to terminate.
    code = (
        "import subprocess,sys,time;"
        "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "print(c.pid,flush=True);"
        "time.sleep(60)"
    )
    return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)


class ProcessTreeTests(unittest.TestCase):
    def test_snapshot_of_missing_is_none(self):
        self.assertIsNone(process_tree.snapshot(2_000_000_000))
        self.assertIsNone(process_tree.snapshot(None))

    def test_matches_requires_fingerprint(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
        try:
            self.assertTrue(process_tree.matches(proc.pid, substrings=["python"])
                            or process_tree.matches(proc.pid, substrings=[]))
            # A fingerprint the command line does not contain fails closed.
            self.assertFalse(process_tree.matches(proc.pid, substrings=["job_runner.py"]))
        finally:
            proc.terminate(); proc.wait(timeout=10)

    def test_create_time_mismatch_fails_closed(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
        try:
            self.assertFalse(process_tree.matches(proc.pid, create_time=1.0))  # wrong start
        finally:
            proc.terminate(); proc.wait(timeout=10)

    def test_terminate_tree_stops_children(self):
        parent = _spawn_sleeper()
        child_pid = int(parent.stdout.readline().decode().strip())
        self.assertIsNotNone(process_tree.snapshot(parent.pid))
        self.assertIsNotNone(process_tree.snapshot(child_pid))
        report = process_tree.terminate_tree(parent.pid, timeout=10)
        self.assertTrue(report["validated"])
        self.assertEqual(report["reason"], "stopped")
        self.assertTrue(process_tree.wait_gone(parent.pid, timeout=10))
        self.assertTrue(process_tree.wait_gone(child_pid, timeout=10))

    def test_terminate_tree_refuses_on_ownership_mismatch(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
        try:
            # A fingerprint that does not match => nothing is terminated.
            report = process_tree.terminate_tree(proc.pid, substrings=["job_runner.py"], timeout=5)
            self.assertFalse(report["validated"])
            self.assertEqual(report["reason"], "ownership_mismatch")
            self.assertIsNotNone(process_tree.snapshot(proc.pid))  # still alive
        finally:
            proc.terminate(); proc.wait(timeout=10)

    def test_terminate_tree_on_missing_pid(self):
        report = process_tree.terminate_tree(2_000_000_000, timeout=1)
        self.assertFalse(report["validated"])
        self.assertEqual(report["reason"], "not_running")


if __name__ == "__main__":
    unittest.main()
