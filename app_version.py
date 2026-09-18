"""The one authoritative product version of Tradutor IA.

The repository has ~40 ``*_SCHEMA_VERSION`` constants; every one of them versions a *data
format* (manifest, install state, payload, protocol), not the product. Until now nothing said
which release of the application the user is running, which is exactly what an updater has to
compare. This module is that single answer, and ``test_remote_update.py`` fails if a second
product-version constant ever appears anywhere in the tree.

Why a module and not ``pyproject.toml``/``VERSION``: the repository has no packaging metadata
at all (no ``pyproject.toml``, no ``setup.py``) and every component is a flat top-level module
imported by name. A file that has to be parsed at runtime would need a parser and a fallback;
``from app_version import PRODUCT_VERSION`` needs neither, and the future Setup can read it with
one import.

**0.9.0** remains the product line, while ``BUILD_VERSION`` carries the ordered beta payload.
The updater's strict SemVer parser understands prereleases such as ``0.9.0-beta.33``.
"""

from __future__ import annotations

#: Version of the application payload this code belongs to. Single source of truth.
PRODUCT_VERSION = "0.9.0"

# Build identity is intentionally separate from the semver payload used by update
# comparisons; diagnostics and installers must identify the exact beta artifact.
BUILD_VERSION = "0.9.1-beta.2"

# Human-facing prerelease identifier derived from the canonical build value.
DISPLAY_VERSION = BUILD_VERSION

#: Identifier of the product itself; kept next to the version because both name the artifact.
PRODUCT_NAME = "tradutor-ia"


def user_agent() -> str:
    """Application identity for outbound update requests — no machine or user detail."""
    return f"{PRODUCT_NAME}/{PRODUCT_VERSION}"
