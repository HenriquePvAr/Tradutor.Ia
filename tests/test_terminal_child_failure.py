"""A terminal pipeline-child failure must reach the parent runner.

Beta-17 stall (job cf1bfa871f15): the pipeline child hit ``LimitExceeded`` and printed
``CHILD_EXIT exit_code=1``, but the frozen process did not actually terminate (a
lingering non-daemon thread / spawned helper), so the runner's ``proc.poll()`` never
observed the exit and the job was heartbeated as ``running`` forever instead of
transitioning to ``failed``.

``start_tradutor._fail_terminal_child`` now guarantees a frozen child exits hard, while
a source/test run re-raises so in-process callers and tracebacks behave normally.
"""
import io
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

import start_tradutor


class TerminalChildFailureTests(unittest.TestCase):
    def test_source_mode_reraises_and_reports_exit(self):
        buf = io.BytesIO()
        text = io.TextIOWrapper(buf, encoding="utf-8")
        err = ValueError("boom")
        with mock.patch.object(sys, "frozen", False, create=True):
            with redirect_stdout(text):
                with self.assertRaises(ValueError):
                    start_tradutor._fail_terminal_child(err, "pipeline_startup")
        text.flush()
        out = buf.getvalue().decode("utf-8")
        self.assertIn("CHILD_STARTUP_EXCEPTION exception_class=ValueError", out)
        self.assertIn("stage=pipeline_startup", out)
        self.assertIn("CHILD_EXIT exit_code=1", out)

    def test_frozen_mode_hard_exits_nonzero(self):
        class _HardExit(BaseException):
            def __init__(self, code):
                self.code = code

        def _fake_exit(code):
            raise _HardExit(code)

        with mock.patch.object(sys, "frozen", True, create=True):
            with mock.patch("os._exit", _fake_exit):
                with redirect_stdout(io.StringIO()):
                    with self.assertRaises(_HardExit) as ctx:
                        start_tradutor._fail_terminal_child(RuntimeError("x"), "pipeline_startup")
        self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
