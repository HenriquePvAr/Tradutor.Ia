"""Fail-closed network guard for the offline test suite."""

from __future__ import annotations

import socket
import ipaddress
import os
from pathlib import Path


_ORIGINAL_SOCKET_CONNECT = socket.socket.connect
_ORIGINAL_SOCKET_CONNECT_EX = socket.socket.connect_ex
_ORIGINAL_SOCKET_SENDTO = socket.socket.sendto
_ORIGINAL_CREATE_CONNECTION = socket.create_connection
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
OFFLINE_TEST_ENV = "TRADUTOR_IA_OFFLINE_TEST_GUARD"


class OfflineNetworkAttempt(AssertionError):
    """Raised before a standard test can open a real network connection."""


def _blocked_network(*_args, **_kwargs):
    raise OfflineNetworkAttempt(
        "A suíte padrão é offline: conexão de rede real bloqueada. "
        "Use um fake/mocking local; smokes de rede são manuais e exigem opt-in."
    )


def _is_loopback_address(address) -> bool:
    try:
        host = address[0]
        return ipaddress.ip_address(host).is_loopback
    except (TypeError, ValueError):
        return False


def _loopback_test_connect(sock, address):
    """Test-only seam for ASGI event-loop self-pipes; external sockets stay blocked."""
    if not _is_loopback_address(address):
        return _blocked_network(sock, address)
    return _ORIGINAL_SOCKET_CONNECT(sock, address)


def _loopback_test_connect_ex(sock, address):
    if not _is_loopback_address(address):
        return _blocked_network(sock, address)
    return _ORIGINAL_SOCKET_CONNECT_EX(sock, address)


def _loopback_test_sendto(sock, data, *args):
    """Block connectionless UDP sends to non-loopback destinations too."""

    address = args[-1] if args else None
    if not _is_loopback_address(address):
        return _blocked_network(sock, address)
    return _ORIGINAL_SOCKET_SENDTO(sock, data, *args)


def _loopback_test_create_connection(address, *args, **kwargs):
    if not _is_loopback_address(address):
        return _blocked_network(address, *args, **kwargs)
    return _ORIGINAL_CREATE_CONNECTION(address, *args, **kwargs)


def _offline_getaddrinfo(host, port, family=0, socktype=0, proto=0, flags=0):
    """Deterministic public DNS fixture for normal tests; never query a resolver.

    URL validation intentionally resolves hostnames as part of its SSRF policy.  Blocking
    only ``connect`` still lets that validation emit real DNS traffic, so the test guard
    supplies a synthetic globally-routable answer.  Security tests replace this function
    explicitly with private/unresolvable fixtures.
    """
    try:
        address = ipaddress.ip_address(str(host or ""))
        resolved = str(address)
        selected_family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    except ValueError:
        selected_family = socket.AF_INET6 if family == socket.AF_INET6 else socket.AF_INET
        resolved = "2606:2800:220:1:248:1893:25c8:1946" if selected_family == socket.AF_INET6 else "93.184.216.34"
    selected_type = socktype or socket.SOCK_STREAM
    selected_proto = proto or 6
    endpoint = (resolved, int(port or 0), 0, 0) if selected_family == socket.AF_INET6 else (resolved, int(port or 0))
    return [(selected_family, selected_type, selected_proto, "", endpoint)]


def install_offline_network_guard() -> None:
    """Block socket connection entry points for direct unittest and pytest runs."""

    if getattr(socket, "_tradutor_ia_offline_guard", False):
        os.environ[OFFLINE_TEST_ENV] = "1"
        return
    # asyncio's Windows proactor creates a loopback socketpair for its own event-loop wakeup.
    # That stays inside the test process; every non-loopback destination fails before a real
    # external request can be opened.
    socket.socket.connect = _loopback_test_connect
    socket.socket.connect_ex = _loopback_test_connect_ex
    socket.socket.sendto = _loopback_test_sendto
    socket.create_connection = _loopback_test_create_connection
    socket.getaddrinfo = _offline_getaddrinfo
    socket._tradutor_ia_offline_guard = True
    # Child Python processes launched by a hermetic test inherit this marker. The local
    # ``sitecustomize`` installs the same guard before they import a pipeline module.
    os.environ[OFFLINE_TEST_ENV] = "1"
    root = str(Path(__file__).resolve().parent)
    inherited_paths = [value for value in os.environ.get("PYTHONPATH", "").split(os.pathsep) if value]
    if root not in inherited_paths:
        # A child Python process imports ``sitecustomize`` during interpreter startup only
        # when this repository is already on PYTHONPATH. Keep the marker scoped to test
        # descendants so worker/fake-pipeline subprocesses inherit the fail-closed boundary.
        os.environ["PYTHONPATH"] = os.pathsep.join([root, *inherited_paths])
