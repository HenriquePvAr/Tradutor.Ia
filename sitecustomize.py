"""Install the offline socket guard for explicit Python test entry points only.

Guarded test children inherit an environment marker and repository ``PYTHONPATH``, which lets
this hook run before they import a pipeline module. Detection deliberately examines only the
Python entry point, never normal command arguments such as a user URL or output slug.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path


def _is_test_invocation(
    argv: Sequence[object] | None = None,
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Return true only for a test launcher or inherited guarded child process."""

    values = sys.argv if argv is None else argv
    env = os.environ if environment is None else environment
    if env.get("TRADUTOR_IA_OFFLINE_TEST_GUARD") == "1":
        return True
    entry_path = Path(str(values[0] if values else ""))
    entrypoint = entry_path.name.casefold()
    path_parts = {part.casefold() for part in entry_path.parts}
    return (
        entrypoint in {"pytest", "pytest.exe", "py.test", "py.test.exe"}
        or "pytest" in path_parts
        or "unittest" in path_parts
        or entrypoint.startswith("test_")
        or entrypoint.endswith("_test.py")
    )


if _is_test_invocation():
    from offline_test_guard import install_offline_network_guard

    install_offline_network_guard()
