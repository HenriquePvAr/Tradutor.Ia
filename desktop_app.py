"""Yomu Sekai desktop shell backed by the local NiceGUI server.

This is a development foundation for the future frozen/installer entrypoint.  It
keeps the existing HTTP app intact and hosts it in Microsoft Edge WebView2 via
pywebview.  Session credentials cross the bridge only for immediate in-memory
sealing into a job-scoped DPAPI envelope; they are never logged or persisted
plaintext.
"""

from __future__ import annotations

import argparse
from collections import deque
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
_NATIVE_AD_WINDOW = None
_NATIVE_AD_SURFACE_CREATE_SCHEDULED = False
_NATIVE_AD_PENDING_PLACEMENT = None
_NATIVE_AD_PENDING_BOUNDS = None


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _content_diagnostic_enabled() -> bool:
    return _env_flag("YOMU_ADS_CONTENT_DIAGNOSTIC")


def _runtime_log(event: str, **fields) -> None:
    """Small bounded lifecycle log available in normal beta builds."""
    try:
        path = Path(os.getenv("LOCALAPPDATA", "")) / "YomuSekai" / "logs" / "runtime.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        safe = {str(k): str(v)[:160] for k, v in fields.items() if k not in {"token", "cookie", "password", "url_query"}}
        line = json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), "event": str(event), **safe}, ensure_ascii=True) + "\n"
        with _NATIVE_POC_LOG_LOCK:
            if path.exists() and path.stat().st_size > 256_000:
                path.replace(path.with_suffix(".previous.log"))
            with path.open("a", encoding="utf-8") as stream:
                stream.write(line)
    except OSError:
        return


