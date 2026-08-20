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

**0.9.0** is deliberate. The format is strict ``MAJOR.MINOR.PATCH`` because
``update_manifest.parse_version`` compares releases as integer tuples and rejects pre-release
suffixes — a version that cannot be ordered cannot gate an update. ``0.x`` carries the
pre-release meaning instead: this is external Scan Beta preparation, not a public stable 1.0,
and claiming ``1.0.0`` would say something false. The first public stable release is what turns
this into ``1.0.0``.
"""

from __future__ import annotations

#: Version of the application payload this code belongs to. Single source of truth.
PRODUCT_VERSION = "0.9.0"

#: Identifier of the product itself; kept next to the version because both name the artifact.
PRODUCT_NAME = "tradutor-ia"


def user_agent() -> str:
    """Application identity for outbound update requests — no machine or user detail."""
    return f"{PRODUCT_NAME}/{PRODUCT_VERSION}"
