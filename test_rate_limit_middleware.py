import unittest
from unittest.mock import patch

from app_ui import MutationRateLimitMiddleware


class MutationRateLimitMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, middleware, path, client=("127.0.0.1", 8080)):
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": path,
                "client": client,
            },
            receive,
            send,
        )
        status = next(
            (message["status"] for message in messages if message["type"] == "http.response.start"),
            200,
        )
        return status, messages

    async def test_telemetry_saturation_does_not_consume_run_bucket(self):
        downstream_calls = []

        async def downstream(scope, receive, send):
            downstream_calls.append(scope["path"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        middleware = MutationRateLimitMiddleware(downstream)
        with patch("app_ui._append_diagnostic_log"):
            for _ in range(middleware._LIMITS["/api/ui/"][0]):
                status, _ = await self._call(middleware, "/api/ui/source-trace")
                self.assertEqual(status, 200)
            status, _ = await self._call(middleware, "/api/ui/source-trace")
            self.assertEqual(status, 429)
            status, _ = await self._call(middleware, "/api/ui/run")
            self.assertEqual(status, 200)

        self.assertEqual(downstream_calls[-1], "/api/ui/run")

    async def test_user_mutation_saturation_still_rejects_run(self):
        async def downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        middleware = MutationRateLimitMiddleware(downstream)
        with patch("app_ui._append_diagnostic_log"):
            for _ in range(middleware._LIMITS["/api/ui/"][0]):
                status, _ = await self._call(middleware, "/api/ui/open")
                self.assertEqual(status, 200)
            status, messages = await self._call(middleware, "/api/ui/run")

        self.assertEqual(status, 429)
        response_start = next(message for message in messages if message["type"] == "http.response.start")
        headers = dict(response_start["headers"])
        self.assertEqual(headers[b"retry-after"], b"60")

    def test_route_classification_uses_real_artifact_semantics(self):
        self.assertEqual(
            MutationRateLimitMiddleware._bucket_group("/api/ui/run"),
            "user_mutation",
        )
        self.assertEqual(
            MutationRateLimitMiddleware._bucket_group("/api/ui/open"),
            "user_mutation",
        )
        self.assertEqual(
            MutationRateLimitMiddleware._bucket_group("/api/ui/source-trace"),
            "telemetry",
        )
        self.assertEqual(
            MutationRateLimitMiddleware._bucket_group("/api/ui/control-plane-trace"),
            "telemetry",
        )


if __name__ == "__main__":
    unittest.main()
