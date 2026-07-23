"""Deterministic, safe loading of the project's local environment file."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from dotenv.parser import parse_stream


PROJECT_ROOT = Path(__file__).resolve().parent
LOCAL_ENV_PATH = PROJECT_ROOT / ".env"
LOCAL_ENV_OVERRIDE_PATH = PROJECT_ROOT / ".env.local"


class LocalEnvironmentError(RuntimeError):
    """A local environment file exists but cannot be safely loaded."""


def _validate_env_file(path: Path) -> None:
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            for binding in parse_stream(stream):
                if binding.error:
                    raise LocalEnvironmentError(
                        f"invalid local .env syntax on line {binding.original.line}"
                    )
    except LocalEnvironmentError:
        raise
    except (OSError, UnicodeError) as exc:
        raise LocalEnvironmentError("local .env could not be read") from exc


def load_local_environment(env_path: str | Path | None = None) -> bool:
    """Load only the project's explicit ``.env`` without replacing process values.

    ``env_path`` is an injection seam for isolated tests. Production callers omit it and
    always use the path beside this module, independently of the current working
    directory. A missing file is allowed; malformed or unreadable files fail closed with
    an error that never includes their contents or absolute path.
    """
    paths = [LOCAL_ENV_PATH] if env_path is None else [Path(env_path)]
    if env_path is None and LOCAL_ENV_OVERRIDE_PATH.exists():
        paths.append(LOCAL_ENV_OVERRIDE_PATH)

    loaded = False
    protected = set(os.environ)
    for path in paths:
        if not path.exists():
            continue
        if not path.is_file():
            raise LocalEnvironmentError("local .env is not a regular file")
        _validate_env_file(path)
        # The base .env is conservative and never replaces process values.  The ignored
        # .env.local may refine local-development toggles such as the Selenium Manager
        # opt-in, but values that existed before this loader ran remain authoritative.
        override = path == LOCAL_ENV_OVERRIDE_PATH
        before = dict(os.environ)
        loaded = load_dotenv(
            dotenv_path=path,
            override=override,
            interpolate=True,
            encoding="utf-8-sig",
        ) or loaded
        if override:
            for key in protected:
                if key in before:
                    os.environ[key] = before[key]
    return loaded


def load_local_environment_for_entrypoint() -> bool:
    """Load local configuration and emit a value-free CLI error on failure."""
    try:
        load_local_environment()
    except LocalEnvironmentError as exc:
        print(f"configuration_error: {exc}", file=sys.stderr)
        return False
    return True
