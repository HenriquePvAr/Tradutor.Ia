"""Verified Windows-installer update handoff for the in-app Atualizações surface.

The manifest trust and byte-integrity checks remain in :mod:`update_manifest` and
:mod:`update_transport`.  This module only handles the installer artifact's bounded staging
and safe process handoff; it never accepts an arbitrary executable path from the UI.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import update_installer
import update_manifest
import update_transport

INSTALLER_ARTIFACT_TYPE = "windows-installer"
INSTALLER_SUFFIX = "-Setup-x64.exe"
DEFAULT_DATA_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "YomuSekai"


class InstallerUpdateError(update_manifest.UpdateError):
    """The verified installer could not be staged or handed off."""


def update_root(data_root: Path | None = None) -> Path:
    return Path(data_root or DEFAULT_DATA_ROOT) / "updates"


def installer_filename(version: str) -> str:
    update_manifest.parse_version(version)
    return f"YomuSekai-{version}{INSTALLER_SUFFIX}"


def staging_paths(version: str, *, data_root: Path | None = None) -> tuple[Path, Path]:
    name = installer_filename(version)
    directory = update_root(data_root) / version
    return directory / f"{name}.download", directory / name


def download_verified_installer(
    manifest: update_manifest.UpdateManifest,
    transport: update_transport.UpdateTransport,
    *,
    data_root: Path | None = None,
    progress_callback=None,
    cancel_event=None,
) -> Path:
    """Download an installer into a disposable ``.download`` file, then promote atomically."""
    if manifest.artifact_type != INSTALLER_ARTIFACT_TYPE:
        raise InstallerUpdateError("manifest is not a Windows installer artifact")
    expected_name = installer_filename(manifest.version_text)
    if manifest.package.filename != expected_name:
        raise InstallerUpdateError("installer filename does not match the signed version")
    partial, final = staging_paths(manifest.version_text, data_root=data_root)
    partial.unlink(missing_ok=True)
    final.unlink(missing_ok=True)
    try:
        kwargs = {"sha256": manifest.package.sha256, "size": manifest.package.size}
        if progress_callback is not None or cancel_event is not None:
            kwargs.update(progress_callback=progress_callback, cancel_event=cancel_event)
        try:
            transport.download_package(manifest.package.url, partial, **kwargs)
        except TypeError:
            # Keep small test transports and third-party adapters compatible with the original
            # two-keyword contract; production transport supports progress/cancellation.
            if progress_callback is not None or cancel_event is not None:
                raise
            transport.download_package(manifest.package.url, partial, sha256=manifest.package.sha256, size=manifest.package.size)
        os.replace(partial, final)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return final


def installer_arguments(*, silent: bool = True) -> list[str]:
    """Arguments supported by the canonical Inno Setup script for an in-place upgrade."""
    if not silent:
        return []
    return ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"]


def spawn_verified_installer(path: Path, *, silent: bool = True) -> subprocess.Popen:
    """Start only a previously verified staged installer, without shell interpretation."""
    path = Path(path)
    if path.suffix.casefold() != ".exe" or not path.is_file():
        raise InstallerUpdateError("verified installer is missing")
    return subprocess.Popen(
        [str(path), *installer_arguments(silent=silent)],
        cwd=str(path.parent), shell=False, close_fds=True,
    )


def discard_partial_download(version: str, *, data_root: Path | None = None) -> None:
    partial, _ = staging_paths(version, data_root=data_root)
    partial.unlink(missing_ok=True)
