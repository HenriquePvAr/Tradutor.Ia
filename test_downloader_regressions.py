from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

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
from benchmark_pipeline import _download_cache_reuse_allowed, _valid_download_paths
import chapter_source
import down
from chapter_source import GenericImageChapterAdapter, REVIEW_REQUIRED_MEDIUM_CONFIDENCE, SourceError
from download_transport import DownloadLimits, LimitExceeded, RequestsTransport

from down import (
    _bounded_driver_teardown,
    _build_download_gate,
    _capture_driver_ownership,
    _candidate_skip_reason,
    _download_candidates,
    _persist_download_metadata,
    _process_matches_ownership,
    _selected_universal_candidates,
    _validate_image_bytes,
    _viewer_image_snapshot,
    analyze_chapter_source,
    download_images,
)


class _FakeDriver:
    def execute_script(self, _script, *_args):
        return {
            "imageCount": 3,
            "urls": ["https://example.test/1.jpg", "https://example.test/2.jpg", "https://example.test/3.jpg"],
        }


class _QuitDriver:
    def __init__(self, action=None):
        self.action = action
        self.quit_calls = 0
        self.current_url = "https://www.webtoons.com/en/fantasy/serie/ep-1/viewer"

    def get(self, _url):
        return None

    def quit(self):
        self.quit_calls += 1
        if self.action:
            self.action()


