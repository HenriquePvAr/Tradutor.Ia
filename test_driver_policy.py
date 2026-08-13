"""Driver resolution uses supported, bounded Selenium Manager when needed.

A missing ChromeDriver must not become a manual developer prerequisite on a clean Windows
install. Selenium Manager is the official resolver; tests keep it hermetic by default and
explicit configuration still has exact-match semantics.
"""

import _test_bootstrap  # noqa: F401

import unittest

from down import driver_download_allowed, driver_resolution_diagnostics


class _Runtime:
    engine = "chrome"
    executable_path = "C:/Program Files/Google/Chrome/Application/chrome.exe"
    driver_path = ""

    @staticmethod
    def public():
        return {
            "engine": "chrome",
            "executable_path": "chrome.exe",
            "driver_path": "",
            "runtime_source": "system",
        }


class _Driver:
    pass


class DriverPolicyTests(unittest.TestCase):
    def test_enabled_by_default_outside_hermetic_tests(self):
        self.assertTrue(driver_download_allowed({}))
        self.assertTrue(driver_download_allowed({"PATH": ""}))

    def test_disabled_by_default_in_hermetic_tests(self):
        self.assertFalse(driver_download_allowed({"TRADUTOR_IA_HERMETIC_TEST_ENV": "1"}))

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

    def test_browser_found_legacy_driver_missing_uses_selenium_manager_once(self):
        import down
        from unittest import mock

        created = []

        with mock.patch.object(down, "CHROMEDRIVER_PATH", ""), \
             mock.patch("browser_runtime.BrowserRuntimeResolver.resolve", return_value=_Runtime()), \
             mock.patch.object(down, "driver_download_allowed", return_value=True), \
             mock.patch.object(down.webdriver, "Chrome",
                               side_effect=lambda service, options: created.append(
                                   (service, options)) or _Driver()):
            driver = down._create_driver()

        self.assertIsInstance(driver, _Driver)
        self.assertEqual(len(created), 1)
        self.assertIn(getattr(created[0][0], "path", None), {"", None})

    def test_missing_driver_is_reported_with_specific_source_code_when_manager_disabled(self):
        import down
        from chapter_source import CHROMEDRIVER_UNAVAILABLE, SourceError
        from unittest import mock

        with mock.patch.object(down, "CHROMEDRIVER_PATH", ""), \
             mock.patch("browser_runtime.BrowserRuntimeResolver.resolve", return_value=_Runtime()), \
             mock.patch.object(down, "driver_download_allowed", return_value=False):
            with self.assertRaises(SourceError) as raised:
                down._create_driver()

        self.assertIn(
            raised.exception.code,
            {CHROMEDRIVER_UNAVAILABLE, "browser_runtime_unavailable"},
        )

    def test_selenium_manager_failure_is_categorized_and_cleans_profile(self):
        import down
        from chapter_source import BROWSER_DRIVER_UNAVAILABLE, SourceError
        from unittest import mock

        with mock.patch.object(down, "CHROMEDRIVER_PATH", ""), \
             mock.patch("browser_runtime.BrowserRuntimeResolver.resolve", return_value=_Runtime()), \
             mock.patch.object(down, "driver_download_allowed", return_value=True), \
             mock.patch.object(down.webdriver, "Chrome",
                               side_effect=RuntimeError("Unable to obtain driver")), \
             mock.patch.object(down.shutil, "rmtree") as rmtree:
            with self.assertRaises(SourceError) as raised:
                down._create_driver()

        self.assertEqual(raised.exception.code, BROWSER_DRIVER_UNAVAILABLE)
        self.assertTrue(rmtree.called)

    def test_existing_explicit_driver_path_still_wins(self):
        import down
        from unittest import mock

        runtime = _Runtime()
        runtime.driver_path = "C:/tools/chromedriver.exe"
        created = []

        with mock.patch.object(down, "CHROMEDRIVER_PATH", runtime.driver_path), \
             mock.patch("browser_runtime.BrowserRuntimeResolver.resolve", return_value=runtime), \
             mock.patch.object(down.webdriver, "Chrome",
                               side_effect=lambda service, options: created.append(
                                   (service, options)) or _Driver()):
            driver = down._create_driver()

        self.assertIsInstance(driver, _Driver)
        self.assertEqual(len(created), 1)
        self.assertEqual(getattr(created[0][0], "path", None), runtime.driver_path)

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
