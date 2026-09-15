"""Yomu Sekai desktop shell backed by the local NiceGUI server.

This is a development foundation for the future frozen/installer entrypoint.  It
keeps the existing HTTP app intact and hosts it in Microsoft Edge WebView2 via
pywebview.  Session credentials cross the bridge only for immediate in-memory
sealing into a job-scoped DPAPI envelope; they are never logged or persisted
plaintext.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
import platform
import app_version
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_ICON_PATH = ROOT / "assets" / "branding" / "generated" / "yomu-sekai.ico"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = int(os.getenv("TRADUTOR_UI_PORT", "8080"))
_REQUEST_WINDOW_CLOSE = None


def export_diagnostics(destination: str) -> Path:
    """Write a small, sanitized diagnostics bundle without credentials."""
    requested = Path(destination).expanduser()
    if requested.suffix.lower() != ".zip":
        requested = requested / f"YomuSekai-Diagnostics-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.zip"
    requested.parent.mkdir(parents=True, exist_ok=True)
    runtime_roots = ([Path(os.getenv("TRADUTOR_RUNTIME_ROOT"))]
        if os.getenv("TRADUTOR_RUNTIME_ROOT") else []) + [
        Path(os.getenv("LOCALAPPDATA", "")) / "TradutorIA" / "runtime" / "diagnostics",
        Path(os.getenv("LOCALAPPDATA", "")) / "TradutorIA" / "runtime",
        Path(os.getenv("APPDATA", "")) / "TradutorIA" / "runtime",
    ]
    sensitive = __import__("re").compile(
        r"(?im)(authorization|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|service[_ -]?role|private[_ -]?key|password|apikey|api[_ -]?key)\s*[:=].*"
    )
    with zipfile.ZipFile(requested, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        manifest = {
            "product": "Yomu Sekai", "format": 1,
            "diagnostics_schema_version": 2,
            "app_version": os.getenv("TRADUTOR_APP_VERSION", getattr(app_version, "BUILD_VERSION", app_version.PRODUCT_VERSION)),
            "frozen": bool(getattr(sys, "frozen", False)),
            "platform": platform.platform(),
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        bundle.writestr("manifest.json", json.dumps(manifest, indent=2))
        seen: set[Path] = set()
        for root in runtime_roots:
            if not root.exists():
                continue
            for source in root.rglob("*"):
                # Auth envelopes are ciphertext, but are still credentials and must
                # never leave the machine in a diagnostics archive.
                if "runtime" in source.parts and "auth" in source.parts:
                    continue
                if not source.is_file() or source in seen or source.suffix.lower() not in {".log", ".json", ".jsonl", ".txt"}:
                    continue
                seen.add(source)
                try:
                    text = source.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                safe = sensitive.sub(r"\1=[REDACTED]", text)
                relative = source.relative_to(root).as_posix()
                bundle.writestr(f"runtime/{relative}", safe[:2_000_000])
        included = sorted(name for name in bundle.namelist() if name.startswith("runtime/"))
        index = {
            "app_log_present": any("app_current" in name for name in included),
            "route_log_present": any("routes_current" in name for name in included),
            "profile_log_present": any("profile_media_current" in name or "PROFILE_MEDIA_TRACE" in name for name in included),
            "source_traces_count": sum("source_" in name for name in included),
            "job_traces_count": sum("job_" in name for name in included),
            "pipeline_traces_count": sum("pipeline_" in name for name in included),
            "files_included": len(included),
        }
        bundle.writestr("diagnostics_index.json", json.dumps(index, indent=2))
    return requested


def _url(host: str, port: int) -> str:
    return f"http://{host}:{port}/"


def _desktop_url(host: str, port: int) -> str:
    return f"{_url(host, port)}?desktop=1"


def _load_external_provider_config(env: dict[str, str]) -> None:
    """Load optional provider configuration from the user's data directory.

    Frozen installs intentionally do not carry the development ``.env`` (or any
    OAuth material).  This used to make Drive-backed profile media silently fall
    back to initials/banner presets in the EXE even though the same account worked
    in the source checkout.  An installer may therefore use a user-created, external
    config file; values stay in the child process and are never bundled or logged.
    """
    candidates: list[Path] = []
    explicit = str(env.get("TRADUTOR_EXTERNAL_CONFIG", "") or "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    local_app_data = str(env.get("LOCALAPPDATA", "") or "").strip()
    if local_app_data:
        base = Path(local_app_data)
        candidates.extend((base / "YomuSekai" / ".env", base / "TradutorIA" / ".env"))
    allowed = {
        "COMMUNITY_STORAGE_PROVIDER", "COMMUNITY_DRIVE_ROOT_FOLDER_ID",
        "GOOGLE_OAUTH_TOKEN_PATH", "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_SCOPES",
    }
    try:
        from dotenv import dotenv_values
    except ImportError:
        return
    for path in candidates:
        if not path.is_file():
            continue
        try:
            values = dotenv_values(path)
        except (OSError, UnicodeError, ValueError):
            continue
        for key in allowed:
            value = values.get(key)
            if value and key not in env:
                env[key] = str(value)
        # The first readable config is authoritative; do not merge unrelated
        # installations' credentials.
        break


class DesktopApi:
    """Minimal WebView bridge for the local install identity.

    Only a public key, stable device id, and signatures leave this bridge.  The
    DPAPI-protected private key remains in the desktop process/user profile.
    """

    def __init__(self):
        from desktop_identity import InstallIdentity
        from secure_auth_context import AuthEnvelopeStore
        self.identity = InstallIdentity()
        runtime = Path(os.getenv("LOCALAPPDATA", "")) / "TradutorIA" / "runtime"
        self._auth_store = AuthEnvelopeStore(runtime)

    def get_install_identity(self) -> dict[str, str]:
        return {"device_id": self.identity.device_id(), "public_key": self.identity.public_key()}

    def sign_device_challenge(self, message: str) -> dict[str, str]:
        if not isinstance(message, str) or not message or len(message) > 4096:
            raise ValueError("invalid_challenge")
        return {"signature": self.identity.sign(message)}

    def set_auth_context(self, job_id: str, access_token: str, expires_at: float | None = None,
                         user_id: str = "") -> dict[str, object]:
        """Seal a fresh access token without exposing it to job/child arguments."""
        from secure_auth_context import AuthContextError
        try:
            path = self._auth_store.seal(job_id, access_token, expires_at=expires_at, user_id=user_id)
        except AuthContextError:
            raise
        context = self._auth_store.acquire(job_id, path)
        return {"job_id": str(job_id), "envelope_ready": True,
                "envelope_expires_at": context.envelope_expires_at}

    def clear_auth_context(self, job_id: str) -> dict[str, object]:
        self._auth_store.cleanup(job_id)
        return {"job_id": str(job_id), "envelope_ready": False}

    def request_shutdown(self) -> dict[str, bool]:
        """Ask the native shell to close after a verified installer was spawned."""
        callback = globals().get("_REQUEST_WINDOW_CLOSE")
        if not callable(callback):
            raise RuntimeError("desktop_shutdown_unavailable")
        callback()
        return {"requested": True}


def _healthy(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(_url(host, port), timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) != 0


def _start_server(host: str, port: int, *, auth_diagnostics: bool = False) -> subprocess.Popen[bytes] | None:
    if _healthy(host, port):
        return None
    if not _port_free(host, port):
        raise RuntimeError(f"A porta {host}:{port} está ocupada por outro serviço.")
    python = Path(sys.executable)
    if not python.exists():
        raise RuntimeError("Python do ambiente beta não encontrado.")
    env = os.environ.copy()
    _load_external_provider_config(env)
    # The frozen bundle carries only browser-public Supabase settings.  Inject
    # them into the owned child environment; private server/provider secrets are
    # intentionally never read from this file.
    public_config = ROOT / "config" / "public-runtime.json"
    if public_config.is_file():
        try:
            payload = json.loads(public_config.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or set(payload) - {"supabase_url", "publishable_key"}:
                raise ValueError("invalid public runtime config")
            if payload.get("supabase_url") and payload.get("publishable_key"):
                env.setdefault("SUPABASE_URL", str(payload["supabase_url"]))
                env.setdefault("SUPABASE_PUBLISHABLE_KEY", str(payload["publishable_key"]))
                env.setdefault("SUPABASE_EXPECTED_AUDIENCE", "authenticated")
                # Provider selection is public wiring, not a credential.  Frozen
                # runtimes must opt into the remote authorizer explicitly; leaving this
                # unset would be interpreted as an invalid/missing provider and abort the
                # UI child before the login screen can start.
                env.setdefault("BETA_LICENSE_PROVIDER", "supabase")
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            pass
    env["TRADUTOR_UI_HOST"] = host
    env["TRADUTOR_UI_PORT"] = str(port)
    env["TRADUTOR_RUNTIME_MODE"] = "desktop"
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if getattr(sys, "frozen", False):
        command = [str(python), "--internal-child", "ui"]
        if auth_diagnostics:
            command.append("--auth-diagnostics")
    else:
        command = [str(python), "-u", str(ROOT / "app_ui.py")]
    return subprocess.Popen(
        command,
        cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )


def _wait_ready(host: str, port: int, process: subprocess.Popen[bytes] | None, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _healthy(host, port):
            return
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"Servidor local encerrou durante o startup (exit={process.returncode}).")
        time.sleep(0.25)
    raise TimeoutError("Servidor local não ficou pronto dentro do tempo esperado.")


def shutdown_owned_runtime(process: subprocess.Popen[bytes] | None) -> None:
    """Cooperatively stop only the server process owned by this desktop shell."""
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=4)


# Backwards-compatible private name used by the existing unit tests/callers.
_stop_server = shutdown_owned_runtime


def _set_windows_app_user_model_id() -> None:
    """Give the native window a stable Windows taskbar identity."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "YomuSekai.Desktop"
        )
    except (AttributeError, OSError):
        # Taskbar identity is an enhancement; the shell remains functional if the
        # host API is unavailable (for example on non-Windows CI runners).
        return


