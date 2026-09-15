"""Startup update orchestration: the seam between the network and the trust chain (TDD #58).

This is the only module that knows the whole sequence — fetch, verify, download, stage,
activate, prove the new release actually starts, roll back if it does not — and it owns none of
those steps itself. ``update_transport`` moves bytes, ``update_manifest`` decides what may be
trusted, ``update_installer`` owns the on-disk layout. Nothing here re-implements a security
gate, so no remote integration can weaken one.

**Where it runs.** ``start_tradutor.py all`` calls ``check_before_start`` *before* the worker
and the UI exist. Windows locks the files of a running application, so the only safe moment to
change which version is current is the moment when no part of it is running. There is no
mid-session update and no hot swap; a translation job can therefore never be running while a
version is activated.

**Bootstrap model.** Releases are immutable directories under ``versions/`` plus one atomic
pointer, so nothing ever overwrites a running file — including the launcher. Whichever
launcher started the check plays the bootstrap role: after it activates a newer release, it
hands off (``subprocess.run``) to *that* release's ``start_tradutor.py`` and waits, so exactly
one process owns the worker supervisor. The handoff sets ``TRADUTOR_IA_UPDATE_CHECKED`` so the
new payload does not check again and cannot loop.

The launcher that performs the handoff is still part of a versioned payload, which means it is
itself updated only by the next Setup/reinstall. Genuine self-replacement of a stable
bootstrap binary is **deferred to the Setup phase**, not faked here: no code copies over a
running executable, renames it, or schedules a shell command to do so later.

**Fail-closed, but not fail-fatal.** Two very different failures must not be confused:

- the network is down, or the manifest cannot be authenticated → the installed release is
  known-good and still starts, with an honest status. An attacker who controls the host must
  not be able to *deny service* either, and above all a tampered manifest claiming
  ``minimum_version: 999.0.0`` must not be able to block the application: ``minimum_version``
  is read only from a payload whose signature already verified.
- a *verified* manifest says the installed release is below ``minimum_version`` and it could
  not be replaced → the outdated release does not start.

**Startup health vs. worker crash.** The probe here answers exactly one question: can the newly
activated release reach a healthy initial startup at all? If not, the activation is rolled back.
Once it has passed, every later worker crash belongs to the TDD #53 supervisor, which handles it
with its bounded restart policy — a crashing worker never rolls the application version back.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

import update_installer
import update_manifest
import update_transport
from app_version import PRODUCT_VERSION

log = logging.getLogger(__name__)

#: Set on the child that a handoff starts, so the updated payload does not re-check and the
#: two launchers cannot bounce updates back and forth.
HANDOFF_ENV = "TRADUTOR_IA_UPDATE_CHECKED"

#: Public beta manifest endpoint. Test/dev callers may still override it with the environment.
MANIFEST_URL_ENV = "TRADUTOR_IA_UPDATE_MANIFEST_URL"
DEFAULT_MANIFEST_URL = "https://raw.githubusercontent.com/HenriquePvAr/YomuSekai-Releases/main/updates/beta/latest.json"

#: Overrides install-root detection; the future Setup can point at the real install location.
INSTALL_ROOT_ENV = "TRADUTOR_IA_INSTALL_ROOT"

#: A release that cannot reach a healthy startup within this window is treated as broken. Long
#: enough for a cold Windows start on a slow disk, short enough that a hung release does not
#: leave the user staring at nothing.
STARTUP_HEALTH_TIMEOUT = 120.0


class StartupUnhealthy(update_manifest.UpdateError):
    """The freshly activated release did not reach a healthy startup."""


@dataclass(frozen=True)
class UpdateStatus:
    """What the startup check concluded, in terms the UI and the logs can both use."""

    state: str
    version: str
    detail: str = ""
    payload_dir: Path | None = None

    #: States in which the application must not start: only ever reached through a *verified*
    #: manifest.
    BLOCKING = frozenset({"mandatory_update_required", "bootstrap_too_old"})

    @property
    def can_launch(self) -> bool:
        return self.state not in self.BLOCKING

    @property
    def user_message(self) -> str:
        """Plain wording for the user; crypto detail stays in the log."""
        return {
            "skipped": "",
            "not_configured": "",
            "up_to_date": "",
            "updated": "O Tradutor IA foi atualizado.",
            "trust_not_configured": "Verificação de atualização indisponível nesta versão.",
            "network_error": "Não foi possível verificar atualizações agora.",
            "verification_failed": "Não foi possível validar a atualização.",
            "update_failed": "A atualização não pôde ser aplicada; a versão atual continua.",
            "mandatory_update_required": "Esta versão precisa ser atualizada para continuar.",
            "bootstrap_too_old": "Esta atualização exige uma reinstalação do programa.",
        }.get(self.state, "")


def detect_install_root(payload_dir: Path) -> Path | None:
    """Return the install root if this code is running as an installed version payload.

    ``<root>/versions/<version>/`` is the layout ``update_installer`` creates, so its shape is
    the detection: no configuration file, and a plain repository clone (the current developer
    setup) simply is not an installation and never pretends to be one.
    """
    override = os.environ.get(INSTALL_ROOT_ENV, "").strip()
    if override:
        root = Path(override)
        return root if update_installer.install_state_path(root).is_file() else None
    payload_dir = Path(payload_dir).resolve()
    root = payload_dir.parent.parent
    if payload_dir.parent.name == "versions" and update_installer.install_state_path(root).is_file():
        return root
    return None


def current_version(root: Path | None) -> str:
    """The version that is running: the activation pointer when installed, else this payload."""
    if root is not None:
        try:
            return update_installer.read_install_state(root).current
        except update_manifest.UpdateError:
            log.warning("update_install_state_unusable")
    return PRODUCT_VERSION


def default_start_probe(payload_dir: Path, *, timeout: float = STARTUP_HEALTH_TIMEOUT) -> None:
    """Prove a release can start by running its own launcher self-check, bounded in time.

    ``start_tradutor.py selftest`` loads the local environment and imports the runtime the
    worker and the UI need, then exits — it starts no worker, opens no UI and runs no
    translation job. That is the smallest evidence that answers "is this payload runnable",
    and it is the payload's *own* code answering, which is what an update has to validate.
    """
    payload_dir = Path(payload_dir)
    try:
        result = subprocess.run(
            [sys.executable, str(payload_dir / "start_tradutor.py"), "selftest"],
            cwd=str(payload_dir), timeout=timeout, capture_output=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise StartupUnhealthy(f"release did not start within {timeout}s") from exc
    except OSError as exc:
        raise StartupUnhealthy(f"release could not be started: {exc}") from exc
    if result.returncode != 0:
        raise StartupUnhealthy(f"release self-check failed with status {result.returncode}")


def run_update_check(
    root: Path,
    *,
    manifest_url: str,
    trusted_keys: Mapping[str, str] | None = None,
    transport: update_transport.UpdateTransport | None = None,
    start_probe: Callable[[Path], None] | None = None,
    bootstrap_version: str = PRODUCT_VERSION,
) -> UpdateStatus:
    """Run the whole startup update decision against one installation. Never raises."""
    root = Path(root)
    installed = current_version(root)
    if not manifest_url:
        return UpdateStatus("not_configured", installed, "no update channel configured")
    if trusted_keys is None:
        try:
            trusted_keys = update_manifest.load_trusted_keys()
        except update_manifest.UpdateTrustNotConfigured as exc:
            log.info("update_trust_not_configured")
            return UpdateStatus("trust_not_configured", installed, str(exc))

    transport = transport or update_transport.UpdateTransport()
    start_probe = start_probe or default_start_probe

    try:
        raw = transport.fetch_manifest(manifest_url)
    except update_transport.TransportError as exc:
        log.warning("update_check_network_error", extra={"error": type(exc).__name__})
        return UpdateStatus("network_error", installed, str(exc))

    try:
        manifest = update_manifest.verify_manifest(raw, trusted_keys=trusted_keys)
    except update_manifest.UpdateError as exc:
        # Nothing in these bytes is trustworthy, so nothing in them may block the launch.
        log.warning("update_verification_failed", extra={"error": type(exc).__name__})
        return UpdateStatus("verification_failed", installed, str(exc))

    decision = update_manifest.decide_update(installed, manifest)
    if not decision.should_install:
        return UpdateStatus("up_to_date", installed, decision.reason)

    if manifest.minimum_bootstrap_version > update_manifest.parse_version(bootstrap_version):
        # Signed, so it may block: an old bootstrap must not launch a payload it cannot host.
        log.warning("update_bootstrap_too_old", extra={"version": manifest.version_text})
        return UpdateStatus("bootstrap_too_old", installed, "reinstall required")

    try:
        payload_dir = _install_release(root, manifest, transport, start_probe)
    except update_manifest.UpdateError as exc:
        log.warning("update_failed", extra={"error": type(exc).__name__})
        if decision.mandatory:
            return UpdateStatus("mandatory_update_required", installed, str(exc))
        return UpdateStatus("update_failed", installed, str(exc))

    log.info("update_applied", extra={"version": manifest.version_text})
    return UpdateStatus("updated", manifest.version_text, payload_dir=payload_dir)


def _install_release(
    root: Path,
    manifest: update_manifest.UpdateManifest,
    transport: update_transport.UpdateTransport,
    start_probe: Callable[[Path], None],
) -> Path:
    """Download, stage, activate and health-gate one verified release, or leave nothing behind.

    Any failure before activation cannot affect the installation at all; a failure *after* it
    rolls the pointer back to the release that was already known good. User data lives outside
    ``versions/`` and is never part of either path.
    """
    downloads = update_installer.staging_dir(root) / "downloads"
    package_path = downloads / manifest.package.filename
    package_path.unlink(missing_ok=True)
    try:
        transport.download_package(
            manifest.package.url, package_path,
            sha256=manifest.package.sha256, size=manifest.package.size,
        )
        payload_dir = update_installer.stage_release(root, package_path, manifest)
    finally:
        package_path.unlink(missing_ok=True)

    update_installer.activate(root, manifest.version_text)
    try:
        start_probe(payload_dir)
    except Exception as exc:  # a broken release must never take the installation down with it
        log.warning("update_startup_unhealthy", extra={"version": manifest.version_text})
        update_installer.rollback(root)
        raise StartupUnhealthy(f"activated release failed to start: {exc}") from exc
    return payload_dir


def check_before_start(payload_dir: Path, **kwargs) -> UpdateStatus:
    """Launcher entry point: resolve the installation, then run the check for it."""
    if os.environ.get(HANDOFF_ENV) == "1":
        return UpdateStatus("skipped", PRODUCT_VERSION, "already checked by the launcher")
    root = detect_install_root(payload_dir)
    if root is None:
        # A repository clone is not an installation: there is no versions/ layout to activate
        # into, so there is nothing an update could safely do here.
        return UpdateStatus("not_configured", PRODUCT_VERSION, "not an installed layout")
    kwargs.setdefault("manifest_url", os.environ.get(MANIFEST_URL_ENV, DEFAULT_MANIFEST_URL).strip())
    return run_update_check(root, **kwargs)
