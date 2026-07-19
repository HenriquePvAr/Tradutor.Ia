from offline_test_guard import OfflineNetworkAttempt, install_offline_network_guard

install_offline_network_guard()

import ast
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sitecustomize

from scripts.manual_network import NetworkSmokeNotAuthorized
from scripts import manual_nvidia_smoke, manual_webtoon_smoke


class HermeticTestBoundaryTests(unittest.TestCase):
    def _environment(self, **values):
        environment = dict(os.environ)
        environment.pop("ALLOW_NETWORK_TESTS", None)
        environment.pop("ALLOW_NVIDIA_SMOKE", None)
        environment.pop("ALLOW_WEBTOON_SMOKE", None)
        environment.update(values)
        return patch.dict(os.environ, environment, clear=True)

    def test_manual_modules_import_without_pipeline_clients(self):
        self.assertNotIn("translator_nvidia", manual_nvidia_smoke.__dict__)
        self.assertNotIn("down", manual_webtoon_smoke.__dict__)
        self.assertNotIn("config", manual_webtoon_smoke.__dict__)

    def test_nvidia_smoke_blocks_without_generic_opt_in_before_client_creation(self):
        with self._environment(), patch.object(manual_nvidia_smoke, "run_smoke") as run:
            result = manual_nvidia_smoke.main([])

        self.assertEqual(result, 2)
        run.assert_not_called()

    def test_nvidia_smoke_requires_specific_opt_in(self):
        with self._environment(ALLOW_NETWORK_TESTS="1"), patch.object(
            manual_nvidia_smoke, "run_smoke"
        ) as run:
            result = manual_nvidia_smoke.main([])

        self.assertEqual(result, 2)
        run.assert_not_called()

    def test_direct_nvidia_smoke_call_requires_opt_in_before_importing_client(self):
        attempted_imports = []
        original_import = __import__

        def recording_import(name, *args, **kwargs):
            if name == "translator_nvidia":
                attempted_imports.append(name)
            return original_import(name, *args, **kwargs)

        with self._environment(), patch("builtins.__import__", side_effect=recording_import):
            with self.assertRaises(NetworkSmokeNotAuthorized):
                manual_nvidia_smoke.run_smoke()

        self.assertEqual(attempted_imports, [])

    def test_only_exact_one_authorizes_network_smokes(self):
        with self._environment(
            ALLOW_NETWORK_TESTS="true",
            ALLOW_NVIDIA_SMOKE="1",
        ), patch.object(manual_nvidia_smoke, "run_smoke") as run:
            result = manual_nvidia_smoke.main([])

        self.assertEqual(result, 2)
        run.assert_not_called()

    def test_webtoon_smoke_blocks_before_selenium_and_output_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "smoke_blocked"
            with self._environment(), patch.object(
                manual_webtoon_smoke, "run_smoke"
            ) as run:
                result = manual_webtoon_smoke.main(
                    [
                        "--url",
                        "https://example.invalid/chapter",
                        "--output-dir",
                        str(output),
                    ]
                )

            self.assertEqual(result, 2)
            run.assert_not_called()
            self.assertFalse(output.exists())

    def test_authorized_webtoon_smoke_uses_isolated_cache_and_intermediates(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "smoke_isolated"
            previous = {
                name: os.environ.get(name)
                for name in ("CACHE_ROOT", "TEMP_FOLDER", "TEMP_OUT")
            }
            try:
                manual_webtoon_smoke._configure_isolated_smoke_environment(output)
                self.assertEqual(os.environ["CACHE_ROOT"], str(output / "smoke_cache"))
                self.assertEqual(os.environ["TEMP_FOLDER"], str(output / "input"))
                self.assertEqual(os.environ["TEMP_OUT"], str(output / "rendered"))
            finally:
                for name, value in previous.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

    def test_blocked_nvidia_smoke_does_not_create_cache_client(self):
        with self._environment(), patch.object(manual_nvidia_smoke, "run_smoke") as run:
            result = manual_nvidia_smoke.main([])

        self.assertEqual(result, 2)
        run.assert_not_called()
        self.assertNotIn("translator_nvidia", manual_nvidia_smoke.__dict__)

    def test_standard_suite_blocks_socket_before_any_request(self):
        with self.assertRaises(OfflineNetworkAttempt):
            socket.create_connection(("198.51.100.9", 443))

    def test_standard_suite_blocks_connect_and_connect_ex_before_any_request(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            with self.assertRaises(OfflineNetworkAttempt):
                probe.connect(("198.51.100.9", 443))
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            with self.assertRaises(OfflineNetworkAttempt):
                probe.connect_ex(("198.51.100.9", 443))

    def test_standard_suite_blocks_connectionless_udp_before_any_request(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            with self.assertRaises(OfflineNetworkAttempt):
                probe.sendto(b"offline-test", ("198.51.100.9", 9))

    def test_standard_suite_uses_a_synthetic_public_dns_answer(self):
        answer = socket.getaddrinfo("reader.example.test", 443)
        self.assertEqual(answer[0][4][0], "93.184.216.34")

    def test_unittest_bootstrap_installs_the_guard_before_importing_tests(self):
        root = Path(__file__).resolve().parent
        environment = dict(os.environ)
        # Prove the direct unittest bootstrap rather than relying on inheritance from this
        # already-guarded pytest process.
        environment.pop("TRADUTOR_IA_OFFLINE_TEST_GUARD", None)
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", "test_unittest_guard_probe.UnittestGuardProbe"],
            cwd=root, env=environment, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_fake_socket_can_be_used_by_unit_tests(self):
        sentinel = object()
        with patch("socket.create_connection", return_value=sentinel):
            self.assertIs(socket.create_connection(("example.invalid", 443)), sentinel)

    def test_manual_smokes_are_not_standard_test_filenames(self):
        root = Path(__file__).resolve().parent
        discovered = {path.name for path in root.glob("test_*.py")}

        self.assertNotIn("test_pipeline_webtoon.py", discovered)
        self.assertNotIn("test_nvidia_translation.py", discovered)
        self.assertTrue((root / "scripts" / "manual_webtoon_smoke.py").is_file())
        self.assertTrue((root / "scripts" / "manual_nvidia_smoke.py").is_file())

    def test_sitecustomize_only_uses_the_test_entrypoint_or_inherited_marker(self):
        self.assertTrue(sitecustomize._is_test_invocation(
            [r"C:\\venv\\Lib\\site-packages\\pytest\\__main__.py"], {}
        ))
        self.assertTrue(sitecustomize._is_test_invocation(
            [r"C:\\Python\\Lib\\unittest\\__main__.py"], {}
        ))
        self.assertTrue(sitecustomize._is_test_invocation(["test_adapter.py"], {}))
        self.assertFalse(sitecustomize._is_test_invocation(
            ["run_webtoon.py", "--output", "pytest_demo"], {}
        ))

    @staticmethod
    def _has_early_unittest_guard(path: Path) -> bool:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        saw_guard_import = False
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                continue
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                continue
            if isinstance(node, ast.Import):
                if any(alias.name == "_test_bootstrap" for alias in node.names):
                    return True
            if isinstance(node, ast.ImportFrom):
                if node.module == "offline_test_guard" and any(
                    alias.name == "install_offline_network_guard" for alias in node.names
                ):
                    saw_guard_import = True
                    continue
            if (
                saw_guard_import
                and isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "install_offline_network_guard"
            ):
                return True
            return False
        return False

    def test_every_discoverable_test_installs_the_offline_guard_for_unittest_too(self):
        root = Path(__file__).resolve().parent
        candidates = set(root.rglob("test_*.py")) | set(root.rglob("*_test.py"))
        tests = sorted(
            path for path in candidates
            if ".venv" not in path.parts and "__pycache__" not in path.parts
        )
        missing = [str(path.relative_to(root)) for path in tests if not self._has_early_unittest_guard(path)]
        self.assertEqual(missing, [], f"testes sem guard offline cedo: {missing}")


if __name__ == "__main__":
    unittest.main()
