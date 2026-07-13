"""Fail-closed network guard for the offline test suite."""

from __future__ import annotations

import socket


class OfflineNetworkAttempt(AssertionError):
    """Raised before a standard test can open a real network connection."""


def _blocked_network(*_args, **_kwargs):
    raise OfflineNetworkAttempt(
        "A suíte padrão é offline: conexão de rede real bloqueada. "
        "Use um fake/mocking local; smokes de rede são manuais e exigem opt-in."
    )


def install_offline_network_guard() -> None:
    """Block socket connection entry points for direct unittest and pytest runs."""

    if getattr(socket, "_tradutor_ia_offline_guard", False):
        return
    socket.socket.connect = _blocked_network
    socket.socket.connect_ex = _blocked_network
    socket.create_connection = _blocked_network
    socket._tradutor_ia_offline_guard = True
