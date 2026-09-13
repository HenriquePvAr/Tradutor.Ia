"""Read-only JWKS transport diagnostics for the frozen runtime.

This deliberately bypasses auth/session state and never logs the JWKS body.
"""
from __future__ import annotations

import json
import platform
import socket
import ssl
import sys
import time
from pathlib import Path

URL = "https://mimrsxnhqbqkffsekxuw.supabase.co/auth/v1/.well-known/jwks.json"
HOST = "mimrsxnhqbqkffsekxuw.supabase.co"
PATH = "/auth/v1/.well-known/jwks.json"
IPS = ("172.64.149.246", "104.18.38.10")


def _timed(fn):
    started = time.perf_counter()
    try:
        value = fn()
        return value, round((time.perf_counter() - started) * 1000, 3), None
    except Exception as exc:  # diagnostics must continue through individual failures
        return None, round((time.perf_counter() - started) * 1000, 3), type(exc).__name__


def _raw_ip_probe(ip: str) -> dict:
    context = ssl.create_default_context()
    result = {"ip": ip}
    sock, tcp_ms, tcp_error = _timed(lambda: socket.create_connection((ip, 443), timeout=2))
    result.update(tcp_ms=tcp_ms, tcp_status="ok" if sock else "error", tcp_exception=tcp_error)
    if sock is None:
        return result
    tls_sock, tls_ms, tls_error = _timed(lambda: context.wrap_socket(sock, server_hostname=HOST))
    result.update(tls_ms=tls_ms, tls_status="ok" if tls_sock else "error", tls_exception=tls_error)
    if tls_sock is None:
        try: sock.close()
        except OSError: pass
        return result
    try:
        write_started = time.perf_counter()
        tls_sock.sendall((f"GET {PATH} HTTP/1.1\r\nHost: {HOST}\r\nConnection: close\r\n\r\n").encode())
        write_ms = round((time.perf_counter() - write_started) * 1000, 3)
        first_started = time.perf_counter()
        first = tls_sock.recv(65536)
        first_ms = round((time.perf_counter() - first_started) * 1000, 3)
        body_started = time.perf_counter(); chunks = [first]
        while first:
            first = tls_sock.recv(65536)
            if first: chunks.append(first)
        body_ms = round((time.perf_counter() - body_started) * 1000, 3)
        status = first_line = chunks[0].split(b"\r\n", 1)[0].decode("ascii", "replace") if chunks else ""
        result.update(http_status=status, write_ms=write_ms, first_byte_ms=first_ms,
                      body_read_ms=body_ms, bytes=len(b"".join(chunks)))
    except Exception as exc:
        result["http_exception"] = type(exc).__name__
    finally:
        try: tls_sock.close()
        except OSError: pass
    return result


def run() -> int:
    info = {"python": sys.version, "platform": platform.platform(), "openssl": ssl.OPENSSL_VERSION}
    try:
        import requests, urllib3, certifi
        info.update(requests=requests.__version__, urllib3=urllib3.__version__, certifi=certifi.__version__,
                    ca_paths={"requests": requests.certs.where(), "certifi": certifi.where()})
        session = requests.Session(); session.trust_env = False
        session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        _, direct_ms, direct_error = _timed(lambda: session.get(URL, timeout=(2, 4)))
        info.update(requests_direct_ms=direct_ms, requests_direct_error=direct_error)
        from google_drive_transport import RequestsHttpTransport
        transport = RequestsHttpTransport(connect_timeout=2, read_timeout=4, session=session)
        _, prod_ms, prod_error = _timed(lambda: transport.request("GET", URL, headers={"Accept": "application/json"}))
        info.update(production_transport_ms=prod_ms, production_transport_error=prod_error)
    except Exception as exc:
        info.update(requests_import_error=type(exc).__name__)
    dns, dns_ms, dns_error = _timed(lambda: socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM))
    info.update(dns_ms=dns_ms, dns_error=dns_error,
                dns_addresses=sorted({item[4][0] for item in (dns or [])}))
    info["raw_ip_probes"] = [_raw_ip_probe(ip) for ip in IPS]
    print(json.dumps(info, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
