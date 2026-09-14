"""Staging, atomic activation and rollback for a verified release (TDD #57).

Windows cannot overwrite the files of a running application, so this installer never tries.
The model is **immutable version directories plus one tiny atomic pointer**::

    <install_root>/
        versions/1.0.0/          # immutable, complete release payload
        versions/1.1.0/
        staging/                 # scratch; anything here is disposable
        current.json             # {"schema_version":1,"current":"1.1.0","previous":"1.0.0"}
        data/                    # user data: jobs DB, history, config, logs, output

Activating a release replaces a few dozen bytes of ``current.json`` via ``os.replace`` (atomic
on NTFS) instead of overwriting hundreds of files in place. Rolling back replaces those same
bytes again, pointing at a release that is still on disk and was already verified when it was
installed — a rollback never downloads an older remote release.

``data/`` is outside ``versions/`` by construction, so no activation, rollback or staging
cleanup in this module can reach the user's jobs database, history or output. Version
directories and user data are different lifetimes and are never mixed.

Everything here refuses to act on unverified input: ``stage_release`` takes an
``UpdateManifest`` that ``update_manifest.verify_manifest`` already accepted, re-proves the
package hash locally, and only then unpacks — into ``staging/``, never over the active
version. A staged tree that fails layout validation is discarded without the installed
application ever changing.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from update_manifest import (
    APP_ID,
    UpdateError,
    UpdateManifest,
    extract_package,
    parse_version,
    verify_package,
)

log = logging.getLogger(__name__)

INSTALL_STATE_SCHEMA_VERSION = 1
STATE_FILENAME = "current.json"

#: Metadata every release payload must carry at its root, so a correctly hashed but wrong or
#: empty archive can never become the active version.
RELEASE_METADATA_NAME = "release.json"

#: Minimum shape of a usable release payload. ``start_tradutor.py`` is the launcher entrypoint
#: the running installation actually invokes (see ``docs/technical/DOCUMENTACAO_TECNICA.md``).
REQUIRED_RELEASE_ENTRIES = (RELEASE_METADATA_NAME, "start_tradutor.py")


class InstallStateCorrupt(UpdateError):
    """The activation pointer is missing, unreadable or names a version that is not installed."""


class StagedReleaseInvalid(UpdateError):
    """The unpacked payload is not a usable release for this application and version."""


class ActivationFailed(UpdateError):
    """The requested version could not become current; the previous pointer still stands."""


class RollbackFailed(UpdateError):
    """There is no known-good previous release to fall back to."""


@dataclass(frozen=True)
class InstallState:
    current: str
    previous: str | None


def versions_dir(root: Path) -> Path:
    return Path(root) / "versions"


def staging_dir(root: Path) -> Path:
    return Path(root) / "staging"


def user_data_dir(root: Path) -> Path:
    return Path(root) / "data"


def install_state_path(root: Path) -> Path:
    return Path(root) / STATE_FILENAME


def version_dir(root: Path, version: str) -> Path:
    return versions_dir(root) / parse_version_text(version)


def parse_version_text(version: str) -> str:
    """Validate a version string before it is ever used as a path component."""
    parse_version(version)
    return version


def initialise_install(root: Path, *, version: str) -> InstallState:
    """Create an empty install layout whose current version is ``version``."""
    root = Path(root)
    for path in (versions_dir(root), staging_dir(root), user_data_dir(root)):
        path.mkdir(parents=True, exist_ok=True)
    state = InstallState(current=parse_version_text(version), previous=None)
    _write_install_state(root, state)
    return state


def read_install_state(root: Path) -> InstallState:
    """Resolve exactly one current release, or fail closed.

    Crash residue is ignored rather than interpreted: a half-written ``current.json.*`` temp
    file and a leftover ``staging/`` tree are both invisible here, because the pointer itself
    is only ever replaced atomically. What is never done is scanning ``versions/`` and picking
    something plausible to run.
    """
    path = install_state_path(root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise InstallStateCorrupt("no activation pointer in this installation") from exc
    except (ValueError, OSError) as exc:
        raise InstallStateCorrupt(f"activation pointer is unreadable: {exc}") from exc

    if not isinstance(raw, dict) or raw.get("schema_version") != INSTALL_STATE_SCHEMA_VERSION:
        raise InstallStateCorrupt("activation pointer has an unsupported schema")
    current = raw.get("current")
    previous = raw.get("previous")
    if not isinstance(current, str) or (previous is not None and not isinstance(previous, str)):
        raise InstallStateCorrupt("activation pointer has malformed version fields")
    try:
        parse_version_text(current)
    except UpdateError as exc:
        raise InstallStateCorrupt(f"activation pointer names an invalid version: {exc}") from exc
    if not version_dir(root, current).is_dir():
        raise InstallStateCorrupt(f"current version {current} is not installed")
    if previous is not None and not version_dir(root, previous).is_dir():
        previous = None  # the fallback was pruned; that is not a corrupt installation
    return InstallState(current=current, previous=previous)


def current_version_dir(root: Path) -> Path:
    return version_dir(root, read_install_state(root).current)


def _write_install_state(root: Path, state: InstallState) -> None:
    """Replace the pointer atomically: no reader can ever observe a half-written state."""
    root = Path(root)
    payload = {
        "schema_version": INSTALL_STATE_SCHEMA_VERSION,
        "current": state.current,
        "previous": state.previous,
    }
    handle, temp_name = tempfile.mkstemp(dir=str(root), prefix=STATE_FILENAME + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, install_state_path(root))
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def validate_release_payload(payload_root: Path, *, version: str, app_id: str = APP_ID) -> None:
    """Refuse a payload that is not this application at this version.

    A correctly hashed archive proves only that the bytes are the signed ones; it says nothing
    about whether they contain a usable application. An empty archive must not be able to
    become the active version.
    """
    for entry in REQUIRED_RELEASE_ENTRIES:
        if not (payload_root / entry).is_file():
            raise StagedReleaseInvalid(f"staged release is missing {entry}")
    try:
        metadata = json.loads((payload_root / RELEASE_METADATA_NAME).read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise StagedReleaseInvalid(f"staged release metadata is unreadable: {exc}") from exc
    if not isinstance(metadata, dict):
        raise StagedReleaseInvalid("staged release metadata is not an object")
    if metadata.get("app_id") != app_id:
        raise StagedReleaseInvalid(f"staged release is for {metadata.get('app_id')!r}")
    if metadata.get("version") != version:
        raise StagedReleaseInvalid(
            f"staged release declares version {metadata.get('version')!r}, manifest says {version}"
        )


def stage_release(root: Path, package_path: Path, manifest: UpdateManifest) -> Path:
    """Verify, unpack in isolation, validate, then install as an immutable version directory.

    The active installation is not touched at any point: on any failure the staging tree is
    removed and ``current.json`` still points where it did. Becoming *current* is a separate,
    explicit step (``activate``).
    """
    root = Path(root)
    version = manifest.version_text
    target = version_dir(root, version)
    if target.exists():
        raise ActivationFailed(f"version {version} is already installed")

    verify_package(package_path, sha256=manifest.package.sha256, size=manifest.package.size)

    staging_dir(root).mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(dir=str(staging_dir(root)), prefix=f"{version}-"))
    try:
        extract_package(package_path, workspace)
        validate_release_payload(workspace, version=version, app_id=manifest.app_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(workspace, target)
    except BaseException:
        shutil.rmtree(workspace, ignore_errors=True)
        raise
    log.info("update_staged", extra={"version": version})
    return target


def discard_staging(root: Path) -> None:
    """Remove disposable staging trees only. Installed versions and user data are untouched."""
    staging = staging_dir(root)
    if staging.is_dir():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)


def activate(root: Path, version: str) -> InstallState:
    """Make an installed version current, keeping the outgoing one as the fallback."""
    root = Path(root)
    version = parse_version_text(version)
    state = read_install_state(root)
    if not version_dir(root, version).is_dir():
        raise ActivationFailed(f"version {version} is not installed")
    log.info("update_activation_started", extra={"from": state.current, "to": version})
    try:
        validate_release_payload(version_dir(root, version), version=version)
        new_state = InstallState(current=version, previous=state.current)
        _write_install_state(root, new_state)
    except UpdateError:
        log.warning("update_activation_failed", extra={"version": version})
        raise
    except OSError as exc:
        log.warning("update_activation_failed", extra={"version": version})
        raise ActivationFailed(f"could not switch the activation pointer: {exc}") from exc
    log.info("update_activation_succeeded", extra={"version": version})
    return new_state


def rollback(root: Path) -> InstallState:
    """Return to the last known-good release that is already installed locally.

    The failed release stays on disk but is no longer reachable as a fallback, so a broken
    version cannot be re-activated by a second failure. User data is never rolled back.
    """
    root = Path(root)
    state = read_install_state(root)
    if state.previous is None:
        raise RollbackFailed("no previous known-good version is installed")
    if not version_dir(root, state.previous).is_dir():
        raise RollbackFailed(f"previous version {state.previous} is missing")
    restored = InstallState(current=state.previous, previous=None)
    try:
        _write_install_state(root, restored)
    except OSError as exc:
        raise RollbackFailed(f"could not restore the activation pointer: {exc}") from exc
    log.info("update_rollback_succeeded", extra={"restored": restored.current})
    return restored