def _request_native_ad_surface() -> None:
    """Create the child WebView lazily, never on the startup critical path."""
    global _NATIVE_AD_SURFACE_CREATE_SCHEDULED, _NATIVE_AD_SURFACE
    if _NATIVE_AD_SURFACE is not None or _NATIVE_AD_SURFACE_CREATE_SCHEDULED:
        return
    window = globals().get("_NATIVE_AD_WINDOW")
    form = getattr(window, "native", None) if window is not None else None
    begin = getattr(form, "BeginInvoke", None)
    if not callable(begin):
        _runtime_log("NATIVE_SURFACE_CREATE_FAILURE", error="MAIN_FORM_NOT_READY")
        return
    _NATIVE_AD_SURFACE_CREATE_SCHEDULED = True
    _runtime_log("NATIVE_SURFACE_CREATE_START")
    try:
        from System import Action
        def create_on_ui_thread():
            global _NATIVE_AD_SURFACE_CREATE_SCHEDULED, _NATIVE_AD_SURFACE
            try:
                _NATIVE_AD_SURFACE = NativeAdSurface(window)
                _runtime_log("NATIVE_SURFACE_CREATE_SUCCESS")
                if _NATIVE_AD_PENDING_PLACEMENT is not None:
                    _NATIVE_AD_SURFACE.set_placement(*_NATIVE_AD_PENDING_PLACEMENT)
                if _NATIVE_AD_PENDING_BOUNDS is not None:
                    _NATIVE_AD_SURFACE.set_bounds(*_NATIVE_AD_PENDING_BOUNDS)
            except Exception as exc:
                _NATIVE_AD_SURFACE = None
                _runtime_log("NATIVE_SURFACE_CREATE_FAILURE", error=type(exc).__name__)
            finally:
                _NATIVE_AD_SURFACE_CREATE_SCHEDULED = False
        begin(Action(create_on_ui_thread))
    except Exception as exc:
        _NATIVE_AD_SURFACE_CREATE_SCHEDULED = False
        _runtime_log("NATIVE_SURFACE_CREATE_FAILURE", error=type(exc).__name__)


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
        self._native_bounds_events = deque()
        self._native_storm_tripped = False
        self._native_bounds_requests = 0
        self._native_bounds_deduped = 0

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
        state = (round(float(x)), round(float(y)), max(0, round(float(width))), max(0, round(float(height))), bool(visible))
        now = time.monotonic()
        self._native_bounds_requests += 1
        self._native_bounds_events.append(now)
        while self._native_bounds_events and now - self._native_bounds_events[0] > 5.0:
            self._native_bounds_events.popleft()
        if len(self._native_bounds_events) > 500 and not self._native_storm_tripped:
            self._native_storm_tripped = True
            _runtime_log("AD_STORM_CIRCUIT_BREAKER", bounds_requests=len(self._native_bounds_events))
            if surface is not None:
                surface.hide()
        if self._native_storm_tripped:
            return {"available": True, "ready": False, "circuit_breaker": True}
        if surface is None:
            global _NATIVE_AD_PENDING_BOUNDS
            _NATIVE_AD_PENDING_BOUNDS = state
            if visible:
                _request_native_ad_surface()
            return {"available": True, "ready": False}
        with self._native_bounds_lock:
            if state == self._last_native_bounds_state:
                self._native_bounds_deduped += 1
                return {"available": True, "ready": bool(getattr(surface, "_ready", False))}
            self._last_native_bounds_state = state
        _native_poc_log("BOUNDS_STATE_CHANGED", x=state[0], y=state[1], width=state[2], height=state[3], visible=state[4])
        return {"available": bool(surface.set_bounds(*state)), "ready": bool(getattr(surface, "_ready", False))}

    def native_ad_set_placement(self, route: str, url: str, width: int, height: int) -> dict[str, bool]:
        surface = globals().get("_NATIVE_AD_SURFACE")
        safe_route = str(route)[:40]
        safe_url = str(url)
        allowed_ad_hosts = (
            "https://henriquepvar.github.io/ad/",
            "https://game-deals-alpha.vercel.app/ad/",
        )
        if not safe_url.startswith(allowed_ad_hosts):
            return {"available": False}
        state = (safe_route, safe_url, max(1, int(width)), max(1, int(height)))
        if surface is None:
            global _NATIVE_AD_PENDING_PLACEMENT
            _NATIVE_AD_PENDING_PLACEMENT = state
            _request_native_ad_surface()
            return {"available": True, "ready": False}
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

    def native_ad_session_state(self, route: str = "") -> dict[str, object]:
        """Return sanitized ad-session readiness for the UI claim gate."""
        surface = globals().get("_NATIVE_AD_SURFACE")
        if surface is None:
            return {"available": False, "core_ready": False, "surface_visible": False,
                    "navigation_success": False, "navigation_in_progress": False, "route": ""}
        committed = getattr(surface, "_committed_placement", None)
        committed_route = str(committed[0]) if committed else ""
        requested_route = str(route or "")
        return {
            "available": True,
            "core_ready": bool(getattr(surface, "_ready", False)),
            "surface_visible": bool(getattr(surface, "_surface_visible", False) and getattr(surface.control, "Visible", False)),
            "navigation_success": bool(committed and (not requested_route or committed_route == requested_route)),
            "navigation_in_progress": bool(getattr(surface, "_navigation_in_progress", False)),
            "route": committed_route,
        }


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
        self._bounds_lock = threading.Lock()
        self._begininvoke_pending = False
        self._bounds_measurements = 0
        self._bounds_applies = 0
        self._bounds_coalesced = 0
        self._bounds_window_started = time.monotonic()
        self._surface_visible = False
        self._navigation_generation = 0
        self._current_navigation_url = None
        self._requested_placement = None
        self._committed_placement = None
        self._active_navigation = None
        self._navigation_in_progress = False
        self._navigation_invoke_pending = False
        self._last_requested_bounds = None
        self._force_visibility_reapply = False
        self._content_diag_enabled = _content_diagnostic_enabled()
        self._content_diag_timers = []
        self._snapshot_timer = None
        self._snapshot_generation = None
        self._snapshot_stages = []
        self._snapshot_stage_index = 0
        self._script_watchers = []
        self._placement = ("home", self.URL, 728, 90)
        self._requested_placement = self._placement
        _native_poc_log("COREWEBVIEW2_INIT_START")
        _runtime_log("CORE_INIT_START", surface_generation=id(self))
        self.control.EnsureCoreWebView2Async(None)

    def _content_diag(self, event: str, **fields):
        if self._content_diag_enabled:
            _native_poc_log(event, **fields)

    @staticmethod
    def _safe_uri(uri):
        try:
            from urllib.parse import urlsplit
            p = urlsplit(str(uri))
            return {"scheme": p.scheme, "host": p.hostname or "", "path": p.path[:180]}
        except Exception:
            return {"scheme": "", "host": "", "path": ""}

    def _cancel_snapshot_sequence(self):
        timer = getattr(self, "_snapshot_timer", None)
        if timer is not None:
            try:
                timer.Stop()
                timer.Dispose()
            except Exception:
                pass
        self._snapshot_timer = None
        self._snapshot_generation = None

    def _watch_script_task(self, task, stage, generation):
        try:
            from System.Windows.Forms import Timer
            started = time.monotonic()
            watcher = Timer()
            watcher.Interval = 50
            def finish(reason=None):
                try:
                    watcher.Stop()
                    watcher.Dispose()
                except Exception:
                    pass
                if watcher in self._script_watchers:
                    self._script_watchers.remove(watcher)
                if reason:
                    self._content_diag("AD_DOM_EXECUTE_SCRIPT_ERROR", stage=stage, reason=reason, generation=generation)
            def tick(sender, args):
                if self._disposed or generation != self._navigation_generation:
                    finish("NAVIGATION_GENERATION_STALE" if generation != self._navigation_generation else "WEBVIEW_DISPOSED")
                    return
                if time.monotonic() - started > 5.0:
                    finish("EXECUTE_SCRIPT_TIMEOUT")
                    return
                try:
                    if not bool(task.IsCompleted):
                        return
                    if bool(task.IsCanceled):
                        finish("EXECUTE_SCRIPT_CANCELED")
                        return
                    if bool(task.IsFaulted):
                        finish("EXECUTE_SCRIPT_TASK_FAULTED")
                        return
                    raw = task.GetAwaiter().GetResult()
                    value = json.loads(str(raw))
                    if isinstance(value, str):
                        value = json.loads(value)
                    if not isinstance(value, dict):
                        finish("JSON_PARSE_ERROR")
                        return
                    self._content_diag("AD_DOM_SNAPSHOT", stage=stage, generation=generation, payload=json.dumps(value, ensure_ascii=True)[:2200])
                    for iframe in value.get("iframes", []) or []:
                        self._content_diag("AD_IFRAME_STATE", stage=stage, generation=generation,
                                           index=iframe.get("index", 0), host=iframe.get("src_host", iframe.get("host", "")),
                                           path=iframe.get("src_path", ""), x=iframe.get("x", 0), y=iframe.get("y", 0),
                                           width=iframe.get("width", iframe.get("w", 0)), height=iframe.get("height", iframe.get("h", 0)),
                                           display=iframe.get("display", ""), visibility=iframe.get("visibility", ""),
                                           opacity=iframe.get("opacity", ""), connected=iframe.get("isConnected", False))
                    for point in value.get("top_elements", []) or []:
                        self._content_diag("AD_TOP_ELEMENT", stage=stage, generation=generation,
                                           point=point.get("point", ""), tag=point.get("tag", ""),
                                           id=point.get("id", ""), class_name=point.get("class_name", ""),
                                           rect=json.dumps(point.get("rect", {}), ensure_ascii=True),
                                           display=point.get("display", ""), visibility=point.get("visibility", ""),
                                           opacity=point.get("opacity", ""), z_index=point.get("z_index", ""))
                    self._content_diag("AD_BODY_CHILDREN", stage=stage, generation=generation,
                                       children=json.dumps(value.get("body_children", []), ensure_ascii=True))
                    if stage in {"1s", "3s", "5s"}:
                        self._capture_preview(stage, generation)
                    finish()
                except Exception as exc:
                    finish("PYTHONNET_RESULT_BINDING_ERROR:" + type(exc).__name__)
            watcher.Tick += tick
            self._script_watchers.append(watcher)
            watcher.Start()
        except Exception as exc:
            self._content_diag("AD_DOM_EXECUTE_SCRIPT_ERROR", stage=stage, reason=type(exc).__name__)

    def _capture_preview(self, stage, generation):
        """Capture only the ad child WebView2 without blocking its UI thread."""
        if self._disposed or generation != self._navigation_generation or not self._ready:
            return
        try:
            from System.IO import MemoryStream
            from Microsoft.Web.WebView2.Core import CoreWebView2CapturePreviewImageFormat
            from System.Windows.Forms import Timer
            stream = MemoryStream()
            task = self.control.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, stream)
            watcher = Timer()
            watcher.Interval = 50
            started = time.monotonic()
            audit_dir = os.getenv("YOMU_ADS_AUDIT_DIR", "")
            out_dir = Path(audit_dir) if audit_dir else Path(os.getenv("LOCALAPPDATA", "")) / "YomuSekai" / "logs" / "ad-content-final" / datetime.now().strftime("%Y%m%d-%H%M%S")
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"ad-webview-{stage}.png"

            def finish(reason=None):
                try:
                    watcher.Stop(); watcher.Dispose(); stream.Dispose()
                except Exception:
                    pass
                if watcher in self._script_watchers:
                    self._script_watchers.remove(watcher)
                if reason:
                    self._content_diag("AD_CAPTURE_PREVIEW_ERROR", stage=stage, reason=reason)

            def tick(sender, args):
                if self._disposed or generation != self._navigation_generation:
                    finish("NAVIGATION_GENERATION_STALE" if generation != self._navigation_generation else "WEBVIEW_DISPOSED")
                    return
                if time.monotonic() - started > 5.0:
                    finish("CAPTURE_PREVIEW_TIMEOUT")
                    return
                try:
                    if not bool(task.IsCompleted):
                        return
                    if bool(task.IsCanceled):
                        finish("CAPTURE_PREVIEW_CANCELED"); return
                    if bool(task.IsFaulted):
                        finish("CAPTURE_PREVIEW_TASK_FAULTED"); return
                    data = bytes(stream.ToArray())
                    if not data:
                        finish("CAPTURE_PREVIEW_EMPTY"); return
                    out_path.write_bytes(data)
                    classification, details = self._classify_preview(data)
                    self._content_diag("AD_CAPTURE_PREVIEW", stage=stage, file=str(out_path), classification=classification, **details)
                    finish()
                except Exception as exc:
                    finish("CAPTURE_PREVIEW_RESULT_ERROR:" + type(exc).__name__)

            watcher.Tick += tick
            self._script_watchers.append(watcher)
            watcher.Start()
        except Exception as exc:
            self._content_diag("AD_CAPTURE_PREVIEW_ERROR", stage=stage, reason="CAPTURE_PREVIEW_BINDING_ERROR:" + type(exc).__name__)

    @staticmethod
    def _classify_preview(data):
        try:
            from io import BytesIO
            from PIL import Image
            image = Image.open(BytesIO(data)).convert("RGB")
            pixels = list(image.getdata())
            near_white = sum(1 for p in pixels if min(p) >= 248) / max(1, len(pixels))
            mean = sum(sum(p) / 3.0 for p in pixels) / max(1, len(pixels))
            variance = sum(((sum(p) / 3.0) - mean) ** 2 for p in pixels) / max(1, len(pixels))
            classification = "WHITE" if near_white >= 0.98 and variance < 20.0 else "CONTENT"
            return classification, {"width": image.width, "height": image.height,
                                    "near_white_pct": round(near_white, 4), "variance": round(variance, 2),
                                    "criterion": "near_white>=0.98_and_variance<20"}
        except Exception as exc:
            return "FAIL", {"criterion": "IMAGE_ANALYSIS_UNAVAILABLE:" + type(exc).__name__}

    def _execute_snapshot(self, stage, generation):
        if not self._content_diag_enabled or self._disposed or generation != self._navigation_generation or not self._ready:
            return
        self._content_diag("AD_SNAPSHOT_STAGE", stage=stage, generation=generation)
        script = r"""(()=>{const rect=e=>{const r=e?.getBoundingClientRect?.()||{};return {x:Math.round(r.x||0),y:Math.round(r.y||0),left:Math.round(r.left||0),top:Math.round(r.top||0),right:Math.round(r.right||0),bottom:Math.round(r.bottom||0),width:Math.round(r.width||0),height:Math.round(r.height||0)}};const style=e=>{const s=e?getComputedStyle(e):{};return {display:s.display||'',visibility:s.visibility||'',opacity:s.opacity||'',position:s.position||'',z_index:s.zIndex||''}};const url=u=>{try{const p=new URL(u||'');return {src_scheme:p.protocol.replace(':',''),src_host:p.hostname||'',src_path:p.pathname||''}}catch(e){return {src_scheme:'',src_host:'',src_path:''}}};const fs=[...document.querySelectorAll('iframe')].slice(0,8).map((f,i)=>Object.assign({index:i,attribute_width:f.getAttribute('width')||'',attribute_height:f.getAttribute('height')||'',clientWidth:f.clientWidth||0,clientHeight:f.clientHeight||0,offsetWidth:f.offsetWidth||0,offsetHeight:f.offsetHeight||0,isConnected:!!f.isConnected,rect:rect(f)},url(f.src),style(f)));const points=[['10,10',10,10],['center',innerWidth/2,innerHeight/2],['bottom-right',Math.max(0,innerWidth-10),Math.max(0,innerHeight-10)]];const tops=points.map(([point,x,y])=>{const e=document.elementFromPoint(x,y);return Object.assign({point,tag:e?.tagName||'',id:(e?.id||'').slice(0,80),class_name:(typeof e?.className==='string'?e.className:'').slice(0,80),rect:rect(e)},style(e))});const children=[...document.body?.children||[]].slice(0,20).map(e=>Object.assign({tag:e.tagName||'',rect:rect(e)},style(e)));return JSON.stringify({href:location.href.split('?')[0],ready:document.readyState,visibility:document.visibilityState,focus:document.hasFocus(),inner:[innerWidth,innerHeight],dpr:devicePixelRatio,scroll:[document.documentElement.scrollWidth,document.documentElement.scrollHeight],scripts:document.scripts.length,invoke:[...document.scripts].some(s=>(s.src||'').includes('highrevenueformat.com')),iframes:fs,top_elements:tops,body_children:children,body_rect:rect(document.body),body:[document.body?.children.length||0],ua:navigator.userAgent,cookie:navigator.cookieEnabled})})()"""
        try:
            task = self.control.CoreWebView2.ExecuteScriptAsync(script)
            self._watch_script_task(task, stage, generation)
        except Exception as exc:
            self._content_diag("AD_DOM_EXECUTE_SCRIPT_ERROR", stage=stage, reason="PYTHONNET_BINDING_ERROR:" + type(exc).__name__)

    def _start_snapshot_sequence(self):
        if not self._content_diag_enabled or self._disposed:
            return
        self._cancel_snapshot_sequence()
        generation = self._navigation_generation
        self._snapshot_generation = generation
        self._snapshot_stages = [("250ms", 250), ("1s", 1000), ("3s", 3000), ("5s", 5000)]
        self._snapshot_stage_index = 0
        self._content_diag("AD_SNAPSHOT_SEQUENCE_CREATED", generation=generation)
        self._execute_snapshot("DOMContentLoaded", generation)
        try:
            from System.Windows.Forms import Timer
            timer = Timer()
            timer.Interval = self._snapshot_stages[0][1]
            def tick(sender, args):
                if self._disposed or generation != self._navigation_generation:
                    self._cancel_snapshot_sequence()
                    return
                stage, _delay = self._snapshot_stages[self._snapshot_stage_index]
                self._execute_snapshot(stage, generation)
                self._snapshot_stage_index += 1
                if self._snapshot_stage_index >= len(self._snapshot_stages):
                    self._content_diag("AD_SNAPSHOT_SEQUENCE_COMPLETE", generation=generation)
                    self._cancel_snapshot_sequence()
                else:
                    timer.Interval = self._snapshot_stages[self._snapshot_stage_index][1] - self._snapshot_stages[self._snapshot_stage_index - 1][1]
            timer.Tick += tick
            self._snapshot_timer = timer
            timer.Start()
        except Exception as exc:
            self._content_diag("AD_SNAPSHOT_SCHEDULER_ERROR", reason=type(exc).__name__)

    def _attach_content_diagnostics(self):
        if not self._content_diag_enabled:
            return
        core = self.control.CoreWebView2
        try:
            def dom_loaded(sender, args):
                self._content_diag("AD_DOM_CONTENT_LOADED")
                self._start_snapshot_sequence()
            core.DOMContentLoaded += dom_loaded
        except Exception:
            self._content_diag("AD_CONTENT_DIAGNOSTIC_ERROR", error="DOMCONTENTLOADED_UNAVAILABLE")
        try:
            def response(sender, args):
                request = getattr(args, "Request", None)
                response_obj = getattr(args, "Response", None)
                fields = self._safe_uri(getattr(request, "Uri", ""))
                fields["status"] = getattr(response_obj, "StatusCode", "unknown")
                self._content_diag("AD_RESOURCE_RESPONSE", **fields)
            core.WebResourceResponseReceived += response
        except Exception:
            self._content_diag("AD_CONTENT_DIAGNOSTIC_ERROR", error="RESOURCE_EVENTS_UNAVAILABLE")
        try:
            core.ProcessFailed += lambda sender, args: self._content_diag("AD_PROCESS_FAILED", kind=str(getattr(args, "ProcessFailedKind", "unknown")))
        except Exception:
            self._content_diag("AD_CONTENT_DIAGNOSTIC_ERROR", error="PROCESS_FAILED_EVENT_UNAVAILABLE")

    def _on_initialized(self, sender, args):
        if getattr(args, "IsSuccess", False):
            self._ready = True
            _native_poc_log("COREWEBVIEW2_INIT_SUCCESS", available=True)
            _runtime_log("CORE_INIT_SUCCESS", surface_generation=id(self))
            self.control.CoreWebView2.NewWindowRequested += self._on_new_window
            self._attach_content_diagnostics()
            self._navigate_requested_on_ui_thread()
            if self._pending_bounds:
                self.set_bounds(*self._pending_bounds)
        else:
            _native_poc_log("COREWEBVIEW2_INIT_FAILURE", error="CORE_INIT_FAILED")
            _runtime_log("CORE_INIT_FAILURE", surface_generation=id(self))

    def _on_new_window(self, sender, args):
        # Keep the main Yomu surface intact; provider clicks may open externally.
        try:
            import webbrowser
            webbrowser.open(str(args.Uri))
            args.Handled = True
        except Exception:
            args.Handled = True

    def _on_navigation_completed(self, sender, args):
        success = bool(getattr(args, "IsSuccess", False))
        error = str(getattr(args, "WebErrorStatus", "unknown"))
        active = self._active_navigation
        source = ""
        try:
            source = str(self.control.CoreWebView2.Source or "")
        except Exception:
            pass
        _native_poc_log("NAVIGATION_COMPLETED", success=success, web_error=error)
        if active is None:
            _runtime_log("NAV_CALLBACK_STALE_IGNORED", reason="NO_ACTIVE_NAVIGATION")
            return
        generation, placement = active
        if generation != self._navigation_generation:
            self._navigation_in_progress = False
            self._active_navigation = None
            _runtime_log("NAV_CALLBACK_STALE_IGNORED", generation=generation, current_generation=self._navigation_generation)
            if self._ready and self._requested_placement:
                self._navigate_requested_on_ui_thread()
            return
        expected = placement[1]
        if success and source and source.split("?", 1)[0] != expected.split("?", 1)[0]:
            _runtime_log("NAV_CALLBACK_STALE_IGNORED", generation=generation, source=self._safe_uri(source), expected=self._safe_uri(expected))
            return
        self._navigation_in_progress = False
        self._active_navigation = None
        if not success:
            self._committed_placement = None
            self._current_navigation_url = None
            _runtime_log("PLACEMENT_NAV_FAILURE", route=placement[0], generation=generation, web_error_status=error)
            self._hide_surface_for_navigation_failure(placement[0])
            return
        self._committed_placement = placement
        self._current_navigation_url = placement[1]
        _runtime_log("NAV_COMMIT_CURRENT", route=placement[0], generation=generation, url=self._safe_uri(placement[1]))
        _runtime_log("PLACEMENT_NAV_SUCCESS", route=placement[0], generation=generation)
        if self._last_requested_bounds is not None:
            # A route request hides the surface while the old navigation is
            # being replaced, but the geometry itself remains unchanged.  A
            # visibility transition must not be coalesced as a geometry no-op.
            self._force_visibility_reapply = True
            x, y, width, height, _previous_visible = self._last_requested_bounds
            self.set_bounds(x, y, width, height, visible=True, force_visibility_reapply=True)

    def _hide_surface_for_navigation_failure(self, route):
        """Invalidate the old creative immediately; never leave a stale ad visible."""
        try:
            self._surface_visible = False
            if self._pending_bounds is not None:
                self._pending_bounds = (*self._pending_bounds[:4], False)
            def hide_ui():
                if self._disposed:
                    return
                self.control.Visible = False
            begin = getattr(self.form, "BeginInvoke", None)
            if callable(begin):
                begin(__import__('System').Action(hide_ui))
            else:
                self.form.Invoke(__import__('System').Action(hide_ui))
            _runtime_log("NAV_FAILURE_HIDE", route=route)
        except Exception as exc:
            _native_poc_log("NATIVE_AD_ERROR", error="NAV_FAILURE_HIDE_" + type(exc).__name__)

    def _navigate_requested_on_ui_thread(self):
        if self._disposed or not self._ready or not self._requested_placement:
            return
        placement = self._requested_placement
        generation = self._navigation_generation
        if self._active_navigation == (generation, placement):
            return
        if self._navigation_in_progress:
            _runtime_log("NAV_LATEST_WINS", route=placement[0], generation=generation,
                         active_generation=self._active_navigation[0] if self._active_navigation else "")
            return
        self._active_navigation = (generation, placement)
        self._navigation_in_progress = True
        _runtime_log("NAV_REQUEST", route=placement[0], generation=generation, url=self._safe_uri(placement[1]))
        _runtime_log("PLACEMENT_NAV_START", route=placement[0], generation=generation, placement_id=placement[1].rstrip("/").split("/")[-1])
        try:
            self.control.CoreWebView2.Navigate(placement[1])
            _native_poc_log("NAVIGATION_START", **self._safe_uri(placement[1]))
        except Exception as exc:
            self._navigation_in_progress = False
            self._active_navigation = None
            self._committed_placement = None
            self._current_navigation_url = None
            _native_poc_log("NATIVE_AD_ERROR", error="NAVIGATE_" + type(exc).__name__)
            _runtime_log("PLACEMENT_NAV_FAILURE", route=placement[0], generation=generation, error=type(exc).__name__)
            self._hide_surface_for_navigation_failure(placement[0])

    def set_bounds(self, x, y, width, height, visible=True, force_visibility_reapply=False):
        if self._disposed:
            return False
        try:
            bounds = (int(x), int(y), max(0, int(width)), max(0, int(height)), bool(visible))
            self._last_requested_bounds = bounds
            with self._bounds_lock:
                self._bounds_measurements += 1
                force_reapply = bool(force_visibility_reapply or self._force_visibility_reapply)
                if not force_reapply and (bounds == self._pending_bounds or bounds == self._applied_bounds):
                    self._bounds_coalesced += 1
                    return True
                self._pending_bounds = bounds
                if self._begininvoke_pending:
                    self._bounds_coalesced += 1
                    return True
                self._begininvoke_pending = True
            _native_poc_log("NATIVE_BOUNDS_CALCULATED", x=bounds[0], y=bounds[1], width=bounds[2], height=bounds[3], visible=bounds[4])
            def apply():
                with self._bounds_lock:
                    self._begininvoke_pending = False
                    current = self._pending_bounds
                if self._disposed or current is None:
                    return
                _, _, w, h, show = current
                force_apply = bool(self._force_visibility_reapply)
                if current == self._applied_bounds and not force_apply:
                    return
                self.control.Location = self._Point(current[0], current[1])
                self.control.Size = self._Size(w, h)
                committed = self._committed_placement
                placement_matches = bool(committed and self._placement and committed[1] == self._placement[1])
                desired_visible = bool(show and w >= 1 and h >= 1 and placement_matches and not self._navigation_in_progress)
                self.control.Visible = desired_visible
                self._applied_bounds = current
                self._bounds_applies += 1
                _native_poc_log("BOUNDS_APPLIED", bounds=str(self.control.Bounds), visible=self.control.Visible, ready=self._ready)
                if desired_visible and not self._surface_visible:
                    self.control.BringToFront()
                    _native_poc_log("SURFACE_SHOW_CALLED", frontmost=True)
                    _runtime_log("SURFACE_SHOW", route=self._placement[0])
                elif not desired_visible and self._surface_visible:
                    _runtime_log("SURFACE_HIDE", route=self._placement[0])
                self._surface_visible = desired_visible
                self._force_visibility_reapply = False
                now = time.monotonic()
                if now - self._bounds_window_started >= 10.0:
                    _runtime_log("ADS_BOUNDS_WINDOW", measurement_requests=self._bounds_measurements, native_applies=self._bounds_applies, native_coalesced=self._bounds_coalesced)
                    self._bounds_measurements = self._bounds_applies = self._bounds_coalesced = 0
                    self._bounds_window_started = now
            begin = getattr(self.form, "BeginInvoke", None)
            if callable(begin):
                begin(__import__('System').Action(apply))
            else:
                self.form.Invoke(__import__('System').Action(apply))
            return True
        except Exception:
            _native_poc_log("NATIVE_AD_ERROR", error="INVALID_BOUNDS_OR_UI_THREAD")
            with self._bounds_lock:
                self._begininvoke_pending = False
            return False

    def set_placement(self, route, url, width, height):
        if self._disposed:
            return False
        self._cancel_snapshot_sequence()
        placement = (str(route), str(url), int(width), int(height))
        if self._requested_placement == placement and self._committed_placement == placement:
            return True
        self._placement = placement
        self._requested_placement = placement
        self._navigation_generation += 1
        self._committed_placement = None
        self._current_navigation_url = None
        _runtime_log("PLACEMENT_RESOLVED", route=placement[0], generation=self._navigation_generation, url=self._safe_uri(placement[1]), width=placement[2], height=placement[3])
        _runtime_log("ROUTE_CHANGED", route=placement[0])
        self._hide_surface_for_navigation_failure(placement[0])
        if self._ready:
            try:
                begin = getattr(self.form, "BeginInvoke", None)
                if callable(begin):
                    begin(__import__('System').Action(self._navigate_requested_on_ui_thread))
                else:
                    self.form.Invoke(__import__('System').Action(self._navigate_requested_on_ui_thread))
            except Exception as exc:
                _native_poc_log("NATIVE_AD_ERROR", error="NAV_REQUEST_" + type(exc).__name__)
                _runtime_log("PLACEMENT_NAV_FAILURE", route=placement[0], generation=self._navigation_generation, error=type(exc).__name__)
                self._hide_surface_for_navigation_failure(placement[0])
                return False
        return True

    def hide(self):
        return self.set_bounds(*(self._pending_bounds or (0, 0, 728, 90, False))[:4], visible=False)

    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
        self._cancel_snapshot_sequence()
        for watcher in list(getattr(self, "_script_watchers", [])):
            try:
                watcher.Stop()
                watcher.Dispose()
            except Exception:
                pass
        self._script_watchers = []
        try:
            self.form.Invoke(__import__('System').Action(lambda: self.control.Dispose()))
        except Exception:
            _native_poc_log("NATIVE_AD_ERROR", error="DISPOSE_FAILED")
        _native_poc_log("SURFACE_DISPOSE_COMPLETE")


