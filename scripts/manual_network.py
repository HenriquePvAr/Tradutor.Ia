"""Fail-closed authorization shared by manual network smoke scripts."""

from __future__ import annotations

import os


NETWORK_OPT_IN = "ALLOW_NETWORK_TESTS"


class NetworkSmokeNotAuthorized(RuntimeError):
    """Raised before a manual smoke script imports external clients."""


def require_network_smoke(specific_opt_in: str) -> None:
    """Require the generic and operation-specific opt-ins to be exactly ``1``."""

    missing = [
        name
        for name in (NETWORK_OPT_IN, specific_opt_in)
        if os.environ.get(name) != "1"
    ]
    if missing:
        names = " e ".join(missing)
        raise NetworkSmokeNotAuthorized(
            "Smoke de rede bloqueado antes de inicializar clientes externos. "
            f"Defina {names}=1 explicitamente para autorizar esta operacao manual."
        )
