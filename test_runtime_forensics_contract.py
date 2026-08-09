"""Forensic regression contracts from the real E2E D retest.

Hermetic: no Webtoon, no Supabase, no real login and no external network.  The
only socket use is loopback, so the tests can prove local runtime provenance.
"""

import _test_bootstrap  # noqa: F401

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
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
            current_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertIn(current_head, html)
            match = re.search(
                r"window\.__tradutorRuntimeIdentity\s*=\s*(\{.*?\});"
                r"window\.__tradutorVisualTestEnabled",
                html,
                re.S,
            )
            self.assertIsNotNone(match)
            identity = json.loads(match.group(1))
            self.assertEqual(current_head, identity["git_head"])
            self.assertEqual(proc.pid, identity["pid"])
            self.assertEqual(
                hashlib.sha256((ROOT / "ui" / "ui_shell.html").read_bytes()).hexdigest(),
                identity["shell_sha256"],
            )
            self.assertEqual(
                hashlib.sha256((ROOT / "static" / "tradutor_ui.js").read_bytes()).hexdigest(),
                identity["tradutor_ui_js_sha256"],
            )
            self.assertEqual(
                hashlib.sha256((ROOT / "static" / "tradutor_ui.css").read_bytes()).hexdigest(),
                identity["tradutor_ui_css_sha256"],
            )
            self.assertNotIn(str(Path.home()), html)
            self.assertNotIn(str(ROOT), html)
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

    def test_page_count_trace_names_excluded_logical_pages(self):
        import benchmark_pipeline as bp

        trace = bp._page_count_trace(
            {"download_gate": {"passed": True, "expected_viewer_images": 171}},
            all_image_paths=[f"raw-{i}" for i in range(171)],
            source_image_paths=[f"raw-{i}" for i in range(171)],
            image_paths=[f"logical-{i}" for i in range(1, 103)],
            completed_states=[{"index": i} for i in range(1, 102)],
            smart_split_report={"enabled": True, "source_images": 171, "pdf_pages": 102},
        )

        self.assertEqual([102], trace["excluded_logical_pages"])
        self.assertEqual("invalid_or_blank_logical_page", trace["logical_page_exclusion_reason"])

    def test_cache_completeness_compares_source_slices_not_logical_pages(self):
        import benchmark_pipeline as bp

        manifest = {
            "download_gate": {"passed": True, "expected_viewer_images": 171},
            "viewer_image_count": 171,
            "total_downloaded": 171,
        }

        self.assertTrue(
            bp._download_cache_is_complete(
                manifest,
                [f"source-slice-{index}" for index in range(171)],
            )
        )
        self.assertFalse(
            bp._download_cache_is_complete(
                manifest,
                [f"logical-page-{index}" for index in range(101)],
            )
        )

    def test_report_exposes_explicit_source_slice_and_logical_page_domains(self):
        import benchmark_pipeline as bp

        states = [{"index": index, "debug_data": {"group_count": 1}} for index in range(101)]
        page_trace = bp._page_count_trace(
            {"download_gate": {"passed": True, "expected_viewer_images": 171}},
            all_image_paths=list(range(171)),
            source_image_paths=list(range(171)),
            image_paths=list(range(101)),
            completed_states=states,
            smart_split_report={"enabled": True, "source_images": 171, "pdf_pages": 101},
        )
        group_trace = bp._group_count_trace(states)

        self.assertEqual(171, page_trace["downloaded"])
        self.assertEqual(101, page_trace["logical_pages"])
        self.assertEqual(101, group_trace["logical_pages"])
        self.assertEqual(101, group_trace["groups_persisted"])