_NATIVE_AD_SURFACE = None


def _native_poc_log(event: str, **fields) -> None:
    if not (_env_flag("YOMU_ADS_NATIVE_POC") or _content_diagnostic_enabled()):
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
        content_diag_requested = _content_diagnostic_enabled()
        content_diag_active = content_diag_requested
        _runtime_log("ADS_CONTENT_DIAGNOSTIC_STATE", requested=content_diag_requested, active=content_diag_active, build_version=getattr(app_version, "BUILD_VERSION", app_version.PRODUCT_VERSION), capability=True)
        if content_diag_active:
            _native_poc_log("AD_CONTENT_DIAGNOSTIC_PROBE", mode="content-only")
        if content_diag_requested and not content_diag_active:
            _runtime_log("DIAGNOSTIC_ACTIVATION_FAILURE", reason="CONTENT_DIAGNOSTIC_DISABLED")
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
        global _NATIVE_AD_WINDOW
        _NATIVE_AD_WINDOW = window
        _runtime_log("MAIN_WINDOW_CREATED", native_ads=native_ads, diagnostic=bool(ads_diagnostic_mode or content_diag_active), content_diagnostic=content_diag_active)
        if native_ads and not direct_ads:
            def create_native_surface() -> None:
                # Surface creation is lazy: startup must not depend on ads.
                _native_poc_log("MAIN_WINDOW_READY")
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
        _runtime_log("APP_SHUTDOWN")
        _REQUEST_WINDOW_CLOSE = None
        shutdown_owned_runtime(server)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Yomu Sekai WebView2 desktop shell")
    args = list(sys.argv[1:] if argv is None else argv)
    _runtime_log("PROCESS_START", pid=os.getpid(), parent_pid=os.getppid(), entrypoint=str(Path(sys.argv[0]).name))
    if _env_flag("YOMU_ADS_NATIVE_POC"):
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
    elif _env_flag("YOMU_ADS_DIAGNOSTICS"):
        ads_mode = "yomu"
    if parsed.export_diagnostics:
        print(export_diagnostics(parsed.export_diagnostics))
        return 0
    return run(parsed.host, parsed.port, lifecycle_selftest=parsed.lifecycle_selftest, auth_diagnostics=parsed.auth_diagnostics, ads_diagnostic_mode=ads_mode)


if __name__ == "__main__":
    raise SystemExit(main())
