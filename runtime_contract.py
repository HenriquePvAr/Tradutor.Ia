"""Identity of the immutable runtime that owns a queued job or worker lease."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import app_version


CONTRACT_VERSION = "worker-contract-v2"
_PYTHON_LAUNCHERS = frozenset({"python", "pythonw"})


def _build_version() -> str:
    return str(getattr(app_version, "BUILD_VERSION", "dev") or "dev")


def canonical_runtime_identity(
    executable: str | Path | None = None,
    *,
    frozen: bool | None = None,
    runtime_prefix: str | Path | None = None,
) -> str:
    """Return the launcher-independent identity of one Yomu runtime.

    Source/dev processes are one runtime when they share the same interpreter
    prefix, regardless of whether Python was launched through ``python.exe`` or
    ``pythonw.exe``.  Frozen processes are identified by the product executable
    payload, so different packaged candidates remain incompatible.
    """
    path = Path(executable or sys.executable)
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
    if not is_frozen and path.stem.casefold() in _PYTHON_LAUNCHERS:
        prefix = Path(runtime_prefix or (path.parent.parent if path.parent.name.casefold() == "scripts" else sys.prefix))
        try:
            return f"source:{prefix.resolve()}".casefold()
        except OSError:
            return f"source:{prefix}".casefold()

    try:
        payload = path.read_bytes()
        fingerprint = hashlib.sha256(payload).hexdigest()[:24]
    except (OSError, ValueError):
        fingerprint = hashlib.sha256(str(path.resolve()).casefold().encode("utf-8")).hexdigest()[:24]
    return f"frozen:{fingerprint}"


def current_runtime_contract(
    executable: str | Path | None = None,
    *,
    frozen: bool | None = None,
    runtime_prefix: str | Path | None = None,
) -> str:
    """Return a stable, launcher-independent contract for one Yomu runtime."""
    identity = canonical_runtime_identity(
        executable, frozen=frozen, runtime_prefix=runtime_prefix)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"{CONTRACT_VERSION}:{_build_version()}:{digest}"
