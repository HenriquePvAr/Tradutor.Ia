"""Beta.10 updater observability and bounded-pretoken UI contracts."""

from __future__ import annotations

import _test_bootstrap  # noqa: F401

import unittest
from pathlib import Path
from starlette.requests import Request

import app_ui


ROOT = Path(__file__).resolve().parent


def _request(headers: list[tuple[bytes, bytes]]) -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/api/update/download",
        "headers": headers,
        "query_string": b"",
        "server": ("127.0.0.1", 8080),
        "client": ("127.0.0.1", 12345),
        "scheme": "http",
    })


class CorrelationIdTests(unittest.TestCase):
    def test_valid_header_is_reused(self):
        request = _request([(b"x-yomu-correlation-id", b"abc-123._x")])
        self.assertEqual(app_ui._update_correlation_id(request), "abc-123._x")

    def test_invalid_header_is_replaced(self):
        request = _request([(b"x-yomu-correlation-id", b"token=secret&password=x")])
        value = app_ui._update_correlation_id(request)
        self.assertRegex(value, r"^[0-9a-f]{32}$")


class ExceptionSanitizationTests(unittest.TestCase):
    def test_message_and_cause_are_bounded_and_redacted(self):
        try:
            try:
                raise ValueError("password=secret Authorization=Bearer abc")
            except ValueError as inner:
                raise RuntimeError("https://example.invalid/x?token=secret") from inner
        except RuntimeError as exc:
            details = app_ui._safe_update_exception(exc)
        self.assertEqual(details["exception_type"], "RuntimeError")
        self.assertNotIn("secret", details["message"])
        self.assertNotIn("https://", details["message"])
        self.assertEqual(details["cause_type"], "ValueError")
        self.assertNotIn("secret", details["cause_message"])


class FrontendContractTests(unittest.TestCase):
    def test_frontend_has_bounded_initial_download_request_and_recovery(self):
        source = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
        self.assertIn("UPDATE_DOWNLOAD_REQUEST_TIMEOUT_MS", source)
        self.assertIn("new AbortController()", source)
        self.assertIn("X-Yomu-Correlation-Id", source)
        self.assertIn("Download demorou além do esperado", source)
        self.assertIn("download.textContent = 'Baixar e instalar'", source)


class RouteLoggingContractTests(unittest.TestCase):
    def test_route_phase_events_are_present(self):
        source = (ROOT / "app_ui.py").read_text(encoding="utf-8")
        for event in (
            "UPDATE_DOWNLOAD_REQUEST_RECEIVED",
            "UPDATE_DOWNLOAD_MANIFEST_FETCH_START",
            "UPDATE_DOWNLOAD_MANIFEST_FETCH_OK",
            "UPDATE_DOWNLOAD_MANIFEST_VERIFY_OK",
            "UPDATE_DOWNLOAD_TOKEN_CREATED",
            "UPDATE_DOWNLOAD_STATE_REGISTERED",
            "UPDATE_DOWNLOAD_THREAD_STARTED",
            "UPDATE_DOWNLOAD_RESPONSE_RETURNED",
            "UPDATE_DOWNLOAD_WORKER_ERROR",
        ):
            self.assertIn(event, source)


if __name__ == "__main__":
    unittest.main()
