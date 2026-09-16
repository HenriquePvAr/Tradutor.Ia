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
import threading
import app_version
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_ICON_PATH = ROOT / "assets" / "branding" / "generated" / "yomu-sekai.ico"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = int(os.getenv("TRADUTOR_UI_PORT", "8080"))
_REQUEST_WINDOW_CLOSE = None
NATIVE_AD_DIAGNOSTIC_BUILD_ID = "native-ad-product-beta"
_NATIVE_POC_LOG_LOCK = threading.Lock()


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
        self._last_native_bounds_state = None
        self._native_placement = None
        self._native_bounds_lock = threading.Lock()
        self._last_d4_slot_diag = None

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

    def native_ad_set_bounds(self, x: float, y: float, width: float, height: float, visible: bool = True) -> dict[str, bool]:
        surface = globals().get("_NATIVE_AD_SURFACE")
        if surface is None:
            return {"available": False}
        state = (round(float(x)), round(float(y)), max(0, round(float(width))), max(0, round(float(height))), bool(visible))
        with self._native_bounds_lock:
            if state == self._last_native_bounds_state:
                return {"available": True, "ready": bool(getattr(surface, "_ready", False))}
            self._last_native_bounds_state = state
        _native_poc_log("BOUNDS_STATE_CHANGED", x=state[0], y=state[1], width=state[2], height=state[3], visible=state[4])
        return {"available": bool(surface.set_bounds(*state)), "ready": bool(getattr(surface, "_ready", False))}

    def native_ad_set_placement(self, route: str, url: str, width: int, height: int) -> dict[str, bool]:
        surface = globals().get("_NATIVE_AD_SURFACE")
        if surface is None:
            return {"available": False}
        safe_route = str(route)[:40]
        safe_url = str(url)
        if not safe_url.startswith("https://henriquepvar.github.io/ad/"):
            return {"available": False}
        state = (safe_route, safe_url, max(1, int(width)), max(1, int(height)))
        if state == self._native_placement:
            return {"available": True}
        self._native_placement = state
        return {"available": bool(surface.set_placement(*state))}

    def native_ad_d4_event(self, event: str, payload: object = None) -> dict[str, bool]:
        """Record only allow-listed D4 geometry breadcrumbs from the UI."""
        allowed = {"D4_PYWEBVIEWREADY", "D4_ANCHOR_FOUND", "D4_ACTIVE_SLOT", "D4_BOUNDS_JS_CALL", "D4_INITIAL_BOUNDS_SEND"}
        name = str(event)[:64]
        if name in allowed:
            fields = payload if isinstance(payload, dict) else {}
            safe = {k: fields[k] for k in ("route", "found", "connected", "display", "visibility", "x", "y", "width", "height", "dpr", "visible") if k in fields}
            if name == "D4_ACTIVE_SLOT":
                diag_key = tuple(sorted((str(k), str(v)) for k, v in safe.items()))
                if diag_key == self._last_d4_slot_diag:
                    return {"recorded": True}
                self._last_d4_slot_diag = diag_key
            _native_poc_log(name, **safe)
        return {"recorded": name in allowed}

    def native_ad_hide(self) -> dict[str, bool]:
        surface = globals().get("_NATIVE_AD_SURFACE")
        return {"available": bool(surface and surface.hide())}


class AdsDiagnosticsApi:
    """Sanitized diagnostics sink used only by the opt-in ads WebView probe."""

    _allowed = {
        "mode", "url", "user_agent", "page_loaded", "iframe_created", "iframe_src",
        "iframe_load", "iframe_metrics", "slot_states", "resource_hosts", "javascript_enabled",
        "local_storage", "session_storage", "error_class", "blocked_reason",
    }

    def __init__(self):
        base = Path(os.getenv("LOCALAPPDATA", "")) / "YomuSekai" / "logs"
        base.mkdir(parents=True, exist_ok=True)
        self.path = base / "ads-diagnostic.json"

    def record(self, payload: object) -> dict[str, bool]:
        if not isinstance(payload, dict):
            raise ValueError("invalid_diagnostic_payload")
        safe = {k: payload[k] for k in self._allowed if k in payload}
        safe["recorded_at"] = datetime.now(timezone.utc).isoformat()
        self.path.write_text(json.dumps(safe, ensure_ascii=True, indent=2), encoding="utf-8")
        return {"recorded": True}