class GroupCountDataflowTests(unittest.TestCase):
    def test_positive_synthetic_ocr_fixture_produces_groups(self):
        from ocr_balloon import analyze_image_array, get_translatable_groups
        from ocr_engine import OCRLine
        import benchmark_pipeline as bp

        original = np.full((420, 320, 3), 255, dtype=np.uint8)
        raw_lines = [
            OCRLine(
                text="Hello there",
                raw_text="Hello there",
                confidence=0.95,
                polygon=np.array([[40, 40], [180, 40], [180, 70], [40, 70]], dtype=np.float32),
                box=(40, 40, 140, 30),
                engine="synthetic-ocr",
                page=1,
                metadata={"estimated_text_regions": 1},
            )
        ]

        candidates, groups = analyze_image_array(original, raw_lines, page_index=1)
        translatable = get_translatable_groups(groups)
        trace = bp._group_count_trace([
            {
                "index": 1,
                "ocr_source": "run",
                "ocr_completed": True,
                "raw_lines": raw_lines,
                "ocr_metadata": {"estimated_text_regions": 1},
                "candidates": candidates,
                "groups": groups,
                "translatable_groups": translatable,
                "debug_data": {"group_count": len(groups), "translated_group_count": 0},
            }
        ])

        self.assertGreater(len(candidates), 0)
        self.assertGreater(len(groups), 0)
        self.assertGreater(len(translatable), 0)
        self.assertEqual(1, trace["ocr_nonempty"])
        self.assertGreater(trace["groups_created"], 0)
        self.assertGreater(trace["translation_groups"], 0)
        self.assertGreater(trace["groups_persisted"], 0)

    def test_genuinely_textless_page_can_have_zero_groups_without_bug(self):
        from ocr_balloon import analyze_image_array, get_translatable_groups
        import benchmark_pipeline as bp

        original = np.full((420, 320, 3), 255, dtype=np.uint8)
        candidates, groups = analyze_image_array(original, [], page_index=1)
        trace = bp._group_count_trace([
            {
                "index": 1,
                "ocr_source": "run",
                "ocr_completed": True,
                "raw_lines": [],
                "candidates": candidates,
                "groups": groups,
                "translatable_groups": get_translatable_groups(groups),
                "debug_data": {"group_count": len(groups), "translated_group_count": 0},
            }
        ])

        self.assertEqual([], candidates)
        self.assertEqual([], groups)
        self.assertEqual(1, trace["ocr_executed"])
        self.assertEqual(0, trace["ocr_nonempty"])
        self.assertEqual(0, trace["groups_created"])
        self.assertEqual(0, trace["groups_persisted"])
        self.assertEqual(1, trace["pages_without_text"])

    def test_trace_distinguishes_ocr_not_executed_empty_ocr_and_grouping_loss(self):
        import benchmark_pipeline as bp

        trace = bp._group_count_trace([
            {"index": 1, "status": "completed", "precheck_reason": "no_text_detected"},
            {"index": 2, "ocr_source": "run", "ocr_completed": True, "raw_lines": []},
            {
                "index": 3,
                "ocr_source": "run",
                "ocr_completed": True,
                "raw_lines": [SimpleNamespace()],
                "candidates": [SimpleNamespace(ignored=False)],
                "groups": [],
                "debug_data": {"ocr_line_count": 1, "group_count": 0},
            },
        ])

        self.assertEqual(3, trace["logical_pages"])
        self.assertEqual(2, trace["ocr_inputs"])
        self.assertEqual(2, trace["ocr_executed"])
        self.assertEqual(1, trace["ocr_nonempty"])
        self.assertEqual(1, trace["candidate_regions"])
        self.assertEqual(0, trace["groups_created"])

    def test_trace_uses_persisted_debug_counts_after_raw_ocr_lines_are_dropped(self):
        import benchmark_pipeline as bp

        trace = bp._group_count_trace([
            {
                "index": 1,
                "status": "completed",
                "debug_data": {
                    "ocr_line_count": 1,
                    "group_count": 1,
                    "translated_group_count": 1,
                },
            }
        ])

        self.assertEqual(1, trace["ocr_nonempty"])
        self.assertEqual(0, trace["pages_without_text"])
        self.assertEqual(1, trace["regions_detected"])
        self.assertEqual(1, trace["candidate_regions"])
        self.assertEqual(1, trace["groups_created"])
        self.assertEqual(1, trace["translation_groups"])
        self.assertEqual(1, trace["groups_persisted"])


class PdfOpenContractTests(unittest.TestCase):
    def test_pdf_open_contract_is_os_default_viewer_for_owner_scoped_artifact(self):
        import ui_bridge

        tmp = Path(tempfile.mkdtemp())
        output = (tmp / "output").resolve()
        output.mkdir()
        pdf = output / "chapter.pdf"
        pdf.write_bytes(b"%PDF-1.4\n%synthetic\n")

        bridge = ui_bridge.UiBridge.__new__(ui_bridge.UiBridge)
        bridge.output_root = output
        bridge.store = SimpleNamespace(
            get_job_for_owner=lambda owner, job: {
                "id": job,
                "owner_id": owner,
                "pdf_path": str(pdf),
            }
        )
        bridge._job_record = lambda job: job

        with mock.patch.object(bridge, "_open_artifact_path") as opener:
            bridge.open_artifact_for_owner("owner-a", "job-b", "pdf")

        opener.assert_called_once_with(str(pdf), select=False)

    def test_open_artifact_path_invokes_platform_opener_and_stays_inside_output_root(self):
        import ui_bridge

        tmp = Path(tempfile.mkdtemp())
        output = (tmp / "output").resolve()
        output.mkdir()
        pdf = output / "chapter.pdf"
        pdf.write_bytes(b"%PDF-1.4\n%synthetic\n")
        outside = (tmp / "outside.pdf").resolve()
        outside.write_bytes(b"%PDF-1.4\n%outside\n")

        bridge = ui_bridge.UiBridge.__new__(ui_bridge.UiBridge)
        bridge.output_root = output

        if os.name == "nt":
            with mock.patch("os.startfile", create=True) as opener:
                bridge._open_artifact_path(str(pdf))
            opener.assert_called_once_with(pdf)
        else:
            with mock.patch("webbrowser.open") as opener:
                bridge._open_artifact_path(str(pdf))
            opener.assert_called_once_with(pdf.as_uri())

        with self.assertRaises(ValueError):
            bridge._open_artifact_path(str(outside))


if __name__ == "__main__":
    unittest.main()
