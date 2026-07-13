from offline_test_guard import OfflineNetworkAttempt, install_offline_network_guard

install_offline_network_guard()

import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
            socket.create_connection(("127.0.0.1", 9))

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


if __name__ == "__main__":
    unittest.main()
