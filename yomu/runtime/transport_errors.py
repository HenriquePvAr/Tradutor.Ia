"""Sanitized transport error classification shared by local loopback clients."""
from __future__ import annotations

import errno
import re

import requests


def classify_transport_error(exc: BaseException) -> str:
    """Classify without returning exception text, URLs, headers, or credentials."""
    if isinstance(exc, requests.exceptions.SSLError):
        return "TLS_ERROR"
    if isinstance(exc, (requests.exceptions.Timeout, TimeoutError)):
        return "HTTP_TIMEOUT"
    if isinstance(exc, requests.exceptions.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return f"HTTP_ERROR_{status}" if isinstance(status, int) else "HTTP_ERROR"
    if isinstance(exc, requests.exceptions.ConnectionError):
        pending = [exc]
        seen = set()
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            code = getattr(current, "errno", None)
            if code is None:
                match = re.search(r"\b(?:WinError|Errno)\s+(10061|10054|10060|111|104|110)\b",
                                  str(current), re.IGNORECASE)
                code = int(match.group(1)) if match else None
            if code in {errno.ECONNREFUSED, 10061, 111}:
                return "CONNECTION_REFUSED"
            if code in {errno.ECONNRESET, 10054, 104}:
                return "CONNECTION_RESET"
            if code in {errno.ETIMEDOUT, 10060, 110}:
                return "HTTP_TIMEOUT"
            pending.extend(item for item in (current.__cause__, current.__context__)
                           if isinstance(item, BaseException))
            pending.extend(item for item in current.args if isinstance(item, BaseException))
        return "CONNECTION_ERROR"
    if isinstance(exc, (requests.exceptions.InvalidURL, requests.exceptions.MissingSchema)):
        return "REMOTE_CONFIG_ERROR"
    return "REMOTE_TRANSPORT_ERROR"
