"""Verified Windows-installer update handoff for the in-app Atualizações surface.

The manifest trust and byte-integrity checks remain in :mod:`update_manifest` and
:mod:`update_transport`.  This module only handles the installer artifact's bounded staging
and safe process handoff; it never accepts an arbitrary executable path from the UI.
"""
from __future__ import annotations

import os
import json
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


def _handoff_script() -> str:
    """Return the small external PowerShell handoff used by the frozen app.

    It deliberately lives outside the install root (the update cache) and waits for
    explicitly supplied PIDs before starting the already verified installer.
    """
    return r'''param(
  [Parameter(Mandatory=$true)][string]$Installer,
  [Parameter(Mandatory=$true)][string]$LogPath,
  [int[]]$OwnedPid = @(),
  [int]$TimeoutSeconds = 30,
  [string]$InstallerArgsJson = '[]'
)
$ErrorActionPreference = 'Stop'
function Write-Log([string]$Event, [string]$Detail = '') {
  $line = "$(Get-Date -Format o) $Event" + ($(if($Detail){" $Detail"}else{''}))
  Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
}
try {
  [string[]]$InstallerArgs = @($InstallerArgsJson | ConvertFrom-Json)
  Write-Log 'HANDOFF_START' ("pid_count=" + $OwnedPid.Count)
  $deadline = (Get-Date).AddSeconds([Math]::Max(1, $TimeoutSeconds))
  do {
    $alive = @($OwnedPid | Where-Object { $_ -gt 0 -and (Get-Process -Id $_ -ErrorAction SilentlyContinue) })
    if ($alive.Count -eq 0) { break }
    Start-Sleep -Milliseconds 100
  } while ((Get-Date) -lt $deadline)
  if ($alive.Count -ne 0) { Write-Log 'HANDOFF_TIMEOUT' ("alive=" + ($alive -join ',')); exit 71 }
  if (-not (Test-Path -LiteralPath $Installer -PathType Leaf)) { Write-Log 'INSTALLER_MISSING'; exit 72 }
  Write-Log 'INSTALLER_SPAWN' 'after_owned_pids_exit'
  $proc = Start-Process -FilePath $Installer -ArgumentList $InstallerArgs -WorkingDirectory (Split-Path -Parent $Installer) -PassThru
  Write-Log 'INSTALLER_SPAWNED' ("pid=" + $proc.Id)
  exit 0
} catch {
  Write-Log 'HANDOFF_ERROR' $_.Exception.GetType().Name
  exit 73
}
'''


def spawn_installer_after_processes_exit(
    path: Path, *, owned_pids: list[int], silent: bool = True,
    timeout_seconds: int = 30, log_path: Path | None = None,
) -> subprocess.Popen:
    """Start an external handoff process and launch the verified installer only after exit.

    The helper is staged beside the downloaded installer, not below ``{app}``, so it can
    survive the parent shutdown and remain runnable while Inno replaces the installation.
    """
    path = Path(path)
    if path.suffix.casefold() != ".exe" or not path.is_file():
        raise InstallerUpdateError("verified installer is missing")
    helper_dir = path.parent
    helper_dir.mkdir(parents=True, exist_ok=True)
    script = helper_dir / f"{path.stem}.handoff.ps1"
    if log_path is None:
        log_path = helper_dir / f"{path.stem}.handoff.log"
    script.write_text(_handoff_script(), encoding="utf-8", newline="\r\n")
    args = ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-Installer", str(path), "-LogPath", str(log_path), "-TimeoutSeconds", str(int(timeout_seconds))]
    for pid in sorted({int(pid) for pid in owned_pids if int(pid) > 0}):
        args.extend(["-OwnedPid", str(pid)])
    args.extend(["-InstallerArgsJson", json.dumps(installer_arguments(silent=silent), separators=(",", ":"))])
    creationflags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    ) if os.name == "nt" else 0
    return subprocess.Popen(
        ["powershell.exe", *args], cwd=str(helper_dir), shell=False, close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )


def discard_partial_download(version: str, *, data_root: Path | None = None) -> None:
    partial, _ = staging_paths(version, data_root=data_root)
    partial.unlink(missing_ok=True)
