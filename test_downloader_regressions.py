import io
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image, ImageDraw
from benchmark_pipeline import _valid_download_paths

from down import (
    _bounded_driver_teardown,
    _build_download_gate,
    _capture_driver_ownership,
    _candidate_skip_reason,
    _persist_download_metadata,
    _process_matches_ownership,
    _validate_image_bytes,
    _viewer_image_snapshot,
    download_images,
)


class _FakeDriver:
    def execute_script(self, _script):
        return {
            "imageCount": 3,
            "urls": ["https://example.test/1.jpg", "https://example.test/2.jpg", "https://example.test/3.jpg"],
        }


class _QuitDriver:
    def __init__(self, action=None):
        self.action = action
        self.quit_calls = 0

    def get(self, _url):
        return None

    def quit(self):
        self.quit_calls += 1
        if self.action:
            self.action()


class _FakeProcess:
    def __init__(
        self,
        pid,
        ppid,
        created,
        name,
        executable,
        command_line,
        survives_terminate=False,
    ):
        self.pid = pid
        self._ppid = ppid
        self._created = created
        self._name = name
        self._executable = executable
        self._command_line = command_line
        self._survives_terminate = survives_terminate
        self._children = []
        self.alive = True
        self.terminate_calls = 0
        self.kill_calls = 0

    def ppid(self):
        return self._ppid

    def create_time(self):
        return self._created

    def name(self):
        return self._name

    def exe(self):
        return self._executable

    def cmdline(self):
        return list(self._command_line)

    def children(self, recursive=True):
        return list(self._children)

    def terminate(self):
        self.terminate_calls += 1
        if not self._survives_terminate:
            self.alive = False

    def kill(self):
        self.kill_calls += 1
        self.alive = False


class _FakeProcessApi:
    def __init__(self, processes):
        self.processes = {process.pid: process for process in processes}

    def Process(self, pid):
        return self.processes[pid]

    @staticmethod
    def wait_procs(processes, timeout):
        del timeout
        gone = [process for process in processes if not process.alive]
        alive = [process for process in processes if process.alive]
        return gone, alive


