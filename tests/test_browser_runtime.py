import _test_bootstrap  # noqa: F401
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from browser_runtime import BrowserRuntimeResolver


class BrowserRuntimeResolverTests(unittest.TestCase):
    def executable(self, folder, name="browser.exe"):
        path = Path(folder) / name
        path.write_bytes(b"runtime")
        return str(path)

    def test_configured_executable_wins_and_supports_spaces_unicode(self):
        with tempfile.TemporaryDirectory(prefix="navegador ç ") as folder:
            executable = self.executable(folder)
            runtime = BrowserRuntimeResolver(
                environment={}, platform_name="Windows").resolve(
                    operation="source_analysis", configured_executable=executable)
        self.assertEqual(runtime.executable_path, executable)
        self.assertEqual(runtime.runtime_source, "configured")
        self.assertEqual(runtime.headless_mode, "new")

    def test_missing_configured_executable_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "browser_executable_not_found"):
            BrowserRuntimeResolver(environment={}, platform_name="Windows").resolve(
                operation="source_analysis", configured_executable="missing.exe")

    def test_system_chrome_precedes_edge_deterministically(self):
        resolver = BrowserRuntimeResolver(environment={}, platform_name="Windows")
        with tempfile.TemporaryDirectory() as folder:
            chrome = self.executable(folder, "chrome.exe")
            edge = self.executable(folder, "msedge.exe")
            with mock.patch.object(
                resolver, "_system_candidates",
                return_value=[("chrome", chrome), ("edge", edge)]
            ):
                first = resolver.resolve(operation="source_analysis")
                selected_edge = resolver.resolve(
                    operation="source_analysis", preferred_engine="edge")
        self.assertEqual(first.engine, "chrome")
        self.assertEqual(selected_edge.engine, "edge")
        self.assertEqual(len(first.policy_hash), 64)

    def test_no_runtime_is_specific(self):
        resolver = BrowserRuntimeResolver(environment={}, platform_name="Windows")
        with mock.patch.object(resolver, "_system_candidates", return_value=[]):
            with self.assertRaisesRegex(ValueError, "browser_runtime_unavailable"):
                resolver.resolve(operation="source_analysis")

    def test_public_diagnostics_hide_personal_path(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = self.executable(folder, "chrome.exe")
            public = BrowserRuntimeResolver(
                environment={}, platform_name="Windows").resolve(
                    operation="source_analysis",
                    configured_executable=executable).public()
        self.assertEqual(public["executable_path"], "chrome.exe")
        self.assertNotIn(folder, str(public))

    def test_policy_hash_is_deterministic(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = self.executable(folder)
            resolver = BrowserRuntimeResolver(environment={}, platform_name="Windows")
            first = resolver.resolve(
                operation="source_analysis", configured_executable=executable)
            second = resolver.resolve(
                operation="source_analysis", configured_executable=executable)
        self.assertEqual(first.policy_hash, second.policy_hash)


if __name__ == "__main__":
    unittest.main()
