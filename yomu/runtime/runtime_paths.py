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


def resolve_runtime_root_for_job(db_path, output_dir: str = "", env=None) -> Path:
    """Runtime root that owns a job's trusted state: ``auth/``, ``jobs.sqlite3``,
    ``logs/`` and the secure auth context envelope.

    ``output_dir`` is an OUTPUT destination (it can point at any volume the caller
    chose) and must NEVER relocate that trusted state -- deriving the runtime root
    from it let a job whose output lived under the default ``TradutorIA\\output``
    root make the runner look for its auth envelope in the wrong ``auth/`` dir,
    surfacing as ``auth_context_missing_or_corrupt``.  Source of truth, in order:

      1. an explicit, existing ``TRADUTOR_RUNTIME_ROOT`` inherited by the process;
      2. the queue DB's parent directory (auth/, logs/ and the DB live together);
      3. legacy fallback: the ``<root>/output/...`` lineage of ``output_dir`` --
         used ONLY when neither of the above is usable.
    """
    environ = os.environ if env is None else env
    # 1. An explicit, existing TRADUTOR_RUNTIME_ROOT is authoritative.  output_dir is a
    #    mere destination and can NEVER relocate the trusted runtime state over it.
    explicit = str(environ.get("TRADUTOR_RUNTIME_ROOT") or "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_dir():
            return candidate
    db_parent = None
    if db_path:
        candidate = Path(db_path).resolve().parent
        if candidate.is_dir():
            db_parent = candidate
    # 2. Legacy: some jobs place the DB one level ABOVE the runtime root and carry a
    #    ``<runtime>/output/...`` output_dir.  Honor that ``<root>/output`` lineage ONLY
    #    when it stays inside the DB's own tree (a genuine same-install output subdir) --
    #    never when output_dir points at a foreign root, which is the exact mismatch that
    #    made the runner look for its auth envelope under the wrong root.
    output_path = Path(str(output_dir or "")).resolve()
    parts = [part.casefold() for part in output_path.parts]
    if "output" in parts:
        derived = Path(*output_path.parts[: parts.index("output")])
        if db_parent is None or derived == db_parent or db_parent in derived.parents:
            return derived
    # 3. The DB parent (auth/, logs/ and the DB live together) is the source of truth.
    if db_parent is not None:
        return db_parent
    return default_user_data_root() / "runtime"


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