class NativeAdSurface:
    """Opt-in Home-only WebView2 child surface for the ads POC.

    This deliberately uses the WinForms/WebView2 objects already loaded by
    pywebview. It is never created during normal startup and does not replace
    pywebview's primary browser control.
    """

    URL = "https://henriquepvar.github.io/ad/banner-728x90.html"

    def __init__(self, window):
        from System import Uri
        from System.Drawing import Color, Point, Size
        from Microsoft.Web.WebView2.WinForms import CoreWebView2CreationProperties, WebView2

        self.window = window
        self.form = getattr(window, "native", None)
        if self.form is None or not hasattr(self.form, "Controls"):
            _native_poc_log("MAIN_FORM_LOOKUP_RESULT", found=False, error="MAIN_FORM_NOT_FOUND")
            raise RuntimeError("native_parent_unavailable")
        _native_poc_log("MAIN_FORM_LOOKUP_RESULT", found=True, type=type(self.form).__name__, handle=self.form.Handle.ToInt32())
        _native_poc_log("MAIN_FORM_CLIENT_SIZE", width=self.form.ClientSize.Width, height=self.form.ClientSize.Height)
        self._Point = Point
        self._Size = Size
        self._Uri = Uri
        self.control = WebView2()
        _native_poc_log("WEBVIEW_CONTROL_CREATE_SUCCESS", type=type(self.control).__name__)
        props = CoreWebView2CreationProperties()
        local = os.getenv("LOCALAPPDATA", "")
        if local:
            props.UserDataFolder = str(Path(local) / "YomuSekai" / "ad-webview")
        self.control.CreationProperties = props
        self.control.Visible = False
        self.control.TabStop = False
        self.control.CoreWebView2InitializationCompleted += self._on_initialized
        try:
            self.control.NavigationCompleted += self._on_navigation_completed
        except Exception:
            _native_poc_log("NATIVE_AD_ERROR", error="NAVIGATION_EVENT_UNAVAILABLE")
        self.form.Controls.Add(self.control)
        self.control.BackColor = Color.Black
        _native_poc_log("AD_HOST_CREATE_SUCCESS", handle=self.control.Handle.ToInt32(), parent=self.form.Handle.ToInt32(), visible=self.control.Visible)
        try:
            tree = []
            for i, item in enumerate(self.form.Controls):
                tree.append({"index": i, "type": type(item).__name__, "dock": str(item.Dock), "visible": bool(item.Visible), "bounds": str(item.Bounds)})
            _native_poc_log("MAIN_CONTROL_TREE", controls=json.dumps(tree, ensure_ascii=True))
        except Exception:
            _native_poc_log("MAIN_CONTROL_TREE", error="CONTROL_TREE_UNAVAILABLE")
        self._disposed = False
        self._ready = False
        self._pending_bounds = None
        self._applied_bounds = None
        self._placement = ("home", self.URL, 728, 90)
        _native_poc_log("COREWEBVIEW2_INIT_START")
        self.control.EnsureCoreWebView2Async(None)

    def _on_initialized(self, sender, args):
        if getattr(args, "IsSuccess", False):
            self._ready = True
            _native_poc_log("COREWEBVIEW2_INIT_SUCCESS", available=True)
            self.control.CoreWebView2.NewWindowRequested += self._on_new_window
            self.control.CoreWebView2.Navigate(self._placement[1])
            _native_poc_log("NAVIGATION_START", host="henriquepvar.github.io", path="/ad/banner-728x90.html")
            if self._pending_bounds:
                self.set_bounds(*self._pending_bounds)
        else:
            _native_poc_log("COREWEBVIEW2_INIT_FAILURE", error="CORE_INIT_FAILED")

    def _on_new_window(self, sender, args):
        # Keep the main Yomu surface intact; provider clicks may open externally.
        try:
            import webbrowser
            webbrowser.open(str(args.Uri))
            args.Handled = True
        except Exception:
            args.Handled = True

    def _on_navigation_completed(self, sender, args):
        _native_poc_log("NAVIGATION_COMPLETED", success=bool(getattr(args, "IsSuccess", False)), web_error=str(getattr(args, "WebErrorStatus", "unknown")))

    def set_bounds(self, x, y, width, height, visible=True):
        if self._disposed:
            return False
        try:
            bounds = (int(x), int(y), max(0, int(width)), max(0, int(height)), bool(visible))
            if bounds == self._pending_bounds or bounds == self._applied_bounds:
                return True
            self._pending_bounds = bounds
            _native_poc_log("NATIVE_BOUNDS_CALCULATED", x=bounds[0], y=bounds[1], width=bounds[2], height=bounds[3], visible=bounds[4])
            def apply():
                if self._disposed:
                    return
                _, _, w, h, show = bounds
                if bounds == self._applied_bounds:
                    return
                self.control.Location = self._Point(bounds[0], bounds[1])
                self.control.Size = self._Size(w, h)
                self.control.Visible = bool(show and w >= 1 and h >= 1)
                self._applied_bounds = bounds
                _native_poc_log("BOUNDS_APPLIED", bounds=str(self.control.Bounds), visible=self.control.Visible, ready=self._ready)
                if self.control.Visible:
                    self.control.BringToFront()
                    _native_poc_log("SURFACE_SHOW_CALLED", frontmost=True)
            begin = getattr(self.form, "BeginInvoke", None)
            if callable(begin):
                begin(__import__('System').Action(apply))
            else:
                self.form.Invoke(__import__('System').Action(apply))
            return True
        except Exception:
            _native_poc_log("NATIVE_AD_ERROR", error="INVALID_BOUNDS_OR_UI_THREAD")
            return False

    def set_placement(self, route, url, width, height):
        if self._disposed:
            return False
        self._placement = (str(route), str(url), int(width), int(height))
        if self._ready:
            try:
                self.control.CoreWebView2.Navigate(self._placement[1])
            except Exception:
                _native_poc_log("NATIVE_AD_ERROR", error="NAVIGATION_FAILED")
                return False
        return True

    def hide(self):
        return self.set_bounds(*(self._pending_bounds or (0, 0, 728, 90, False))[:4], visible=False)

    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
        try:
            self.form.Invoke(__import__('System').Action(lambda: self.control.Dispose()))
        except Exception:
            _native_poc_log("NATIVE_AD_ERROR", error="DISPOSE_FAILED")
        _native_poc_log("SURFACE_DISPOSE_COMPLETE")


