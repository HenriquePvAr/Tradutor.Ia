"""Forensic regression contracts from the real E2E D retest.

Hermetic: no Webtoon, no Supabase, no real login and no external network.  The
only socket use is loopback, so the tests can prove local runtime provenance.
"""

import _test_bootstrap  # noqa: F401

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from PIL import Image


ROOT = Path(__file__).resolve().parent


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RuntimeUiForensicsTests(unittest.TestCase):
    def test_startup_port_preflight_fails_when_port_is_already_owned(self):
        import app_ui

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as owner:
            owner.bind(("127.0.0.1", 0))
            owner.listen(1)
            port = int(owner.getsockname()[1])

            with self.assertRaises(SystemExit) as raised:
                app_ui._assert_startup_port_available("127.0.0.1", port)

        self.assertIn("port_in_use", str(raised.exception))

    def test_local_http_runtime_serves_branch_shell_not_stale_8080_process(self):
        port = _free_loopback_port()
        tmp = Path(tempfile.mkdtemp())
        env = {
            **os.environ,
            "APP_ENV": "test",
            "AUTH_PROVIDER": "local_test",
            "ALLOW_LOCAL_TEST_IDENTITIES": "1",
            "LOCAL_TEST_AUTH_DB": str(tmp / "local-test-auth.sqlite3"),
            "LOCAL_TEST_AUTH_SESSION_SECRET": "x" * 40,
            "STORAGE_PROVIDER": "local_test",
            "LOCAL_TEST_STORAGE_ROOT": str(tmp / "local-test-storage"),
            "TRADUTOR_UI_PORT": str(port),
            "TRADUTOR_UI_HOST": "127.0.0.1",
        }
        env["NICEGUI_SCREEN_TEST_PORT"] = str(port)
        proc = subprocess.Popen(
            [sys.executable, "app_ui.py"],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            html = ""
            deadline = time.time() + 45
            last_error = None
            while time.time() < deadline:
                if proc.poll() is not None:
                    output = proc.stdout.read() if proc.stdout else ""
                    self.fail(f"app_ui.py exited before serving HTTP: {output}")
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                        html = response.read().decode("utf-8", "replace")
                    break
                except Exception as exc:  # noqa: BLE001 - keep polling startup
                    last_error = exc
                    time.sleep(0.5)
            else:
                self.fail(f"local UI did not become ready: {last_error!r}")

            self.assertIn('<button type="button" class="rail-tab active" data-tab="inicio"', html)
            self.assertNotIn('<li class="rail-tab active" data-tab="inicio"', html)
            self.assertIn("window.__tradutorRuntimeIdentity", html)
            self.assertIn("0d863495ee5637c8e0532b51bfe2aea62a4952f1", html)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


class DownloadCountConservationTests(unittest.TestCase):
    def _png(self, path: Path) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (500, 300), "white").save(path)
        return str(path)

    def test_partial_cache_101_is_not_reused_when_manifest_declares_171_expected(self):
        import benchmark_pipeline as bp

        tmp = Path(tempfile.mkdtemp())
        cache_root = tmp / "cache"
        output = tmp / "out"
        url = "https://www.webtoons.com/en/action/example/episode-1/viewer"
        key = bp.stable_hash({
            "url": url,
            "max_images": None,
            "source_candidate_ids": [],
            "download_rules": "chapter-download-v4",
        })
        cache_dir = cache_root / "downloads" / key
        cache_items = []
        for index in range(1, 102):
            path = Path(self._png(cache_dir / f"{index:03}.png"))
            cache_items.append({
                "path": str(path),
                "url": f"https://cdn.example/{index:03}.png",
                "is_chapter_candidate": True,
                "sha256": bp.file_sha256(path),
            })
        bp.atomic_write_json(cache_dir / "manifest.json", {
            "download_gate": {
                "passed": False,
                "expected_viewer_images": 171,
                "downloaded_viewer_images": 101,
                "missing_viewer_images": 70,
                "reasons": ["viewer_count_mismatch", "viewer_images_missing"],
            },
            "downloaded": cache_items,
            "total_downloaded": 101,
        })

        def fake_download_images(*_args, **kwargs):
            target = Path(kwargs["target_folder"])
            report_items = []
            for index in range(1, 172):
                path = Path(self._png(target / f"{index:03}.png"))
                report_items.append({
                    "path": str(path),
                    "url": f"https://cdn.example/new-{index:03}.png",
                    "is_chapter_candidate": True,
                })
            bp.atomic_write_json(output / "downloaded_images.json", {
                "download_gate": {"passed": True, "expected_viewer_images": 171},
                "viewer_image_count": 171,
                "downloaded": report_items,
                "total_downloaded": 171,
            })
            return [item["path"] for item in report_items]

        with mock.patch.object(bp, "cache_folder", lambda name: cache_root / name), \
                mock.patch.object(bp, "_download_cache_reuse_allowed", return_value=True), \
                mock.patch.object(bp, "download_images", side_effect=fake_download_images):
            paths, manifest, cache_hit = bp._download_with_cache(
                url, None, output, force=False, source_candidate_ids=[]
            )

        self.assertFalse(cache_hit)
        self.assertEqual(171, len(paths))
        self.assertEqual(171, manifest["total_downloaded"])

    def test_page_count_trace_preserves_171_source_to_101_logical_boundary(self):
        import benchmark_pipeline as bp

        trace = bp._page_count_trace(
            {
                "download_gate": {"passed": True, "expected_viewer_images": 171},
                "viewer_image_count": 171,
            },
            all_image_paths=[f"raw-{i}" for i in range(171)],
            source_image_paths=[f"raw-{i}" for i in range(171)],
            image_paths=[f"logical-{i}" for i in range(101)],
            completed_states=[{"index": i} for i in range(101)],
            smart_split_report={
                "enabled": True,
                "source_images": 171,
                "pdf_pages": 101,
                "unsafe_split_count": 4,
            },
        )

        self.assertEqual(171, trace["source_expected"])
        self.assertEqual(171, trace["downloaded"])
        self.assertEqual(101, trace["logical_pages"])
        self.assertEqual(101, trace["processed_pages"])
        self.assertTrue(trace["smart_split_enabled"])


if __name__ == "__main__":
    unittest.main()
