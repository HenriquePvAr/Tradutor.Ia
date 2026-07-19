"""Driver resolution stays opt-in.

A missing ChromeDriver must not silently become a browser download: source analysis would
gain an unrelated network side effect. Selenium Manager is the official resolver and is
allowed only behind an exact-match flag.
"""

import _test_bootstrap  # noqa: F401

import unittest

from down import driver_download_allowed


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
        block = source[source.index("ChromeDriver local indisponivel"):][:400]
        for hint in ("CHROMEDRIVER_PATH", "PATH", "TRADUTOR_ALLOW_DRIVER_DOWNLOAD"):
            self.assertIn(hint, block, hint)


if __name__ == "__main__":
    unittest.main()