def _accepted_source_analysis(*, count=1, outcome="supported_specific_adapter", warnings=()):
    """Small, offline adapter-analysis fixture used by downloader contract tests."""
    accepted = [
        SimpleNamespace(
            id=f"page-{index:03}",
            url=f"https://webtoon-phinf.pstatic.net/chapter/{index:03}.png",
            source="data-src",
            order=index,
            y=index * 1200,
            width=800,
            height=1200,
            natural_width=800,
            natural_height=1200,
            container="reader",
            canvas_data=b"",
            origin="dom",
        )
        for index in range(1, count + 1)
    ]
    return SimpleNamespace(
        adapter="webtoons",
        outcome=outcome,
        confidence=1.0,
        accepted=accepted,
        warnings=list(warnings),
        can_download=True,
        public=lambda: {
            "adapter": "webtoons",
            "outcome": outcome,
            "confidence": 1.0,
            "accepted_count": len(accepted),
            "warnings": list(warnings),
        },
    )


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
    def test_local_driver_keeps_chrome_sandbox_enabled(self):
        class OptionsProbe:
            def __init__(self):
                self.arguments = []

            def add_argument(self, value):
                self.arguments.append(value)

        options = OptionsProbe()
        with mock.patch.object(down, "Options", return_value=options), \
             mock.patch.object(down, "CHROMEDRIVER_PATH", "C:/local/chromedriver.exe"), \
             mock.patch.object(down.os.path, "isfile", return_value=True), \
             mock.patch.object(down, "Service", return_value=object()), \
             mock.patch.object(down.webdriver, "Chrome", return_value=object()):
            down._create_driver()
        self.assertNotIn("--no-sandbox", options.arguments)

    def test_local_driver_enables_performance_logging_when_supported(self):
        class OptionsProbe:
            def __init__(self):
                self.arguments = []
                self.capabilities = []

            def add_argument(self, value):
                self.arguments.append(value)

            def set_capability(self, name, value):
                self.capabilities.append((name, value))

        options = OptionsProbe()
        with mock.patch.object(down, "Options", return_value=options), \
             mock.patch.object(down, "CHROMEDRIVER_PATH", "C:/local/chromedriver.exe"), \
             mock.patch.object(down.os.path, "isfile", return_value=True), \
             mock.patch.object(down, "Service", return_value=object()), \
             mock.patch.object(down.webdriver, "Chrome", return_value=object()):
            down._create_driver()

        self.assertIn(("goog:loggingPrefs", {"performance": "ALL"}), options.capabilities)

    def test_viewer_snapshot_includes_browser_and_lazy_image_sources(self):
        class Driver:
            script = ""

            def execute_script(self, script, *_args):
                self.script = script
                return {
                    "imageCount": 3,
                    "urls": [
                        "https://reader.test/current.webp",
                        "https://reader.test/declared.webp",
                        "https://reader.test/lazy.webp",
                    ],
                }

        driver = Driver()
        snapshot = _viewer_image_snapshot(driver)

        self.assertEqual(snapshot["image_count"], 3)
        self.assertTrue(snapshot["complete_manifest"])
        self.assertIn("el.currentSrc", driver.script)
        self.assertIn("el.src", driver.script)
        self.assertIn("data-original-src", driver.script)
        self.assertIn("data-lazyload", driver.script)

    def test_specific_adapter_download_uses_accepted_manifest_and_report_metadata(self):
        analysis = _accepted_source_analysis()
        analyse = mock.Mock(return_value=analysis)
        adapter = SimpleNamespace(
            name="specific-reader",
            adapter_version="7",
            is_specific=True,
            validate_url=lambda _url: None,
            validate_path=lambda _url: None,
            normalize_url=lambda value: value,
            validate_redirect=lambda _url: None,
            analyze=analyse,
        )
        driver = _QuitDriver()
        captured = {}

        class Transport:
            name = "requests"

            @staticmethod
            def close():
                return None

        def fake_download(*args, **_kwargs):
            captured["candidates"] = args[1]
            report = args[6]
            captured["report"] = report
            report["download_gate"] = {"passed": True}
            report["download_valid"] = True
            return ["page.png"]

        with tempfile.TemporaryDirectory() as folder:
            with (
                mock.patch("chapter_source.select_adapter", return_value=adapter),
                mock.patch("down.preflight_browser_navigation", side_effect=lambda _adapter, value: value),
                mock.patch("down._create_driver", return_value=driver),
                mock.patch("down._capture_driver_ownership", return_value={}),
                mock.patch("down._refresh_driver_ownership"),
                mock.patch("down._bounded_driver_teardown", return_value={"status": "success", "timeout_occurred": False}),
                mock.patch("down._viewer_image_snapshot", return_value={"image_count": 1, "urls": ["one"], "complete_manifest": True}),
                mock.patch("down._scroll_incrementally", return_value={"reached_document_end": True, "stabilized": True}),
                mock.patch("down._collect_image_candidates", side_effect=AssertionError("legacy collector must not run")),
                mock.patch("down._dedupe_candidates", side_effect=lambda values: values),
                mock.patch("download_transport.build_transports", return_value=[Transport()]),
                mock.patch("down._download_candidates", side_effect=fake_download),
                mock.patch("down._persist_download_metadata"),
                mock.patch("down._write_download_report"),
                mock.patch("down.time.sleep"),
            ):
                result = download_images(
                    "https://specific.test/chapter/1",
                    debug_folder=str(Path(folder) / "debug"),
                    target_folder=str(Path(folder) / "input"),
                    force=False,
                )

        self.assertEqual(result, ["page.png"])
        self.assertEqual(analyse.call_args.kwargs["extra_warnings"], ())
        self.assertEqual(captured["candidates"][0]["candidate_id"], "page-001")
        self.assertEqual(captured["report"]["source_type"], "url")
        self.assertEqual(captured["report"]["adapter_name"], "specific-reader")
        self.assertEqual(captured["report"]["adapter_version"], "7")
        self.assertEqual(captured["report"]["transport_metadata"], {"configured": ["requests"], "count": 1})
        # The mocked downloader did not fetch a page.  Configuration order must not
        # be reported as if it were observed run provenance.
        self.assertEqual(captured["report"]["transport_name"], "none")

    def test_specific_adapter_incomplete_scroll_fails_before_transport(self):
        analysis = _accepted_source_analysis()
        analyse = mock.Mock(return_value=analysis)
        adapter = SimpleNamespace(
            name="specific-reader", adapter_version="1", is_specific=True,
            validate_url=lambda _url: None, validate_path=lambda _url: None,
            normalize_url=lambda value: value, validate_redirect=lambda _url: None,
            analyze=analyse,
        )
        transport_builder = mock.Mock()
        with (
            mock.patch("chapter_source.select_adapter", return_value=adapter),
            mock.patch("down.preflight_browser_navigation", side_effect=lambda _adapter, value: value),
            mock.patch("down._create_driver", return_value=_QuitDriver()),
            mock.patch("down._capture_driver_ownership", return_value={}),
            mock.patch("down._refresh_driver_ownership"),
            mock.patch("down._bounded_driver_teardown", return_value={"status": "success", "timeout_occurred": False}),
            mock.patch("down._viewer_image_snapshot", return_value={"image_count": 1, "urls": ["one"], "complete_manifest": True}),
            mock.patch("down._scroll_incrementally", return_value={"reached_document_end": False, "stabilized": False}),
            mock.patch("download_transport.build_transports", transport_builder),
            mock.patch("down.time.sleep"),
        ):
            with self.assertRaises(SourceError) as ctx:
                download_images("https://specific.test/chapter/1", force=False)

        self.assertEqual(ctx.exception.code, "incomplete_download")
        self.assertEqual(analyse.call_args.kwargs["extra_warnings"], ("scroll_incomplete",))
        transport_builder.assert_not_called()

    def test_specific_adapter_over_400_accepted_pages_fails_before_transport(self):
        analysis = _accepted_source_analysis(count=401)
        adapter = SimpleNamespace(
            name="specific-reader", adapter_version="1", is_specific=True,
            validate_url=lambda _url: None, validate_path=lambda _url: None,
            normalize_url=lambda value: value, validate_redirect=lambda _url: None,
            analyze=mock.Mock(return_value=analysis),
        )
        transport_builder = mock.Mock()
        with (
            mock.patch("chapter_source.select_adapter", return_value=adapter),
            mock.patch("down.preflight_browser_navigation", side_effect=lambda _adapter, value: value),
            mock.patch("down._create_driver", return_value=_QuitDriver()),
            mock.patch("down._capture_driver_ownership", return_value={}),
            mock.patch("down._refresh_driver_ownership"),
            mock.patch("down._bounded_driver_teardown", return_value={"status": "success", "timeout_occurred": False}),
            mock.patch("down._viewer_image_snapshot", return_value={"image_count": 401, "urls": ["one"], "complete_manifest": True}),
            mock.patch("down._scroll_incrementally", return_value={"reached_document_end": True, "stabilized": True}),
            mock.patch("download_transport.build_transports", transport_builder),
            mock.patch("down.time.sleep"),
        ):
            with self.assertRaises(SourceError) as ctx:
                download_images("https://specific.test/chapter/1", force=False)

        self.assertEqual(ctx.exception.code, "incomplete_download")
        transport_builder.assert_not_called()

    def test_confirmed_medium_confidence_selection_can_continue_with_fresh_subset(self):
        selected = _selected_universal_candidates(
            SimpleNamespace(
                outcome=REVIEW_REQUIRED_MEDIUM_CONFIDENCE,
                can_download=False,
                accepted=[SimpleNamespace(id="page-a"), SimpleNamespace(id="page-b")],
            ),
            ["page-b"],
        )
        self.assertEqual([candidate.id for candidate in selected], ["page-b"])

    def test_medium_confidence_without_confirmation_stops_before_download(self):
        with self.assertRaises(SourceError) as ctx:
            _selected_universal_candidates(
                SimpleNamespace(
                    outcome=REVIEW_REQUIRED_MEDIUM_CONFIDENCE,
                    can_download=False,
                    accepted=[SimpleNamespace(id="page-a")],
                ),
                [],
            )
        self.assertEqual(ctx.exception.code, REVIEW_REQUIRED_MEDIUM_CONFIDENCE)

    def test_generic_gate_uses_confirmed_cluster_not_all_dom_images(self):
        report = {
            "viewer_urls": ["ad", "chosen-a", "chosen-b"],
            "expected_chapter_urls": ["chosen-a", "chosen-b"],
            "downloaded": [
                {"url": "chosen-a", "order": 1, "is_chapter_candidate": True},
                {"url": "chosen-b", "order": 2, "is_chapter_candidate": True},
            ],
        }
        gate = _build_download_gate(report)
        self.assertTrue(gate["passed"])
        self.assertEqual(gate["expected_viewer_images"], 2)

    def test_generic_gate_uses_opaque_ids_so_signed_path_collisions_cannot_hide_missing_pages(self):
        report = {
            "expected_chapter_candidate_ids": ["page-a", "page-b"],
            # Both remote URLs intentionally have the same path fingerprint. The gate must
            # still identify the unfulfilled second selected candidate.
            "downloaded": [
                {"candidate_id": "page-a", "url": "https://cdn.test/abcdef",
                 "order": 1, "is_chapter_candidate": True},
            ],
        }
        gate = _build_download_gate(report)
        self.assertFalse(gate["passed"])
        self.assertEqual(gate["missing_candidate_id_samples"], ["page-b"])

    def test_source_error_writes_only_a_coded_failure_report(self):
        with tempfile.TemporaryDirectory() as folder:
            debug = Path(folder) / "debug"
            with (
                mock.patch("down.preflight_browser_navigation",
                           side_effect=SourceError("challenge_required", "https://bad/?secret")),
                mock.patch.object(chapter_source.socket, "getaddrinfo",
                                  return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]),
            ):
                with self.assertRaises(SourceError):
                    download_images(
                        "https://www.webtoons.com/en/fantasy/serie/ep-1/viewer?token=secret",
                        debug_folder=str(debug), target_folder=str(Path(folder) / "input"),
                    )
            report = json.loads((debug / "downloaded_images.json").read_text(encoding="utf-8"))
        self.assertEqual(report["failure"], {"code": "challenge_required"})
        self.assertNotIn("secret", json.dumps(report))

    def test_canvas_capture_charges_the_shared_transport_budget(self):
        image = Image.new("RGB", (800, 1200), "navy")
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        transport = RequestsTransport(
            GenericImageChapterAdapter(name="reader", allowed_hosts=("reader.test",)),
            limits=DownloadLimits(max_bytes_per_file=20),
        )
        self.addCleanup(transport.close)
        report = {"viewer_image_count": 0, "ignored": [], "downloaded": [],
                  "timings": {"download_seconds": 0.0, "validation_seconds": 0.0,
                              "image_save_seconds": 0.0}}
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(LimitExceeded):
                _download_candidates(
                    None, [{"url": "canvas://oversize", "canvas_data": buffer.getvalue(),
                            "source": "canvas_capture", "order": 1, "width": 800, "height": 1200,
                            "isChapterCandidate": True}],
                    None, 1, None, None, report, "https://reader.test/chapter/1", folder,
                    transports=[transport],
                )

    def test_generic_source_never_uses_download_cache_as_reader_review(self):
        with mock.patch("benchmark_pipeline.config.ENABLE_DOWNLOAD_CACHE", True):
            self.assertFalse(_download_cache_reuse_allowed(
                "https://reader.example.test/chapter/1", force=False, approved_ids=[]))
            self.assertFalse(_download_cache_reuse_allowed(
                "https://www.webtoons.com/en/viewer", force=False, approved_ids=["opaque"]))
            self.assertTrue(_download_cache_reuse_allowed(
                "https://www.webtoons.com/en/viewer", force=False, approved_ids=[]))

    def test_preanalysis_rejects_invalid_browser_destination_before_scroll(self):
        class Driver:
            current_url = "file:///C:/private.html"

            @staticmethod
            def get(_url):
                return None

            @staticmethod
            def execute_script(*_args):
                raise AssertionError("scroll or DOM analysis must not run")

            @staticmethod
            def quit():
                return None

        with (
            mock.patch("down.preflight_browser_navigation", return_value="https://reader.example.test/chapter/1"),
            mock.patch("down._create_driver", return_value=Driver()),
            mock.patch("down._capture_driver_ownership", return_value={}),
            mock.patch("down._refresh_driver_ownership"),
            mock.patch("down._bounded_driver_teardown", return_value={}),
            mock.patch.object(chapter_source.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]),
        ):
            with self.assertRaises(SourceError):
                analyze_chapter_source("https://reader.example.test/chapter/1")

    def test_specific_adapter_preanalysis_applies_scroll_coverage_protection(self):
        analyse = mock.Mock(return_value=SimpleNamespace(
            outcome="incomplete_download", can_download=False, accepted=[],
            warnings=["scroll_incomplete"],
        ))
        adapter = SimpleNamespace(
            is_specific=True,
            validate_url=lambda _url: None,
            validate_path=lambda _url: None,
            normalize_url=lambda value: value,
            validate_redirect=lambda _url: None,
            analyze=analyse,
        )
        driver = _QuitDriver()
        with (
            mock.patch("chapter_source.select_adapter", return_value=adapter),
            mock.patch("down.preflight_browser_navigation", return_value="https://known.example.test/chapter/1"),
            mock.patch("down._create_driver", return_value=driver),
            mock.patch("down._capture_driver_ownership", return_value={}),
            mock.patch("down._refresh_driver_ownership"),
            mock.patch("down._bounded_driver_teardown", return_value={}),
            mock.patch("down._scroll_incrementally", return_value={"reached_document_end": False, "stabilized": False}),
            mock.patch("down._load_source_profile", return_value=None),
            mock.patch("down.time.sleep"),
        ):
            result = analyze_chapter_source("https://known.example.test/chapter/1")

        self.assertEqual(result.outcome, "incomplete_download")
        self.assertEqual(analyse.call_args.kwargs["extra_warnings"], ("scroll_incomplete",))

    def test_generic_adapter_preanalysis_keeps_scroll_coverage_protection(self):
        result = SimpleNamespace(
            outcome="incomplete_download", can_download=False, accepted=[],
            warnings=["scroll_incomplete"],
        )
        analyse = mock.Mock(return_value=result)
        adapter = SimpleNamespace(
            is_specific=False,
            validate_url=lambda _url: None,
            validate_path=lambda _url: None,
            normalize_url=lambda value: value,
            validate_redirect=lambda _url: None,
            analyze=analyse,
        )
        driver = _QuitDriver()
        with (
            mock.patch("chapter_source.select_adapter", return_value=adapter),
            mock.patch("down.preflight_browser_navigation", return_value="https://reader.example.test/chapter/1"),
            mock.patch("down._create_driver", return_value=driver),
            mock.patch("down._capture_driver_ownership", return_value={}),
            mock.patch("down._refresh_driver_ownership"),
            mock.patch("down._bounded_driver_teardown", return_value={}),
            mock.patch("down._scroll_incrementally", return_value={"reached_document_end": False, "stabilized": False}),
            mock.patch("down._load_source_profile", return_value=None),
            mock.patch("down.time.sleep"),
        ):
            self.assertIs(analyze_chapter_source("https://reader.example.test/chapter/1"), result)

        self.assertEqual(analyse.call_args.kwargs["extra_warnings"], ("scroll_incomplete",))

    def test_canvas_capture_is_saved_without_a_network_transport(self):
        image = Image.new("RGB", (800, 1200), "navy")
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        with tempfile.TemporaryDirectory() as folder:
            report = {
                "viewer_image_count": 0, "ignored": [], "downloaded": [],
                "timings": {"download_seconds": 0.0, "validation_seconds": 0.0,
                            "image_save_seconds": 0.0},
            }
            paths = _download_candidates(
                None,
                [{"url": "canvas://opaque", "canvas_data": buffer.getvalue(),
                  "source": "canvas_capture", "order": 1, "width": 800, "height": 1200,
                  "isChapterCandidate": True}],
                None, 1, None, None, report, "https://reader.example.test/chapter/1",
                folder, transports=[],
            )
            self.assertEqual(len(paths), 1)
            self.assertTrue(Path(paths[0]).is_file())
            self.assertEqual(report["downloaded"][0]["url"], "canvas:opaque")
            self.assertEqual(report["downloaded"][0]["transport_name"], "canvas")
            self.assertEqual(report["transport_name"], "canvas")

    def test_download_report_records_the_fallback_that_actually_saved_the_page(self):
        image = Image.new("RGB", (800, 1200), "navy")
        buffer = io.BytesIO()
        image.save(buffer, "PNG")

        class DeniedTransport:
            name = "requests"

            @staticmethod
            def fetch(_url, *, referer=""):
                raise SourceError("invalid_image_response", "offline_fixture")

        class BrowserFallback:
            name = "browser_session"

            @staticmethod
            def fetch(_url, *, referer=""):
                return SimpleNamespace(content=buffer.getvalue())

        report = {
            "viewer_image_count": 0,
            "ignored": [],
            "downloaded": [],
            "transport_metadata": {"configured": ["requests", "browser_session"], "count": 2},
            "transport_name": "none",
            "timings": {"download_seconds": 0.0, "validation_seconds": 0.0,
                        "image_save_seconds": 0.0},
        }
        with tempfile.TemporaryDirectory() as folder:
            paths = _download_candidates(
                None,
                [{"candidate_id": "page-001", "url": "https://reader.example.test/page.png",
                  "source": "fixture", "order": 1, "width": 800, "height": 1200,
                  "isChapterCandidate": True}],
                None, 1, None, None, report, "https://reader.example.test/chapter/1",
                folder, transports=[DeniedTransport(), BrowserFallback()],
            )

        self.assertEqual(len(paths), 1)
        self.assertEqual(report["transport_metadata"]["configured"], ["requests", "browser_session"])
        self.assertEqual(report["downloaded"][0]["transport_name"], "browser_session")
        self.assertEqual(report["transport_name"], "browser_session")
        self.assertEqual(report["transport_usage"], {
            "successful": ["browser_session"],
            "counts": {"browser_session": 1},
            "count": 1,
        })

    def test_download_report_records_specific_http_failure_for_ignored_page(self):
        class DeniedTransport:
            name = "requests"

            @staticmethod
            def fetch(_url, *, referer=""):
                raise SourceError("source_access_denied", "403")

        report = {
            "viewer_image_count": 0,
            "ignored": [],
            "downloaded": [],
            "transport_metadata": {"configured": ["requests"], "count": 1},
            "transport_name": "none",
            "timings": {"download_seconds": 0.0, "validation_seconds": 0.0,
                        "image_save_seconds": 0.0},
        }
        with tempfile.TemporaryDirectory() as folder:
            paths = _download_candidates(
                None,
                [{"candidate_id": "page-001", "url": "https://reader.example.test/page.png?sig=1",
                  "source": "currentSrc", "order": 1, "width": 800, "height": 1200,
                  "isChapterCandidate": True}],
                None, 1, 1, None, report, "https://reader.example.test/chapter/1",
                folder, transports=[DeniedTransport()],
            )

        self.assertEqual(paths, [])
        item = report["ignored"][0]
        self.assertEqual(item["candidate_id"], "page-001")
        self.assertEqual(item["reason"], "http_403")
        self.assertEqual(item["reason_code"], "http_403")
        self.assertEqual(item["status"], 403)
        self.assertEqual(item["transport"], "requests")
        self.assertEqual(item["host"], "reader.example.test")
        self.assertTrue(item["query_preserved"])
        self.assertTrue(item["referer_sent"])
        self.assertTrue(item["user_agent_sent"])

    def test_download_report_records_empty_response_diagnostics(self):
        class EmptyTransport:
            name = "browser_session"

            @staticmethod
            def fetch(url, *, referer=""):
                return SimpleNamespace(
                    content=b"",
                    content_type="image/png",
                    final_url=url,
                    status=200,
                )

        report = {
            "viewer_image_count": 0,
            "ignored": [],
            "downloaded": [],
            "transport_metadata": {"configured": ["browser_session"], "count": 1},
            "transport_name": "none",
            "timings": {"download_seconds": 0.0, "validation_seconds": 0.0,
                        "image_save_seconds": 0.0},
        }
        with tempfile.TemporaryDirectory() as folder:
            paths = _download_candidates(
                None,
                [{"candidate_id": "page-002", "url": "https://reader.example.test/empty.png",
                  "source": "currentSrc", "order": 2, "width": 800, "height": 1200,
                  "isChapterCandidate": True}],
                None, 1, 1, None, report, "https://reader.example.test/chapter/1",
                folder, transports=[EmptyTransport()],
            )

        self.assertEqual(paths, [])
        item = report["ignored"][0]
        self.assertEqual(item["reason_code"], "empty_response")
        self.assertEqual(item["status"], 200)
        self.assertEqual(item["content_type"], "image/png")
        self.assertEqual(item["bytes_received"], 0)
        self.assertTrue(item["cookies_possible"])

    def test_duplicate_image_bytes_are_not_saved_twice(self):
        image = Image.new("RGB", (800, 1200), "navy")
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        with tempfile.TemporaryDirectory() as folder:
            report = {
                "viewer_image_count": 0, "ignored": [], "downloaded": [],
                "timings": {"download_seconds": 0.0, "validation_seconds": 0.0,
                            "image_save_seconds": 0.0},
            }
            paths = _download_candidates(
                None,
                [
                    {"url": "canvas://one", "canvas_data": buffer.getvalue(), "source": "canvas_capture", "order": 1,
                     "width": 800, "height": 1200, "isChapterCandidate": True},
                    {"url": "canvas://two", "canvas_data": buffer.getvalue(), "source": "canvas_capture", "order": 2,
                     "width": 800, "height": 1200, "isChapterCandidate": True},
                ],
                None, 1, None, None, report, "https://reader.example.test/chapter/1", folder, transports=[],
            )
            self.assertEqual(len(paths), 1)
            self.assertIn("duplicate_image_bytes", [item["reason"] for item in report["ignored"]])

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
        driver.current_url = "https://www.webtoons.com/en/fantasy/serie/ep-1/viewer"

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
                mock.patch("down._scroll_incrementally", return_value={"reached_document_end": True, "stabilized": True}),
                mock.patch.object(chapter_source.WEBTOONS, "analyze", return_value=_accepted_source_analysis()),
                mock.patch("down._collect_image_candidates", side_effect=AssertionError("legacy collector must not run")),
                mock.patch("down._dedupe_candidates", side_effect=lambda items: items),
                mock.patch("down._download_candidates", side_effect=fake_candidates),
                mock.patch("download_transport.build_transports", return_value=[]),
                mock.patch("down.preflight_browser_navigation", side_effect=lambda _adapter, request_url: request_url),
                mock.patch("down._persist_download_metadata", side_effect=fake_persist),
                mock.patch("down._bounded_driver_teardown", side_effect=fake_teardown),
                mock.patch("down._write_download_report"),
                mock.patch("down.time.sleep"),
            ):
                result = download_images(
                    "https://www.webtoons.com/en/fantasy/serie/ep-1/viewer",
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
                mock.patch("down._scroll_incrementally", return_value={"reached_document_end": True, "stabilized": True}),
                mock.patch.object(chapter_source.WEBTOONS, "analyze", return_value=_accepted_source_analysis()),
                mock.patch("down._collect_image_candidates", side_effect=AssertionError("legacy collector must not run")),
                mock.patch("down._dedupe_candidates", side_effect=lambda items: items),
                mock.patch("down._download_candidates", side_effect=fake_candidates),
                mock.patch("download_transport.build_transports", return_value=[]),
                mock.patch("down.preflight_browser_navigation", side_effect=lambda _adapter, request_url: request_url),
                mock.patch("down._write_download_artifacts"),
                mock.patch("down.time.sleep"),
            ):
                download_images(
                    "https://www.webtoons.com/en/fantasy/serie/ep-1/viewer",
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
            try:
                with (
                    mock.patch("down._create_driver", return_value=_QuitDriver(block_quit)),
                    mock.patch("down._capture_driver_ownership", return_value={}),
                    mock.patch("down._refresh_driver_ownership"),
                    mock.patch("down._viewer_image_snapshot", return_value={"image_count": 1, "urls": ["one"], "complete_manifest": True}),
                    mock.patch("down._scroll_incrementally", return_value={"reached_document_end": True, "stabilized": True}),
                    mock.patch.object(chapter_source.WEBTOONS, "analyze", return_value=_accepted_source_analysis()),
                    mock.patch("down._collect_image_candidates", side_effect=AssertionError("legacy collector must not run")),
                    mock.patch("down._dedupe_candidates", side_effect=lambda items: items),
                    mock.patch("down._download_candidates", side_effect=fake_candidates),
                    mock.patch("download_transport.build_transports", return_value=[]),
                    mock.patch("down.preflight_browser_navigation", side_effect=lambda _adapter, request_url: request_url),
                    mock.patch("down._write_download_artifacts"),
                    mock.patch("down.SELENIUM_QUIT_TIMEOUT_SECONDS", 0.02),
                    mock.patch("down.SELENIUM_CLEANUP_TIMEOUT_SECONDS", 0.01),
                    mock.patch("down.time.sleep"),
                ):
                    download_images(
                        "https://www.webtoons.com/en/fantasy/serie/ep-1/viewer",
                        debug_folder=str(base / "debug"),
                        target_folder=str(base / "input"),
                        force=False,
                    )
                report = json.loads(
                    (base / "debug" / "downloaded_images.json").read_text(
                        encoding="utf-8"
                    )
                )
            finally:
                release.set()

        # The functional teardown result is the deterministic proof that the
        # blocked quit was interrupted by the timeout: status "timeout" is set
        # only when the quit thread did not finish within the quit budget.
        self.assertTrue(report["download_valid"])
        self.assertEqual(report["teardown"]["status"], "timeout")
        self.assertEqual(report["teardown"]["fallback_status"], "skipped")
        # Bound the internally-measured teardown duration (quit-wait + cleanup),
        # not the whole download wall clock: the latter also includes temp-dir
        # I/O and metadata writes and is sensitive to shared-runner load, which
        # is what made the old clock assertion flaky on CI. The margin over the
        # mocked quit/cleanup budgets (0.02 + 0.01) absorbs thread spawn/join and
        # OS scheduler jitter; a broken timeout would hang indefinitely and an
        # abnormal cleanup would blow far past this bound.
        quit_budget = 0.02
        cleanup_budget = 0.01
        self.assertLess(
            report["teardown"]["duration_seconds"],
            quit_budget + cleanup_budget + 0.5,
        )

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


class PaginatedReaderSafetyTests(unittest.TestCase):
    PAGE_ONE = "https://reader.example.test/chapter/one"
    PAGE_TWO = "https://reader.example.test/chapter/one?page=2"

    @staticmethod
    def _analysis(page_number, *, can_download=True):
        candidate = SimpleNamespace(
            id=f"page-{page_number}",
            url=f"https://cdn.example.test/chapter/one-{page_number}.webp",
            source="currentSrc",
            order=page_number,
            y=page_number * 1000,
            width=900,
            height=1400,
            natural_width=900,
            natural_height=1400,
            container="chapter reader",
            class_name="",
            element_id="",
            alt="",
            context="chapter reader",
            network_order=-1,
            content_type="image/webp",
            origin="dom",
            visible=True,
            attribute_names=(),
            canvas_data=b"",
        )
        return SimpleNamespace(
            accepted=[candidate],
            can_download=can_download,
            outcome="supported_generic_high_confidence" if can_download else "unsupported_low_confidence",
            warnings=[],
        )

    @staticmethod
    def _control(href):
        return {
            "href": href,
            "rel_next": True,
            "labelled_next": True,
            "numbered": False,
            "visible": True,
            "disabled": False,
            "target": "",
            "download": False,
        }

    def test_only_same_path_numeric_next_page_is_a_safe_target(self):
        self.assertEqual(
            down._same_chapter_pagination_target(self.PAGE_ONE, self.PAGE_TWO),
            self.PAGE_TWO,
        )
        self.assertEqual(
            down._same_chapter_pagination_target(
                self.PAGE_ONE, "https://reader.example.test/chapter/two?page=2"),
            "",
        )
        self.assertEqual(
            down._same_chapter_pagination_target(
                self.PAGE_ONE, "https://other.example.test/chapter/one?page=2"),
            "",
        )
        self.assertEqual(
            down._same_chapter_pagination_target(
                self.PAGE_ONE, "https://reader.example.test/chapter/one?page=3"),
            "",
        )
        later_number = self._control("https://reader.example.test/chapter/one?page=3")
        later_number.update({"rel_next": False, "labelled_next": False, "numbered": True})
        status, target, _ = down._select_safe_pagination_target(
            self.PAGE_ONE, [self._control(self.PAGE_TWO), later_number])
        self.assertEqual((status, target), ("safe", self.PAGE_TWO))

    def test_safe_paginated_controls_use_get_not_click_and_aggregate_only_accepted_pages(self):
        class Driver:
            def __init__(self):
                self.current_url = PaginatedReaderSafetyTests.PAGE_ONE
                self.get_calls = []

            def get(self, url):
                self.get_calls.append(url)
                self.current_url = url

        driver = Driver()
        initial = self._analysis(1)
        second = self._analysis(2)
        aggregate = self._analysis(99)
        adapter = SimpleNamespace(
            is_specific=False,
            validate_navigation_url=mock.Mock(),
            validate_path=mock.Mock(),
            validate_redirect=mock.Mock(),
            analyze=mock.Mock(return_value=second),
        )

        def controls(_driver):
            return [self._control(self.PAGE_TWO)] if driver.current_url == self.PAGE_ONE else []

        with (
            mock.patch("down._read_pagination_controls", side_effect=controls),
            mock.patch("down._scroll_incrementally", return_value={
                "reached_document_end": True, "stabilized": True}),
            mock.patch("down._aggregate_paginated_analyses", return_value=aggregate) as aggregate_call,
            mock.patch("down._load_source_profile", return_value=None),
            mock.patch("down.time.sleep"),
        ):
            result, diagnostic = down._maybe_collect_paginated_reader(
                driver, adapter, initial, page_url=self.PAGE_ONE)

        self.assertIs(result, aggregate)
        self.assertEqual(driver.get_calls, [self.PAGE_TWO])
        self.assertEqual(diagnostic["status"], "complete")
        self.assertEqual(diagnostic["followed_pages"], 1)
        aggregate_call.assert_called_once()
        self.assertFalse(hasattr(driver, "click"))

    def test_next_chapter_control_is_never_followed(self):
        class Driver:
            current_url = PaginatedReaderSafetyTests.PAGE_ONE

            def get(self, _url):
                raise AssertionError("next chapter navigation must never be followed")

        initial = self._analysis(1)
        adapter = SimpleNamespace(is_specific=False)
        with mock.patch(
            "down._read_pagination_controls",
            return_value=[self._control("https://reader.example.test/chapter/two")],
        ):
            result, diagnostic = down._maybe_collect_paginated_reader(
                Driver(), adapter, initial, page_url=self.PAGE_ONE)

        self.assertIs(result, initial)
        self.assertEqual(diagnostic["status"], "blocked")
        self.assertEqual(diagnostic["reason"], "unverified_control")
        self.assertEqual(down._coverage_failure_reason(result), "pagination_incomplete")

    def test_visible_next_button_without_href_blocks_without_clicking(self):
        class Driver:
            current_url = PaginatedReaderSafetyTests.PAGE_ONE

            def get(self, _url):
                raise AssertionError("a page-controlled button must never be clicked")

        initial = self._analysis(1)
        adapter = SimpleNamespace(is_specific=False)
        button = self._control("")
        button.update({"interactive": True, "href": ""})
        with mock.patch("down._read_pagination_controls", return_value=[button]):
            result, diagnostic = down._maybe_collect_paginated_reader(
                Driver(), adapter, initial, page_url=self.PAGE_ONE)

        self.assertIs(result, initial)
        self.assertEqual(diagnostic["status"], "blocked")
        self.assertEqual(diagnostic["reason"], "unverified_control")
        self.assertEqual(down._coverage_failure_reason(result), "pagination_incomplete")

    def test_page_shaped_but_unprovable_control_fails_closed_without_navigation(self):
        class Driver:
            current_url = PaginatedReaderSafetyTests.PAGE_ONE

            def get(self, _url):
                raise AssertionError("unverified control must never be followed")

        initial = self._analysis(1)
        adapter = SimpleNamespace(is_specific=False)
        with mock.patch(
            "down._read_pagination_controls",
            return_value=[self._control("https://reader.example.test/chapter/two?page=2")],
        ):
            result, diagnostic = down._maybe_collect_paginated_reader(
                Driver(), adapter, initial, page_url=self.PAGE_ONE)

        self.assertIs(result, initial)
        self.assertEqual(diagnostic["status"], "blocked")
        self.assertEqual(diagnostic["reason"], "unverified_control")
        self.assertEqual(down._coverage_failure_reason(result), "pagination_incomplete")

    def test_stale_cycle_is_detected_before_a_second_navigation(self):
        class Driver:
            def __init__(self):
                self.current_url = PaginatedReaderSafetyTests.PAGE_ONE
                self.get_calls = []

            def get(self, url):
                self.get_calls.append(url)
                self.current_url = url

        driver = Driver()
        initial = self._analysis(1)
        second = self._analysis(2)
        adapter = SimpleNamespace(
            is_specific=False,
            validate_navigation_url=mock.Mock(),
            validate_path=mock.Mock(),
            validate_redirect=mock.Mock(),
            analyze=mock.Mock(return_value=second),
        )
        with (
            mock.patch("down._select_safe_pagination_target", side_effect=[
                ("safe", self.PAGE_TWO, self.PAGE_TWO),
                ("safe", self.PAGE_TWO, self.PAGE_TWO),
            ]),
            mock.patch("down._read_pagination_controls", return_value=[]),
            mock.patch("down._scroll_incrementally", return_value={
                "reached_document_end": True, "stabilized": True}),
            mock.patch("down._load_source_profile", return_value=None),
            mock.patch("down.time.sleep"),
        ):
            result, diagnostic = down._maybe_collect_paginated_reader(
                driver, adapter, initial, page_url=self.PAGE_ONE)

        self.assertIs(result, initial)
        self.assertEqual(driver.get_calls, [self.PAGE_TWO])
        self.assertEqual(diagnostic["reason"], "cycle")
        self.assertEqual(down._coverage_failure_reason(result), "pagination_incomplete")

    def test_page_follow_bound_blocks_before_an_unbounded_third_view(self):
        page_three = "https://reader.example.test/chapter/one?page=3"

        class Driver:
            def __init__(self):
                self.current_url = PaginatedReaderSafetyTests.PAGE_ONE
                self.get_calls = []

            def get(self, url):
                self.get_calls.append(url)
                self.current_url = url

        driver = Driver()
        initial = self._analysis(1)
        second = self._analysis(2)
        adapter = SimpleNamespace(
            is_specific=False,
            validate_navigation_url=mock.Mock(),
            validate_path=mock.Mock(),
            validate_redirect=mock.Mock(),
            analyze=mock.Mock(return_value=second),
        )

        def controls(_driver):
            if driver.current_url == self.PAGE_ONE:
                return [self._control(self.PAGE_TWO)]
            return [self._control(page_three)]

        with (
            mock.patch("down._read_pagination_controls", side_effect=controls),
            mock.patch("down._scroll_incrementally", return_value={
                "reached_document_end": True, "stabilized": True}),
            mock.patch("down._load_source_profile", return_value=None),
            mock.patch("down.time.sleep"),
            mock.patch.object(down, "MAX_PAGINATED_READER_FOLLOWS", 1),
        ):
            result, diagnostic = down._maybe_collect_paginated_reader(
                driver, adapter, initial, page_url=self.PAGE_ONE)

        self.assertIs(result, initial)
        self.assertEqual(driver.get_calls, [self.PAGE_TWO])
        self.assertEqual(diagnostic["reason"], "page_limit")
        self.assertEqual(down._coverage_failure_reason(result), "pagination_incomplete")

    def test_manual_candidate_submission_order_is_preserved(self):
        first = SimpleNamespace(id="first")
        second = SimpleNamespace(id="second")
        analysis = SimpleNamespace(
            outcome=REVIEW_REQUIRED_MEDIUM_CONFIDENCE,
            can_download=False,
            accepted=[first, second],
            warnings=[],
        )
        selected = down._selected_source_candidates(analysis, ["second", "first"])
        self.assertEqual(selected, [second, first])

    def test_manual_review_cannot_override_known_incomplete_pagination(self):
        analysis = SimpleNamespace(
            outcome=REVIEW_REQUIRED_MEDIUM_CONFIDENCE,
            can_download=False,
            accepted=[SimpleNamespace(id="only-page")],
            warnings=["pagination_incomplete"],
        )
        with self.assertRaises(SourceError) as ctx:
            down._selected_source_candidates(analysis, ["only-page"])
        self.assertEqual(ctx.exception.code, "incomplete_download")


if __name__ == "__main__":
    unittest.main()