_NATIVE_AD_SURFACE = None


def _native_poc_log(event: str, **fields) -> None:
    if os.getenv("YOMU_ADS_NATIVE_POC", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    try:
        is_d4g = "geometry" in NATIVE_AD_DIAGNOSTIC_BUILD_ID
        filename = "native-ad-d4g.log" if is_d4g else ("native-ad-d4.log" if NATIVE_AD_DIAGNOSTIC_BUILD_ID.startswith("native-ad-d4") else ("native-home-slot-probe.log" if "home-slot-probe" in NATIVE_AD_DIAGNOSTIC_BUILD_ID else "native-ad-poc.log"))
        path = Path(os.getenv("LOCALAPPDATA", "")) / "YomuSekai" / "logs" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        safe = {"event": str(event)}
        safe.update({str(k): str(v)[:240] for k, v in fields.items() if k not in {"cookie", "token", "url_query"}})
        with _NATIVE_POC_LOG_LOCK:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **safe}, ensure_ascii=True) + "\n")
    except OSError:
        return


def _native_d3_log(event: str, **fields) -> None:
    """Early, dual-path bootstrap log for the side-by-side D3 artifact."""
    if os.getenv("YOMU_ADS_NATIVE_POC", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    safe = {"event": str(event), "build_id": NATIVE_AD_DIAGNOSTIC_BUILD_ID}
    safe.update({str(k): str(v)[:160] for k, v in fields.items() if k not in {"cookie", "token", "url_query"}})
    line = json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **safe}, ensure_ascii=True) + "\n"
    targets = [
        Path(os.getenv("LOCALAPPDATA", "")) / "YomuSekai" / "logs" / ("native-ad-d4g.log" if "geometry" in NATIVE_AD_DIAGNOSTIC_BUILD_ID else ("native-ad-d4.log" if NATIVE_AD_DIAGNOSTIC_BUILD_ID.startswith("native-ad-d4") else ("native-home-slot-probe.log" if "home-slot-probe" in NATIVE_AD_DIAGNOSTIC_BUILD_ID else "native-ad-d3.log"))),
        Path(os.getenv("TEMP", "")) / ("yomu-native-ad-d4g.log" if "geometry" in NATIVE_AD_DIAGNOSTIC_BUILD_ID else ("yomu-native-ad-d4.log" if NATIVE_AD_DIAGNOSTIC_BUILD_ID.startswith("native-ad-d4") else ("yomu-native-home-slot-probe.log" if "home-slot-probe" in NATIVE_AD_DIAGNOSTIC_BUILD_ID else "yomu-native-ad-d3.log"))),
    ]
    for path in targets:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(line)
        except OSError:
            continue


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


