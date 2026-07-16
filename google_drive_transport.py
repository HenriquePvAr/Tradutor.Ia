"""Concrete HTTP transport for the Google Drive provider, backed by requests.

Implements the injected ``HttpTransport`` contract with a reused ``requests.Session``,
explicit connect/read timeouts, streaming responses (so a large download is never loaded
whole into memory), and TLS verification on. It applies no retry policy of its own - the
provider owns retry/backoff/refresh - and converts transport-level failures into the
typed StorageError the provider expects. It never logs the Authorization header or any
credential.
"""

from __future__ import annotations

from typing import Iterator

import requests

from community_storage import StorageError
from google_drive_storage import HttpResponse


class RequestsHttpTransport:
    def __init__(self, *, connect_timeout: float = 10.0, read_timeout: float = 60.0,
                 session: requests.Session | None = None):
        self._timeout = (connect_timeout, read_timeout)
        self._session = session or requests.Session()

    def request(self, method: str, url: str, *, headers: dict | None = None,
                data: bytes | None = None, stream: bool = False) -> HttpResponse:
        try:
            resp = self._session.request(
                method, url, headers=headers, data=data, stream=stream,
                timeout=self._timeout, allow_redirects=False)
        except requests.exceptions.SSLError as exc:
            # A TLS failure is not something to retry blindly; surface it as fatal.
            raise StorageError("tls_verification_failed") from exc
        except requests.exceptions.RequestException as exc:
            # Connection resets/timeouts are transient; the provider decides on retry.
            raise StorageError(f"http_transport_error: {type(exc).__name__}", transient=True) from exc

        resp_headers = {k: v for k, v in resp.headers.items()}
        if stream:
            def _iter(chunk=65536) -> Iterator[bytes]:
                try:
                    for block in resp.iter_content(chunk_size=chunk):
                        if block:
                            yield block
                finally:
                    resp.close()
            return HttpResponse(status=resp.status_code, headers=resp_headers, _iter=_iter())
        content = resp.content
        resp.close()
        return HttpResponse(status=resp.status_code, headers=resp_headers, content=content)

    def close(self) -> None:
        self._session.close()