class DownloaderRegressionTests(unittest.TestCase):
    def test_lazy_placeholder_uses_declared_chapter_dimensions(self):
        candidate = {
            "url": "https://example.test/chapter-page.jpg",
            "source": "data-url",
            "naturalWidth": 1,
            "naturalHeight": 1,
            "width": 800,
            "height": 1000,
            "declaredWidth": 800,
            "declaredHeight": 1000,
            "isChapterCandidate": True,
        }
        self.assertIsNone(_candidate_skip_reason(candidate))

    def test_non_chapter_placeholder_remains_rejected(self):
        candidate = {
            "url": "https://example.test/recommendation.jpg",
            "source": "src",
            "naturalWidth": 1,
            "naturalHeight": 1,
            "width": 1,
            "height": 1,
        }
        self.assertEqual(_candidate_skip_reason(candidate), "too_small_dom_size")

    def test_short_chapter_strip_is_validated_conservatively(self):
        image = Image.new("RGB", (800, 80), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((30, 20, 770, 60), fill="black")
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        valid, info, decoded = _validate_image_bytes(
            buffer.getvalue(), chapter_candidate=True
        )
        self.assertTrue(valid)
        self.assertEqual((info["width"], info["height"]), (800, 80))
        decoded.close()

    def test_official_flat_separator_is_preserved(self):
        image = Image.new("RGB", (800, 26), "white")
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        valid, info, decoded = _validate_image_bytes(
            buffer.getvalue(), chapter_candidate=True
        )
        self.assertTrue(valid)
        self.assertEqual((info["width"], info["height"]), (800, 26))
        decoded.close()

    def test_download_gate_detects_missing_viewer_image(self):
        report = {
            "viewer_urls": ["one", "two", "three"],
            "downloaded": [
                {"url": "one", "order": 1, "is_chapter_candidate": True},
                {"url": "three", "order": 3, "is_chapter_candidate": True},
            ],
        }
        gate = _build_download_gate(report)
        self.assertFalse(gate["passed"])
        self.assertEqual(gate["missing_viewer_images"], 1)

    def test_partial_download_gate_uses_requested_limit(self):
        report = {
            "viewer_urls": ["one", "two", "three"],
            "requested_max_images": 2,
            "downloaded": [
                {"url": "one", "order": 1, "is_chapter_candidate": True},
                {"url": "two", "order": 2, "is_chapter_candidate": True},
            ],
        }
        gate = _build_download_gate(report)
        self.assertTrue(gate["passed"])
        self.assertEqual(gate["expected_viewer_images"], 2)
        self.assertEqual(gate["total_viewer_images"], 3)

    def test_viewer_snapshot_identifies_complete_manifest(self):
        snapshot = _viewer_image_snapshot(_FakeDriver())
        self.assertEqual(snapshot["image_count"], 3)
        self.assertTrue(snapshot["complete_manifest"])

    def test_bounded_teardown_returns_success_without_fallback(self):
        driver = _QuitDriver()
        fallback = mock.Mock()

        result = _bounded_driver_teardown(
            driver,
            {},
            timeout_seconds=0.1,
            cleanup_callback=fallback,
        )

        self.assertEqual(result["status"], "success")
        self.assertFalse(result["timeout_occurred"])
        self.assertEqual(driver.quit_calls, 1)
        fallback.assert_not_called()

    def test_bounded_teardown_records_quit_exception(self):
        def raise_quit_error():
            raise RuntimeError("sensitive details must not be persisted")

        driver = _QuitDriver(raise_quit_error)
        fallback = mock.Mock()
        result = _bounded_driver_teardown(
            driver,
            {},
            timeout_seconds=0.1,
            cleanup_callback=fallback,
        )

        self.assertEqual(result["status"], "exception")
        self.assertEqual(result["exception_type"], "RuntimeError")
        self.assertNotIn("sensitive details", json.dumps(result))
        fallback.assert_not_called()

    def test_bounded_teardown_timeout_returns_while_quit_is_blocked(self):
        release = threading.Event()
        started = threading.Event()

        def block_forever_for_test():
            started.set()
            release.wait()

        driver = _QuitDriver(block_forever_for_test)
        fallback = mock.Mock(
            return_value={
                "status": "completed",
                "matched_count": 2,
                "terminated_count": 2,
                "killed_count": 0,
                "skipped_ownership_unproven_count": 0,
                "remaining_count": 0,
            }
        )
        before = time.perf_counter()
        result = _bounded_driver_teardown(
            driver,
            {"service_pid": 10},
            timeout_seconds=0.03,
            cleanup_timeout_seconds=0.02,
            cleanup_callback=fallback,
        )
        elapsed = time.perf_counter() - before
        release.set()

        self.assertTrue(started.is_set())
        self.assertLess(elapsed, 0.5)
        self.assertEqual(result["status"], "timeout")
        self.assertTrue(result["timeout_occurred"])
        self.assertTrue(result["thread_daemon"])
        self.assertTrue(result["thread_alive_after_cleanup"])
        self.assertEqual(result["fallback_status"], "completed")
        fallback.assert_called_once()

    def test_bounded_teardown_records_fallback_failure(self):
        release = threading.Event()

        def block_until_released():
            release.wait()

        def failed_cleanup(*args, **kwargs):
            raise RuntimeError("cleanup internals must not be persisted")

        result = _bounded_driver_teardown(
            _QuitDriver(block_until_released),
            {"service_pid": 10},
            timeout_seconds=0.01,
            cleanup_timeout_seconds=0.01,
            cleanup_callback=failed_cleanup,
        )
        release.set()

        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["fallback_status"], "failed")
        self.assertEqual(result["fallback_exception_type"], "RuntimeError")
        self.assertNotIn("cleanup internals", json.dumps(result))

    def test_download_metadata_is_persisted_before_teardown_starts(self):
        events = []
        driver = mock.Mock()

        def fake_candidates(*args, **kwargs):
            report = args[6]
            report["download_gate"] = {"passed": True}
            report["download_valid"] = True
            events.append("download_gate")
            return ["page.png"]

        def fake_persist(_folder, report):
            self.assertEqual(report["teardown"]["status"], "pending")
            events.append("persist")

        def fake_teardown(*args, **kwargs):
            events.append("teardown")
            return {"status": "success", "timeout_occurred": False}

        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            with (
                mock.patch("down._create_driver", return_value=driver),
                mock.patch("down._capture_driver_ownership", return_value={}),
                mock.patch("down._refresh_driver_ownership"),
                mock.patch("down._viewer_image_snapshot", return_value={"image_count": 1, "urls": ["one"], "complete_manifest": True}),
                mock.patch("down._scroll_incrementally", return_value={}),
                mock.patch("down._collect_image_candidates", return_value=[{"url": "one"}]),
                mock.patch("down._dedupe_candidates", side_effect=lambda items: items),
                mock.patch("down._download_candidates", side_effect=fake_candidates),
                mock.patch("down._persist_download_metadata", side_effect=fake_persist),
                mock.patch("down._bounded_driver_teardown", side_effect=fake_teardown),
                mock.patch("down._write_download_report"),
                mock.patch("down.time.sleep"),
            ):
                result = download_images(
                    "https://example.test/chapter",
                    debug_folder=str(base / "debug"),
                    target_folder=str(base / "input"),
                    force=False,
                )

        self.assertEqual(result, ["page.png"])
        self.assertEqual(events, ["download_gate", "persist", "teardown"])

    def test_quit_exception_keeps_valid_download_metadata(self):
        def raise_quit_error():
            raise RuntimeError("quit failed")

        def fake_candidates(*args, **kwargs):
            report = args[6]
            report["download_gate"] = {"passed": True}
            report["download_valid"] = True
            return ["page.png"]

        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            with (
                mock.patch("down._create_driver", return_value=_QuitDriver(raise_quit_error)),
                mock.patch("down._capture_driver_ownership", return_value={}),
                mock.patch("down._refresh_driver_ownership"),
                mock.patch("down._viewer_image_snapshot", return_value={"image_count": 1, "urls": ["one"], "complete_manifest": True}),
                mock.patch("down._scroll_incrementally", return_value={}),
                mock.patch("down._collect_image_candidates", return_value=[{"url": "one"}]),
                mock.patch("down._dedupe_candidates", side_effect=lambda items: items),
                mock.patch("down._download_candidates", side_effect=fake_candidates),
                mock.patch("down._write_download_artifacts"),
                mock.patch("down.time.sleep"),
            ):
                download_images(
                    "https://example.test/chapter",
                    debug_folder=str(base / "debug"),
                    target_folder=str(base / "input"),
                    force=False,
                )
            report = json.loads(
                (base / "debug" / "downloaded_images.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertTrue(report["download_valid"])
        self.assertEqual(report["teardown"]["status"], "exception")
        self.assertEqual(report["teardown"]["exception_type"], "RuntimeError")

    def test_blocked_quit_keeps_metadata_and_returns_after_timeout(self):
        release = threading.Event()

        def block_quit():
            release.wait()

        def fake_candidates(*args, **kwargs):
            report = args[6]
            report["download_gate"] = {"passed": True}
            report["download_valid"] = True
            return ["page.png"]

        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            before = time.perf_counter()
            try:
                with (
                    mock.patch("down._create_driver", return_value=_QuitDriver(block_quit)),
                    mock.patch("down._capture_driver_ownership", return_value={}),
                    mock.patch("down._refresh_driver_ownership"),
                    mock.patch("down._viewer_image_snapshot", return_value={"image_count": 1, "urls": ["one"], "complete_manifest": True}),
                    mock.patch("down._scroll_incrementally", return_value={}),
                    mock.patch("down._collect_image_candidates", return_value=[{"url": "one"}]),
                    mock.patch("down._dedupe_candidates", side_effect=lambda items: items),
                    mock.patch("down._download_candidates", side_effect=fake_candidates),
                    mock.patch("down._write_download_artifacts"),
                    mock.patch("down.SELENIUM_QUIT_TIMEOUT_SECONDS", 0.02),
                    mock.patch("down.SELENIUM_CLEANUP_TIMEOUT_SECONDS", 0.01),
                    mock.patch("down.time.sleep"),
                ):
                    download_images(
                        "https://example.test/chapter",
                        debug_folder=str(base / "debug"),
                        target_folder=str(base / "input"),
                        force=False,
                    )
                elapsed = time.perf_counter() - before
                report = json.loads(
                    (base / "debug" / "downloaded_images.json").read_text(
                        encoding="utf-8"
                    )
                )
            finally:
                release.set()

        self.assertLess(elapsed, 0.5)
        self.assertTrue(report["download_valid"])
        self.assertEqual(report["teardown"]["status"], "timeout")
        self.assertEqual(report["teardown"]["fallback_status"], "skipped")

    def test_atomic_metadata_files_are_valid_json(self):
        with tempfile.TemporaryDirectory() as folder:
            report = {
                "download_gate": {"passed": True},
                "download_valid": True,
                "teardown": {"status": "pending"},
            }
            _persist_download_metadata(folder, report)

            downloaded = json.loads(
                (Path(folder) / "downloaded_images.json").read_text(encoding="utf-8")
            )
            audit = json.loads(
                (Path(folder) / "download_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(downloaded, report)
            self.assertEqual(audit, report)

    def test_owned_descendant_with_matching_identity_is_eligible(self):
        ownership = {
            "service_pid": 100,
            "service_create_time": 10.0,
            "profile_path": "/tmp/owned-profile",
            "known_processes": {
                101: {"create_time": 11.0, "profile_path": "/tmp/owned-profile"}
            },
        }
        snapshot = {
            "pid": 101,
            "create_time": 11.0,
            "profile_path": "/tmp/owned-profile",
        }
        self.assertTrue(
            _process_matches_ownership(snapshot, ownership, descendant_pids={101})
        )

    def test_driver_ownership_captures_service_identity_and_profile(self):
        profile = str(Path(tempfile.gettempdir()) / "owned-profile")
        service = _FakeProcess(
            100,
            1,
            10.0,
            "chromedriver.exe",
            "chromedriver.exe",
            ["chromedriver.exe"],
        )
        child = _FakeProcess(
            101,
            100,
            11.0,
            "chrome.exe",
            "chrome.exe",
            ["chrome.exe", f"--user-data-dir={profile}"],
        )
        service._children = [child]
        process_api = _FakeProcessApi([service, child])
        driver = SimpleNamespace(
            service=SimpleNamespace(process=SimpleNamespace(pid=100))
        )

        ownership = _capture_driver_ownership(driver, process_api=process_api)

        self.assertEqual(ownership["service_pid"], 100)
        self.assertEqual(ownership["service_create_time"], 10.0)
        self.assertEqual(
            ownership["profile_path"],
            os.path.normcase(os.path.normpath(profile)),
        )
        self.assertIn(101, ownership["known_processes"])

    def test_normal_user_chrome_is_not_owned(self):
        ownership = {
            "service_pid": 100,
            "service_create_time": 10.0,
            "profile_path": "/tmp/owned-profile",
            "known_processes": {},
        }
        snapshot = {
            "pid": 500,
            "create_time": 20.0,
            "profile_path": "/home/user/default-profile",
        }
        self.assertFalse(
            _process_matches_ownership(snapshot, ownership, descendant_pids=set())
        )

    def test_reused_pid_with_different_create_time_is_not_owned(self):
        ownership = {
            "service_pid": 100,
            "service_create_time": 10.0,
            "profile_path": "/tmp/owned-profile",
            "known_processes": {
                101: {"create_time": 11.0, "profile_path": "/tmp/owned-profile"}
            },
        }
        reused = {
            "pid": 101,
            "create_time": 99.0,
            "profile_path": "/tmp/owned-profile",
        }
        self.assertFalse(
            _process_matches_ownership(reused, ownership, descendant_pids={101})
        )

    def test_unknown_ownership_is_never_eligible(self):
        snapshot = {
            "pid": 101,
            "create_time": 11.0,
            "profile_path": "/tmp/owned-profile",
        }
        self.assertFalse(
            _process_matches_ownership(snapshot, {}, descendant_pids={101})
        )

    def test_selective_cleanup_terminates_only_owned_fake_processes(self):
        profile = str(Path(tempfile.gettempdir()) / "owned-profile")
        service = _FakeProcess(
            100,
            1,
            10.0,
            "chromedriver.exe",
            "chromedriver.exe",
            ["chromedriver.exe"],
        )
        owned_chrome = _FakeProcess(
            101,
            100,
            11.0,
            "chrome.exe",
            "chrome.exe",
            ["chrome.exe", f"--user-data-dir={profile}"],
        )
        user_chrome = _FakeProcess(
            500,
            1,
            5.0,
            "chrome.exe",
            "chrome.exe",
            ["chrome.exe", "--user-data-dir=user-default"],
        )
        service._children = [owned_chrome]
        process_api = _FakeProcessApi([service, owned_chrome, user_chrome])
        ownership = {
            "service_pid": 100,
            "service_create_time": 10.0,
            "profile_path": profile,
            "known_processes": {
                100: {
                    "create_time": 10.0,
                    "name": "chromedriver.exe",
                    "executable": "chromedriver.exe",
                    "profile_path": "",
                },
                101: {
                    "create_time": 11.0,
                    "name": "chrome.exe",
                    "executable": "chrome.exe",
                    "profile_path": profile,
                },
            },
        }

        from down import _cleanup_owned_processes

        result = _cleanup_owned_processes(
            ownership,
            timeout_seconds=0.01,
            process_api=process_api,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["matched_count"], 2)
        self.assertEqual(result["terminated_count"], 2)
        self.assertEqual(owned_chrome.terminate_calls, 1)
        self.assertEqual(service.terminate_calls, 1)
        self.assertEqual(user_chrome.terminate_calls, 0)
        self.assertEqual(user_chrome.kill_calls, 0)

    def test_selective_cleanup_escalates_only_revalidated_survivors(self):
        profile = str(Path(tempfile.gettempdir()) / "owned-profile")
        service = _FakeProcess(
            100,
            1,
            10.0,
            "chromedriver.exe",
            "chromedriver.exe",
            ["chromedriver.exe"],
            survives_terminate=True,
        )
        owned_chrome = _FakeProcess(
            101,
            100,
            11.0,
            "chrome.exe",
            "chrome.exe",
            ["chrome.exe", f"--user-data-dir={profile}"],
            survives_terminate=True,
        )
        user_chrome = _FakeProcess(
            500,
            1,
            5.0,
            "chrome.exe",
            "chrome.exe",
            ["chrome.exe", "--user-data-dir=user-default"],
            survives_terminate=True,
        )
        service._children = [owned_chrome]
        process_api = _FakeProcessApi([service, owned_chrome, user_chrome])
        ownership = {
            "service_pid": 100,
            "service_create_time": 10.0,
            "profile_path": profile,
            "known_processes": {
                100: {
                    "create_time": 10.0,
                    "name": "chromedriver.exe",
                    "executable": "chromedriver.exe",
                    "profile_path": "",
                },
                101: {
                    "create_time": 11.0,
                    "name": "chrome.exe",
                    "executable": "chrome.exe",
                    "profile_path": profile,
                },
            },
        }

        from down import _cleanup_owned_processes

        result = _cleanup_owned_processes(
            ownership,
            timeout_seconds=0.01,
            process_api=process_api,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["terminated_count"], 0)
        self.assertEqual(result["killed_count"], 2)
        self.assertEqual(owned_chrome.kill_calls, 1)
        self.assertEqual(service.kill_calls, 1)
        self.assertEqual(user_chrome.terminate_calls, 0)
        self.assertEqual(user_chrome.kill_calls, 0)

    def test_legacy_report_without_teardown_remains_writable(self):
        with tempfile.TemporaryDirectory() as folder:
            legacy = {"download_gate": {"passed": True}, "downloaded": []}
            _persist_download_metadata(folder, legacy)
            persisted = json.loads(
                (Path(folder) / "downloaded_images.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted, legacy)

    def test_teardown_fields_do_not_invalidate_download_cache_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "001.png"
            Image.new("RGB", (800, 80), "white").save(path, "PNG")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest = {
                "teardown": {"status": "timeout", "fallback_status": "completed"},
                "downloaded": [
                    {
                        "url": "https://webtoon-phinf.pstatic.net/chapter/001.png",
                        "path": str(path),
                        "sha256": digest,
                        "is_chapter_candidate": True,
                    }
                ],
            }
            self.assertEqual(_valid_download_paths(manifest), [str(path)])


if __name__ == "__main__":
    unittest.main()