def run(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *, lifecycle_selftest: bool = False, auth_diagnostics: bool = False, ads_diagnostic_mode: str | None = None) -> int:
    try:
        import webview
    except ImportError as exc:  # pragma: no cover - environment diagnostic
        raise SystemExit("WebView2 indisponível: instale pywebview no .venv-beta.") from exc

    server = _start_server(host, port, auth_diagnostics=auth_diagnostics)
    global _REQUEST_WINDOW_CLOSE
    try:
        _wait_ready(host, port, server)
        _set_windows_app_user_model_id()
        direct_ads = ads_diagnostic_mode == "direct"
        # The native ad surface is a product capability on Windows.  The legacy
        # POC flag remains only as an optional diagnostic marker; it no longer
        # gates creation of the surface.
        native_ads = os.name == "nt" and not direct_ads
        diagnostic_api = AdsDiagnosticsApi() if ads_diagnostic_mode else None
        target_url = "https://henriquepvar.github.io/ad/banner-728x90.html" if direct_ads else _desktop_url(host, port)
        window_title = "Yomu Sekai - Ads Diagnostic" if ads_diagnostic_mode else "Yomu Sekai"
        window = webview.create_window(
            window_title, target_url,
            width=900 if direct_ads else 1440, height=240 if direct_ads else 900,
            min_size=(760, 180) if direct_ads else (980, 680), confirm_close=True,
            js_api=diagnostic_api if diagnostic_api else DesktopApi(),
        )
        if native_ads and not direct_ads:
            def create_native_surface() -> None:
                global _NATIVE_AD_SURFACE
                _native_poc_log("AD_HOST_CREATE_START")
                try:
                    form = getattr(window, "native", None)
                    if form is None:
                        raise RuntimeError("MAIN_FORM_NOT_FOUND")
                    from System import Action
                    def create_on_ui_thread():
                        global _NATIVE_AD_SURFACE
                        try:
                            _NATIVE_AD_SURFACE = NativeAdSurface(window)
                        except Exception as exc:
                            _NATIVE_AD_SURFACE = None
                            _native_poc_log("AD_HOST_CREATE_FAILURE", error=type(exc).__name__, detail=str(exc)[:160])
                    form.BeginInvoke(Action(create_on_ui_thread))
                except Exception as exc:
                    _NATIVE_AD_SURFACE = None
                    detail = str(exc).replace("\\r", " ").replace("\\n", " ")[:160]
                    _native_poc_log("AD_HOST_CREATE_FAILURE", error=type(exc).__name__, detail=detail)
            # pywebview invokes the start callback on its GUI thread, after the
            # WinForms parent exists. The normal app remains untouched by this
            # opt-in diagnostic path.
            native_surface_callback = create_native_surface
        else:
            native_surface_callback = None
        lifecycle = {"closing": False, "closed": False}

        def on_closing() -> None:
            lifecycle["closing"] = True

        def on_closed() -> None:
            lifecycle["closed"] = True
            surface = globals().get("_NATIVE_AD_SURFACE")
            if surface is not None:
                surface.dispose()

        _REQUEST_WINDOW_CLOSE = lambda: window.destroy()

        window.events.closing += on_closing
        window.events.closed += on_closed
        if native_surface_callback:
            # The start callback can run before pywebview assigns window.native.
            # BrowserForm.shown is the first deterministic point at which the
            # parent HWND/control tree exists.
            window.events.shown += native_surface_callback
            def trigger_native_geometry() -> None:
                # Some pywebview builds dispatch pywebviewready before the
                # document's late-loaded asset registers its listener. Re-run
                # the real slot initializer once the page is fully loaded.
                try:
                    _native_poc_log("D4_FORCED_INIT_AFTER_LOADED")
                    probe = window.evaluate_js("({ready:document.readyState, legacyAnchor:!!document.getElementById('native-ad-d4-anchor'), homeSlot:!!document.querySelector('[data-native-ad-slot=\\\"home\\\"]'), activeRoute:document.querySelector('.panel-view.active')?.id || '', nativeFlag:window.__yomuAdsNativePoc, providerFlag:window.__yomuPassiveAdsProviderEnabled, slot:typeof window.PassiveAdSlot, py:!!window.pywebview, api:!!window.pywebview?.api, bounds:typeof window.pywebview?.api?.native_ad_set_bounds})")
                    _native_poc_log("D4_BRIDGE_PROBE", probe=json.dumps(probe, ensure_ascii=True)[:240])
                    window.evaluate_js("window.PassiveAdSlot && window.PassiveAdSlot.init && window.PassiveAdSlot.init()")
                except Exception as exc:
                    _native_poc_log("NATIVE_AD_ERROR", error="FORCED_INIT_FAILED", detail=type(exc).__name__)
            window.events.loaded += trigger_native_geometry
        if ads_diagnostic_mode:
            def collect_ads_diagnostics() -> None:
                script = r"""
                (() => {
                  const resources = performance.getEntriesByType('resource')
                    .map(e => { try { return new URL(e.name).hostname; } catch (_) { return ''; } })
                    .filter(Boolean).filter((v, i, a) => a.indexOf(v) === i).slice(0, 20);
                  const frames = [...document.querySelectorAll('iframe')];
                  const iframeMetrics = frames.map(f => {
                    const r = f.getBoundingClientRect();
                    let bodyLength = null, childCount = null;
                    try { bodyLength = f.contentDocument?.body?.innerHTML?.length ?? null; childCount = f.contentDocument?.body?.children?.length ?? null; } catch (_) {}
                    return { width: Math.round(r.width), height: Math.round(r.height), body_length: bodyLength, child_count: childCount };
                  });
                  const slots = [...document.querySelectorAll('[data-passive-ad-slot]')].map(x => ({
                    name: x.dataset.passiveAdSlot || '', state: x.dataset.state || '', hidden: !!x.hidden,
                    iframe: !!x.querySelector('iframe'), src: x.querySelector('iframe')?.src || ''
                  }));
                  return window.pywebview.api.record({
                    mode: %MODE%, url: location.href, user_agent: navigator.userAgent,
                    page_loaded: document.readyState === 'complete', iframe_created: frames.length > 0,
                    iframe_src: frames.map(f => f.src).filter(Boolean).slice(0, 10), iframe_metrics: iframeMetrics,
                    iframe_load: frames.map(f => f.contentWindow ? 'created' : 'unknown'),
                    slot_states: slots, resource_hosts: resources,
                    javascript_enabled: true,
                    local_storage: (() => { try { localStorage.setItem('__yk_probe','1'); localStorage.removeItem('__yk_probe'); return true; } catch (_) { return false; } })(),
                    session_storage: (() => { try { sessionStorage.setItem('__yk_probe','1'); sessionStorage.removeItem('__yk_probe'); return true; } catch (_) { return false; } })()
                  });
                })()
                """.replace("%MODE%", json.dumps(ads_diagnostic_mode))
                try:
                    window.evaluate_js(script)
                except Exception:
                    return
            def on_loaded() -> None:
                threading.Timer(3.0, collect_ads_diagnostics).start()
            window.events.loaded += on_loaded
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
    if os.getenv("YOMU_ADS_NATIVE_POC", "").strip().lower() in {"1", "true", "yes", "on"}:
        prefix = "D4" if NATIVE_AD_DIAGNOSTIC_BUILD_ID.startswith("native-ad-d4") else "D3"
        _native_d3_log(f"{prefix}_PROCESS_START", native_flag=os.getenv("YOMU_ADS_NATIVE_POC", "absent"), provider_flag=os.getenv("YOMU_PASSIVE_ADS_PROVIDER_ENABLED", "absent"), entrypoint=str(Path(sys.argv[0]).name))
        _native_d3_log(f"{prefix}_NATIVE_FLAG", value=os.getenv("YOMU_ADS_NATIVE_POC", "absent"))
        _native_d3_log(f"{prefix}_PROVIDER_FLAG", value=os.getenv("YOMU_PASSIVE_ADS_PROVIDER_ENABLED", "absent"))
        _native_d3_log(f"{prefix}_BOOTSTRAP_REACHED")
        if prefix == "D4":
            _native_d3_log("D4_AUTH_INDEPENDENT_DIAGNOSTIC_START")
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
    parser.add_argument("--ads-diagnostic-direct", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ads-diagnostic-yomu", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--export-diagnostics", metavar="DESTINO", help="exporta um ZIP sanitizado de diagnósticos")
    parsed = parser.parse_args(args)
    if parsed.host not in {"127.0.0.1", "localhost"}:
        parser.error("o desktop shell aceita somente loopback")
    if parsed.auth_diagnostics:
        os.environ["TRADUTOR_AUTH_DIAGNOSTICS"] = "1"
    ads_mode = None
    if parsed.ads_diagnostic_direct and parsed.ads_diagnostic_yomu:
        parser.error("escolha apenas um modo de diagnóstico de anúncios")
    if parsed.ads_diagnostic_direct:
        ads_mode = "direct"
    elif parsed.ads_diagnostic_yomu:
        ads_mode = "yomu"
    elif os.getenv("YOMU_ADS_DIAGNOSTICS", "").strip().lower() in {"1", "true", "yes", "on"}:
        ads_mode = "yomu"
    if parsed.export_diagnostics:
        print(export_diagnostics(parsed.export_diagnostics))
        return 0
    return run(parsed.host, parsed.port, lifecycle_selftest=parsed.lifecycle_selftest, auth_diagnostics=parsed.auth_diagnostics, ads_diagnostic_mode=ads_mode)


if __name__ == "__main__":
    raise SystemExit(main())
