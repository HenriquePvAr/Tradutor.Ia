"""Runtime path resolution for writable Tradutor IA state.

Installed Beta builds may live under read-only locations such as ``Program Files``.
Mutable runtime data therefore defaults to a per-user application-data directory,
while explicit environment variables and hermetic test roots remain supported.
"""

from __future__ import annotations

import os
from pathlib import Path


APP_DIR_NAME = "TradutorIA"


def _env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def default_user_data_root() -> Path:
    """Return the writable per-user root for runtime state."""

    if os.getenv("TRADUTOR_IA_RUNTIME_ISOLATION_GUARD") == "1":
        isolated = _env_str("TRADUTOR_TEST_RUNTIME_ROOT")
        if not isolated:
            raise RuntimeError("hermetic_test_runtime_root_required")
        return Path(isolated).expanduser() / "user_data"
    configured = _env_str("TRADUTOR_USER_DATA_ROOT")
    if configured:
        return Path(configured).expanduser()
    local_app_data = _env_str("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APP_DIR_NAME
    xdg_state_home = _env_str("XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home) / APP_DIR_NAME
    return Path.home() / f".{APP_DIR_NAME.lower()}"


def runtime_root() -> Path:
    """Root for queue DB, logs, profiles and other mutable app state."""

    hermetic_root = _env_str("TRADUTOR_TEST_RUNTIME_ROOT")
    if os.getenv("TRADUTOR_IA_RUNTIME_ISOLATION_GUARD") == "1" and hermetic_root:
        return Path(hermetic_root).expanduser()
    configured = _env_str("TRADUTOR_RUNTIME_ROOT")
    return Path(configured).expanduser() if configured else default_user_data_root() / "runtime"


def cache_root() -> Path:
    configured = _env_str("CACHE_ROOT")
    return Path(configured).expanduser() if configured else default_user_data_root() / "cache"


def output_root() -> Path:
    configured = _env_str("TRADUTOR_OUTPUT_ROOT")
    return Path(configured).expanduser() if configured else default_user_data_root() / "output"


def temp_input_root() -> Path:
    configured = _env_str("TEMP_FOLDER")
    return Path(configured).expanduser() if configured else default_user_data_root() / "temp" / "input"


def temp_output_root() -> Path:
    configured = _env_str("TEMP_OUT")
    return Path(configured).expanduser() if configured else default_user_data_root() / "temp" / "output"
