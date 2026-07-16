"""Deterministic, safe loading of the project's local environment file."""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv
from dotenv.parser import parse_stream


PROJECT_ROOT = Path(__file__).resolve().parent
LOCAL_ENV_PATH = PROJECT_ROOT / ".env"


class LocalEnvironmentError(RuntimeError):
    """A local environment file exists but cannot be safely loaded."""


def load_local_environment(env_path: str | Path | None = None) -> bool:
    """Load only the project's explicit ``.env`` without replacing process values.

    ``env_path`` is an injection seam for isolated tests. Production callers omit it and
    always use the path beside this module, independently of the current working
    directory. A missing file is allowed; malformed or unreadable files fail closed with
    an error that never includes their contents or absolute path.
    """
    path = LOCAL_ENV_PATH if env_path is None else Path(env_path)
    if not path.exists():
        return False
    if not path.is_file():
        raise LocalEnvironmentError("local .env is not a regular file")

    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            for binding in parse_stream(stream):
                if binding.error:
                    raise LocalEnvironmentError(
                        f"invalid local .env syntax on line {binding.original.line}"
                    )
        return load_dotenv(
            dotenv_path=path,
            override=False,
            interpolate=True,
            encoding="utf-8-sig",
        )
    except LocalEnvironmentError:
        raise
    except (OSError, UnicodeError) as exc:
        raise LocalEnvironmentError("local .env could not be read") from exc


def load_local_environment_for_entrypoint() -> bool:
    """Load local configuration and emit a value-free CLI error on failure."""
    try:
        load_local_environment()
    except LocalEnvironmentError as exc:
        print(f"configuration_error: {exc}", file=sys.stderr)
        return False
    return True
