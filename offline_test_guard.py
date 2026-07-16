"""Fail-closed network guard for the offline test suite."""

from __future__ import annotations

import socket
import ipaddress


_ORIGINAL_SOCKET_CONNECT = socket.socket.connect
_ORIGINAL_SOCKET_CONNECT_EX = socket.socket.connect_ex
_ORIGINAL_CREATE_CONNECTION = socket.create_connection


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


def _loopback_test_create_connection(address, *args, **kwargs):
    if not _is_loopback_address(address):
        return _blocked_network(address, *args, **kwargs)
    return _ORIGINAL_CREATE_CONNECTION(address, *args, **kwargs)


def install_offline_network_guard() -> None:
    """Block socket connection entry points for direct unittest and pytest runs."""

    if getattr(socket, "_tradutor_ia_offline_guard", False):
        return
    socket.socket.connect = _blocked_network
    socket.socket.connect_ex = _blocked_network
    socket.create_connection = _blocked_network
    socket._tradutor_ia_offline_guard = True