def run(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *, lifecycle_selftest: bool = False, auth_diagnostics: bool = False) -> int:
    try:
        import webview
    except ImportError as exc:  # pragma: no cover - environment diagnostic
        raise SystemExit("WebView2 indisponível: instale pywebview no .venv-beta.") from exc

    server = _start_server(host, port, auth_diagnostics=auth_diagnostics)
    global _REQUEST_WINDOW_CLOSE
    try:
        _wait_ready(host, port, server)
        _set_windows_app_user_model_id()
        window = webview.create_window(
            "Yomu Sekai", _desktop_url(host, port), width=1440, height=900,
            min_size=(980, 680), confirm_close=True, js_api=DesktopApi(),
        )
        lifecycle = {"closing": False, "closed": False}

        def on_closing() -> None:
            lifecycle["closing"] = True

        def on_closed() -> None:
            lifecycle["closed"] = True

        _REQUEST_WINDOW_CLOSE = lambda: window.destroy()

        window.events.closing += on_closing
        window.events.closed += on_closed
        if lifecycle_selftest:
            # Exercise the real Window close path; the same events fire as for the
            # user pressing the native close button.  This hook is CLI-only.
            def request_window_close() -> None:
                try:
                    window.destroy()
                except Exception:
                    pass

            webview_start_callback = request_window_close
        else:
            webview_start_callback = None
        webview.start(func=webview_start_callback, gui="edgechromium", debug=False,
                      icon=str(APP_ICON_PATH))
        if lifecycle_selftest and not lifecycle["closed"]:
            return 1
        return 0
    finally:
        _REQUEST_WINDOW_CLOSE = None
        shutdown_owned_runtime(server)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Yomu Sekai WebView2 desktop shell")
    args = list(sys.argv[1:] if argv is None else argv)
    # Frozen builds keep the worker/UI child protocol from start_tradutor.  Route
    # those private commands before parsing desktop-only options so subprocesses
    # spawned by the frozen runtime remain compatible with the shared entrypoint.
    if args and args[0] in {"--internal-child", "--internal-selftest"}:
        import start_tradutor
        return start_tradutor.main(args)
    if args and args[0] == "--jwks-network-diagnostics":
        from jwks_network_diagnostics import run as run_jwks_network_diagnostics
        return run_jwks_network_diagnostics()
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--lifecycle-selftest", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--auth-diagnostics", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--export-diagnostics", metavar="DESTINO", help="exporta um ZIP sanitizado de diagnósticos")
    parsed = parser.parse_args(args)
    if parsed.host not in {"127.0.0.1", "localhost"}:
        parser.error("o desktop shell aceita somente loopback")
    if parsed.auth_diagnostics:
        os.environ["TRADUTOR_AUTH_DIAGNOSTICS"] = "1"
    if parsed.export_diagnostics:
        print(export_diagnostics(parsed.export_diagnostics))
        return 0
    return run(parsed.host, parsed.port, lifecycle_selftest=parsed.lifecycle_selftest, auth_diagnostics=parsed.auth_diagnostics)


if __name__ == "__main__":
    raise SystemExit(main())
