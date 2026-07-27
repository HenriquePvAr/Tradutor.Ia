"""Driver resolution stays opt-in.

A missing ChromeDriver must not silently become a browser download: source analysis would
gain an unrelated network side effect. Selenium Manager is the official resolver and is
allowed only behind an exact-match flag.
"""

import _test_bootstrap  # noqa: F401

import unittest

from down import driver_download_allowed, driver_resolution_diagnostics


class DriverPolicyTests(unittest.TestCase):
    def test_disabled_by_default(self):
        self.assertFalse(driver_download_allowed({}))

    def test_only_the_literal_one_enables_it(self):
        self.assertTrue(driver_download_allowed({"TRADUTOR_ALLOW_DRIVER_DOWNLOAD": "1"}))
        for value in ("true", "yes", "0", "", "on", "TRUE"):
            self.assertFalse(
                driver_download_allowed({"TRADUTOR_ALLOW_DRIVER_DOWNLOAD": value}), value)

    def test_whitespace_is_tolerated_but_not_other_text(self):
        self.assertTrue(driver_download_allowed({"TRADUTOR_ALLOW_DRIVER_DOWNLOAD": " 1 "}))
        self.assertFalse(driver_download_allowed({"TRADUTOR_ALLOW_DRIVER_DOWNLOAD": "1x"}))

    def test_single_flag_name_no_competing_alias(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parent / "down.py").read_text(encoding="utf-8")
        self.assertIn("TRADUTOR_ALLOW_DRIVER_DOWNLOAD", source)
        for alias in ("SELENIUM_MANAGER_ENABLED", "ALLOW_CHROMEDRIVER_DOWNLOAD"):
            self.assertNotIn(alias, source, alias)

    def test_failure_message_names_every_option(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parent / "down.py").read_text(encoding="utf-8")
        block = source[source.index("CHROMEDRIVER_UNAVAILABLE"):][:500]
        for hint in ("CHROMEDRIVER_PATH", "PATH", "TRADUTOR_ALLOW_DRIVER_DOWNLOAD"):
            self.assertIn(hint, block, hint)

    def test_missing_driver_is_reported_with_specific_source_code(self):
        import down
        from chapter_source import CHROMEDRIVER_UNAVAILABLE, SourceError
        from unittest import mock

        with mock.patch.object(down, "CHROMEDRIVER_PATH", ""), \
             mock.patch.object(down.shutil, "which", return_value=None), \
             mock.patch.object(down, "driver_download_allowed", return_value=False):
            with self.assertRaises(SourceError) as raised:
                down._create_driver()

        self.assertIn(
            raised.exception.code,
            {CHROMEDRIVER_UNAVAILABLE, "browser_runtime_unavailable"},
        )

    def test_driver_diagnostics_are_sanitized_and_show_selenium_manager(self):
        import down
        from unittest import mock

        with mock.patch.object(down, "CHROMEDRIVER_PATH", ""), \
             mock.patch.object(down.os.path, "isfile", return_value=False), \
             mock.patch.object(down.shutil, "which", return_value=None):
            info = driver_resolution_diagnostics({"TRADUTOR_ALLOW_DRIVER_DOWNLOAD": "1"})

        self.assertTrue(info["driver_download_allowed"])
        self.assertFalse(info["chromedriver_path_configured"])
        self.assertTrue(info["selenium_manager_available"])
        self.assertEqual(info["driver_resolution_source"], "selenium_manager")
        self.assertNotIn("C:\\", str(info))

    def test_no_space_left_on_device_is_reported_as_disk_full(self):
        import errno
        import down

        self.assertEqual(
            down._pipeline_exception_code(OSError(errno.ENOSPC, "No space left on device")),
            "disk_full",
        )

    def test_browser_failures_keep_actionable_reason_codes(self):
        import down

        cases = {
            RuntimeError("session not created: Chrome failed to start"):
                "browser_launch_failed",
            RuntimeError("driver not found"): "browser_driver_unavailable",
            TimeoutError("startup timeout"): "browser_startup_timeout",
            RuntimeError("unexpected source analysis issue"): "source_analysis_failed",
        }
        for error, expected in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(down._pipeline_exception_code(error), expected)


if __name__ == "__main__":
    unittest.main()
