"""Categorization of Drive admin OAuth refresh failures — hermetic, no network.

Covers invalid_grant/invalid_client/invalid_request, HTTP 429/5xx, transport-level
timeouts, malformed/non-JSON bodies, and unrecognized OAuth error codes. Also proves
no secret value (access token, refresh token, client secret, or the raw response body)
ever appears in the exception's str()/repr() or in captured stdout/stderr.
"""

import _test_bootstrap  # noqa: F401

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import drive_auth
from community_storage import StorageError
from google_drive_storage import HttpResponse

TEST_ACCESS_TOKEN = "TEST_ACCESS_TOKEN_DO_NOT_LOG"
TEST_REFRESH_TOKEN = "TEST_REFRESH_TOKEN_DO_NOT_LOG"
TEST_CLIENT_SECRET = "TEST_CLIENT_SECRET_DO_NOT_LOG"
TEST_ERROR_DESCRIPTION = "TEST_ERROR_DESCRIPTION_DO_NOT_LOG contains user@example.com and a code"


class FakeTransport:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.calls = []

    def request(self, method, url, *, headers=None, data=None, stream=False):
        self.calls.append({"method": method, "url": url, "data": data})
        if self._exc is not None:
            raise self._exc
        return self._response


def _oauth_error_body(error: str, description: str = TEST_ERROR_DESCRIPTION) -> bytes:
    return json.dumps({"error": error, "error_description": description}).encode("utf-8")


class DriveAuthRefreshCategorizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "token.json"
        drive_auth.save_tokens(self.path, drive_auth.OAuthTokens(refresh_token=TEST_REFRESH_TOKEN))

    def _source(self, transport):
        return drive_auth.FileTokenSource(
            token_path=self.path, client_id="cid", client_secret=TEST_CLIENT_SECRET,
            transport=transport)

    def _refresh_error(self, transport):
        src = self._source(transport)
        with self.assertRaises(drive_auth.DriveAuthRefreshError) as ctx:
            src.refresh()
        return ctx.exception

    # -- category mapping ---------------------------------------------------

    def test_invalid_grant_requires_reconnect_no_auto_retry_signal(self):
        resp = HttpResponse(400, {}, _oauth_error_body("invalid_grant"))
        exc = self._refresh_error(FakeTransport(response=resp))
        self.assertEqual(exc.category, drive_auth.CATEGORY_RECONNECT_REQUIRED)
        self.assertFalse(exc.transient)
        self.assertEqual(exc.oauth_error, "invalid_grant")

    def test_invalid_client_is_configuration_error(self):
        resp = HttpResponse(401, {}, _oauth_error_body("invalid_client"))
        exc = self._refresh_error(FakeTransport(response=resp))
        self.assertEqual(exc.category, drive_auth.CATEGORY_CONFIGURATION_ERROR)
        self.assertFalse(exc.transient)

    def test_invalid_request_is_not_promoted_to_reconnect(self):
        resp = HttpResponse(400, {}, _oauth_error_body("invalid_request"))
        exc = self._refresh_error(FakeTransport(response=resp))
        self.assertNotEqual(exc.category, drive_auth.CATEGORY_RECONNECT_REQUIRED)

    def test_http_429_is_transient(self):
        resp = HttpResponse(429, {}, _oauth_error_body("invalid_grant"))
        exc = self._refresh_error(FakeTransport(response=resp))
        self.assertEqual(exc.category, drive_auth.CATEGORY_TRANSIENT_ERROR)
        self.assertTrue(exc.transient)

    def test_http_500_is_transient(self):
        resp = HttpResponse(500, {}, b"")
        exc = self._refresh_error(FakeTransport(response=resp))
        self.assertEqual(exc.category, drive_auth.CATEGORY_TRANSIENT_ERROR)

    def test_http_503_is_transient(self):
        resp = HttpResponse(503, {}, b"")
        exc = self._refresh_error(FakeTransport(response=resp))
        self.assertEqual(exc.category, drive_auth.CATEGORY_TRANSIENT_ERROR)

    def test_transport_timeout_is_transient(self):
        transport_exc = StorageError("http_transport_error: ConnectTimeout", transient=True)
        exc = self._refresh_error(FakeTransport(exc=transport_exc))
        self.assertEqual(exc.category, drive_auth.CATEGORY_TRANSIENT_ERROR)
        self.assertTrue(exc.transient)

    def test_non_json_body_is_protocol_error_not_invalid_grant(self):
        resp = HttpResponse(400, {}, b"<html>not json</html>")
        exc = self._refresh_error(FakeTransport(response=resp))
        self.assertEqual(exc.category, drive_auth.CATEGORY_PROTOCOL_ERROR)
        self.assertNotEqual(exc.category, drive_auth.CATEGORY_RECONNECT_REQUIRED)

    def test_json_without_error_field_is_protocol_error(self):
        resp = HttpResponse(400, {}, json.dumps({"foo": "bar"}).encode())
        exc = self._refresh_error(FakeTransport(response=resp))
        self.assertEqual(exc.category, drive_auth.CATEGORY_PROTOCOL_ERROR)

    def test_unrecognized_oauth_error_is_unknown_not_reconnect(self):
        resp = HttpResponse(400, {}, _oauth_error_body("some_future_google_error"))
        exc = self._refresh_error(FakeTransport(response=resp))
        self.assertEqual(exc.category, drive_auth.CATEGORY_UNKNOWN_ERROR)
        self.assertNotEqual(exc.category, drive_auth.CATEGORY_RECONNECT_REQUIRED)
        self.assertEqual(exc.oauth_error, "some_future_google_error")

    # -- secret safety --------------------------------------------------------

    def test_no_secret_or_raw_body_leaks_in_exception_or_output(self):
        resp = HttpResponse(400, {}, _oauth_error_body("invalid_grant"))
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            try:
                self._source(FakeTransport(response=resp)).refresh()
            except drive_auth.DriveAuthRefreshError as exc:
                print(str(exc))
                print(repr(exc))
        haystack = buf_out.getvalue() + buf_err.getvalue()
        for secret in (TEST_ACCESS_TOKEN, TEST_REFRESH_TOKEN, TEST_CLIENT_SECRET, TEST_ERROR_DESCRIPTION):
            self.assertNotIn(secret, haystack)
        self.assertNotIn("error_description", haystack)

    def test_exception_str_and_repr_never_contain_secrets(self):
        resp = HttpResponse(400, {}, _oauth_error_body("invalid_client"))
        exc = self._refresh_error(FakeTransport(response=resp))
        for secret in (TEST_ACCESS_TOKEN, TEST_REFRESH_TOKEN, TEST_CLIENT_SECRET, TEST_ERROR_DESCRIPTION):
            self.assertNotIn(secret, str(exc))
            self.assertNotIn(secret, repr(exc))


if __name__ == "__main__":
    unittest.main()
