"""Regression: the Comix DOWNLOAD path must not treat a reader-API 401/403 as terminal.

Physically observed: the reader JSON API (/api/v1/chapters/{id}) returned 403 while a normal
controlled browser still opened the reader (105 progress segments).  The download used to try
the dynamic browser resolver, silently swallow its (transient) failure, fall to the Selenium
legacy path, hit the reader-API 403 and die with source_access_denied — even though the
supported browser resolver materialises the chapter without that API.

These tests pin the new contract in down.download_images:
  * dynamic-first fails -> Selenium legacy raises source_access_denied (reader_api_status_403)
    -> ONE bounded dynamic-resolver retry -> success (no terminal denial);
  * if the retry also fails -> terminal source_access_denied (real inaccessibility, never masked).
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chapter_source import SourceError
from down import download_images
from scrapling_reader_resolver import DynamicReaderError
from test_downloader_regressions import _accepted_source_analysis


class _QuitDriver:
    current_url = "https://comix.to/title/x/1-chapter-1"

    def get(self, _url):
        return None

    def quit(self):
        return None


def _comix_adapter(analyze):
    return SimpleNamespace(
        name="comix", adapter_version="1", is_specific=True,
        validate_url=lambda _u: None, validate_path=lambda _u: None,
        normalize_url=lambda v: v, validate_redirect=lambda _u: None,
        analyze=analyze,
    )


def _selenium_mocks():
    return (
        mock.patch("http_source_discovery.discover_via_http", return_value=None),
        mock.patch("down.preflight_browser_navigation", side_effect=lambda _a, v: v),
        mock.patch("down._create_driver", return_value=_QuitDriver()),
        mock.patch("down._capture_driver_ownership", return_value={}),
        mock.patch("down._refresh_driver_ownership"),
        mock.patch("down._bounded_driver_teardown",
                   return_value={"status": "success", "timeout_occurred": False}),
        mock.patch("down._viewer_image_snapshot",
                   return_value={"image_count": 1, "urls": ["one"], "complete_manifest": True}),
        mock.patch("down._scroll_incrementally",
                   return_value={"reached_document_end": True, "stabilized": True}),
        mock.patch("down.time.sleep"),
    )


class ComixDownload403FallbackTest(unittest.TestCase):
    def _run(self, *, retry_side_effect):
        # analyze raises the reader-API 403; resolve() fails first (dynamic-first at line 188),
        # then does whatever retry_side_effect dictates for the fallback attempt.
        analyze = mock.Mock(side_effect=SourceError("source_access_denied", "reader_api_status_403"))
        adapter = _comix_adapter(analyze)
        resolve = mock.Mock(side_effect=[DynamicReaderError("no_reader_images")] + list(retry_side_effect))
        captured = {}

        class _T:
            name = "requests"

            @staticmethod
            def close():
                return None

        def fake_download(*args, **_k):
            report = args[6]
            captured["report"] = report
            captured["candidate_count"] = len(args[1])
            report["download_gate"] = {"passed": True}
            report["download_valid"] = True
            return [f"p{i}.png" for i in range(len(args[1]))]

        patches = _selenium_mocks() + (
            mock.patch("chapter_source.select_adapter", return_value=adapter),
            mock.patch("scrapling_reader_resolver.resolve", resolve),
            mock.patch("download_transport.build_transports", return_value=[_T()]),
            mock.patch("down._download_candidates", side_effect=fake_download),
            mock.patch("down._persist_download_metadata"),
            mock.patch("down._write_download_report"),
        )
        with tempfile.TemporaryDirectory() as folder:
            ctx = []
            for p in patches:
                ctx.append(p.__enter__())
            try:
                return download_images(
                    "https://comix.to/title/x/1-chapter-1",
                    target_folder=str(Path(folder) / "input"),
                    force=True,
                ), captured, resolve
            finally:
                for p in reversed(patches):
                    p.__exit__(None, None, None)

    def test_reader_api_403_triggers_dynamic_fallback_success(self):
        analysis = _accepted_source_analysis(count=105, outcome="supported_specific_adapter")
        result, captured, resolve = self._run(retry_side_effect=[analysis])
        # No terminal denial: the download materialised via the dynamic resolver retry.
        self.assertEqual(len(result), 105)
        # The reader-API 403 re-triggered the supported browser resolver instead of failing.
        self.assertTrue(captured["report"].get("dynamic_resolver_403_fallback_attempted"))
        self.assertTrue(captured["report"].get("dynamic_resolver_403_fallback"))
        self.assertEqual(resolve.call_count, 2)  # dynamic-first + one bounded retry (no infinite loop)

    def test_reader_api_403_then_dynamic_also_fails_is_terminal(self):
        with self.assertRaises(SourceError) as ctx:
            self._run(retry_side_effect=[DynamicReaderError("no_reader_images")])
        # Real inaccessibility is never masked as success.
        self.assertEqual(ctx.exception.code, "source_access_denied")


if __name__ == "__main__":
    unittest.main()
