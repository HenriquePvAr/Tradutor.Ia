from __future__ import annotations

import _test_bootstrap  # noqa: F401

import json
import tempfile
import unittest
from pathlib import Path

from request_observability import (
    StructuredRequestAuditMiddleware,
    configured_request_audit_destination,
)


class StructuredRequestAuditMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def _request(self, middleware, path="/api/ui/bootstrap", query=b"token=secret"):
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await middleware(
            {
                "type": "http",
                "method": "GET",
                "path": path,
                "query_string": query,
                "headers": [(b"authorization", b"Bearer secret")],
            },
            receive,
            send,
        )
        return messages

    async def test_records_only_sanitized_request_metadata(self):
        async def app(_scope, _receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "requests.jsonl"
            middleware = StructuredRequestAuditMiddleware(app, destination=destination)
            messages = await self._request(middleware)
            record = json.loads(destination.read_text(encoding="utf-8"))

        self.assertEqual(messages[0]["status"], 200)
        self.assertEqual(record["method"], "GET")
        self.assertEqual(record["path"], "/api/ui/bootstrap")
        self.assertEqual(record["status"], 200)
        self.assertGreaterEqual(record["duration_ms"], 0)
        serialized = json.dumps(record)
        self.assertNotIn("token", serialized)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("authorization", serialized.lower())

    async def test_disabled_destination_is_inert(self):
        called = []

        async def app(_scope, _receive, send):
            called.append(True)
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        middleware = StructuredRequestAuditMiddleware(app, destination=None)
        await self._request(middleware)
        self.assertEqual(called, [True])

    async def test_non_http_scope_is_not_logged(self):
        called = []

        async def app(_scope, _receive, _send):
            called.append(True)

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "requests.jsonl"
            middleware = StructuredRequestAuditMiddleware(app, destination=destination)
            await middleware({"type": "websocket"}, None, None)
            self.assertFalse(destination.exists())
        self.assertEqual(called, [True])

    def test_configured_destination_must_remain_under_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allowed = root / ".runtime" / "audit" / "requests.jsonl"
            self.assertEqual(
                configured_request_audit_destination(root, {
                    "TRADUTOR_REQUEST_OBSERVABILITY": "1",
                    "TRADUTOR_REQUEST_AUDIT_PATH": str(allowed),
                }),
                allowed.resolve(),
            )
            with self.assertRaisesRegex(RuntimeError, "request_observability_path_not_local"):
                configured_request_audit_destination(root, {
                    "TRADUTOR_REQUEST_OBSERVABILITY": "1",
                    "TRADUTOR_REQUEST_AUDIT_PATH": str(root / "outside.jsonl"),
                })


if __name__ == "__main__":
    unittest.main()
