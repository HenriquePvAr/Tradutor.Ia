"""NiceGUI host for the custom Tradutor.Ia local frontend."""

from __future__ import annotations

import os
import json
from collections import deque
import re
import secrets
import socket
import ssl
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

try:
    import certifi
except ImportError:  # pragma: no cover - packaged builds include certifi
    certifi = None

from fastapi import Body, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from nicegui import app, ui

from community_auth import (
    AuthConfigurationError,
    AuthenticationRequired,
    AuthorizationDenied,
    AuthProvider,
    CsrfRejected,
    RequestPrincipal,
    SESSION_COOKIE_NAME,
    configured_bind_host,
    get_auth_provider,
    validate_bind_security,
)
from community_api import CommunityApi
from community_http import (
    CommunityNetworkBoundaryMiddleware,
    create_admin_community_router,
    create_community_router,
)
from json_utils import dumps_json
import audit_decisions
import linguistic_triage
import region_taxonomy
import natural_ptbr_refinement
import refinement_selection_decisions
import update_manifest
import update_transport
import installer_update
from update_bootstrap import DEFAULT_MANIFEST_URL
from app_version import BUILD_VERSION
from translator_nvidia import TranslatorNvidiaBatch
from chapter_quality_revision import REVIEW_SCHEMA_VERSION
from local_environment import load_local_environment_for_entrypoint
from process_options import hidden_console_options
from request_observability import (
    StructuredRequestAuditMiddleware,
    configured_request_audit_destination,
)
from ui_bridge import UiBridge, local_folder_ui_allowed


if not load_local_environment_for_entrypoint():
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parent
_UPDATE_INSTALLER_HANDOFFS: dict[str, Path] = {}
_UPDATE_INSTALLER_HANDOFFS_LOCK = threading.Lock()
_UPDATE_DOWNLOADS: dict[str, dict[str, Any]] = {}
_UPDATE_DOWNLOADS_LOCK = threading.Lock()


def _https_context() -> ssl.SSLContext:
    """Use the packaged CA bundle when available; never disable verification."""
    return ssl.create_default_context(cafile=certifi.where() if certifi else None)


def _supabase_csp_origin() -> str:
    """Return the exact configured Supabase HTTPS origin, or empty fail-closed."""
    raw = os.getenv("SUPABASE_URL", "").strip()
    if not raw:
        try:
            payload = json.loads((ROOT / "config" / "public-runtime.json").read_text(encoding="utf-8"))
            raw = str(payload.get("supabase_url", "")).strip() if isinstance(payload, dict) else ""
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            raw = ""
    parsed = urllib_parse.urlparse(raw)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return ""
    return f"https://{parsed.hostname}{(':' + str(parsed.port)) if parsed.port else ''}"


_SUPABASE_CSP_ORIGIN = _supabase_csp_origin()
_AUTH_DIAGNOSTICS_ENABLED = os.getenv("TRADUTOR_AUTH_DIAGNOSTICS", "").strip() == "1"
_AUTH_DIAGNOSTIC_EVENTS: deque[dict[str, Any]] = deque(maxlen=200)
_SERVER_STARTED_AT = __import__("datetime").datetime.now(
    __import__("datetime").timezone.utc).isoformat()
_RUNTIME_INSTANCE_ID = secrets.token_urlsafe(16)
_MAX_PROFILE_MEDIA_BYTES = 12 * 1024 * 1024
_PROFILE_MEDIA_MAX_BYTES = {"avatar": 5 * 1024 * 1024, "banner": 12 * 1024 * 1024}
STATIC_DIR = ROOT / "static"
SHELL_PATH = ROOT / "ui" / "ui_shell.html"
AUTH_UI_ASSET = ROOT / "static" / "auth_ui.js"
AUTH_PROVIDER_ASSET = ROOT / "static" / "auth_provider.js"
TRADUTOR_UI_ASSET = ROOT / "static" / "tradutor_ui.js"
TRADUTOR_CSS_ASSET = ROOT / "static" / "tradutor_ui.css"
LOADING_SURFACE_CSS_ASSET = ROOT / "static" / "loading_surface.css"
LOADING_VIEW_ASSET = ROOT / "static" / "loading_view.js"
PIPELINE_HARNESS_ASSET = ROOT / "static" / "pipeline_loading_harness.js"
PROCESSING_SURFACE_ASSET = ROOT / "static" / "processing_surface.js"
SOCIAL_COMMUNITY_ASSET = ROOT / "static" / "social_community.js"
SERVICE_HEALTH_ASSET = ROOT / "static" / "service_health.js"
CHAPTER_READER_ASSET = ROOT / "static" / "chapter_reader.js"
FAVICON_ASSET = ROOT / "static" / "assets" / "branding" / "yomu-sekai.ico"
I18N_ASSETS = [
    ROOT / "static" / "i18n" / "pt-BR.js",
    ROOT / "static" / "i18n" / "en-US.js",
    ROOT / "static" / "i18n" / "es-ES.js",
    ROOT / "static" / "i18n" / "fr-FR.js",
    ROOT / "static" / "i18n" / "ja-JP.js",
    ROOT / "static" / "i18n" / "ko-KR.js",
    ROOT / "static" / "i18n" / "index.js",
]


class SecurityHeadersMiddleware:
    """Apply conservative headers to every local UI response.

    The loopback desktop runtime is HTTP by design, so HSTS is intentionally omitted.
    Inline UI bootstrap is currently required by the NiceGUI shell and is constrained
    to this same-origin application; no external script sources are allowed.
    """

    _HEADERS = {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Content-Security-Policy": (
            "default-src 'self'; base-uri 'self'; object-src 'none'; "
            "frame-ancestors 'none'; form-action 'self'; "
            "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob: https:; font-src 'self' data:; "
            "connect-src 'self' ws: wss: " + (_SUPABASE_CSP_ORIGIN + "; " if _SUPABASE_CSP_ORIGIN else "") +
            "frame-src 'self'" + (" " + _SUPABASE_CSP_ORIGIN if _SUPABASE_CSP_ORIGIN else "")
        ),
    }

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def secured_send(message):
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                existing = {k.lower() for k, _ in headers}
                # Auth modules are mutable critical code in the desktop app. Do
                # not allow a stable module URL to remain fresh for an hour in
                # WebView2; the entry point already uses content-hash URLs, and
                # these dependency modules are defensively no-store as well.
                if scope.get("path", "") in {
                    "/static/auth_ui.js", "/static/auth_provider.js",
                    "/static/supabase_auth.js", "/static/auth_presentation.js",
                }:
                    headers = [(k, v) for k, v in headers if k.lower() != b"cache-control"]
                    headers.append((b"cache-control", b"no-store"))
                    existing = {k.lower() for k, _ in headers}
                for name, value in self._HEADERS.items():
                    key = name.lower().encode("latin-1")
                    if key not in existing:
                        headers.append((key, value.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, secured_send)


class MutationRateLimitMiddleware:
    """Bound accidental mutation bursts without treating loopback as identity."""

    _WINDOW = 60.0
    _MAX_ENTRIES = 2048
    _LIMITS = {
        "/api/auth/desktop/handoff/start": (10, 600.0),
        "/api/auth/desktop/handoff/": (120, 60.0),
        "/api/ui/profile": (60, 60.0),
        "/api/community": (120, 60.0),
        "/api/ui/": (240, 60.0),
    }

    def __init__(self, app):
        self.app = app
        self._events: dict[tuple[str, str], list[float]] = {}

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") not in {"POST", "PUT", "PATCH", "DELETE"}:
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        rule = next(((prefix, limit, window) for prefix, (limit, window) in self._LIMITS.items()
                     if path.startswith(prefix)), None)
        if rule is None:
            await self.app(scope, receive, send)
            return
        prefix, limit, window = rule
        client = scope.get("client") or ("unknown", 0)
        key = (str(client[0]), prefix)
        now = time.monotonic()
        events = [stamp for stamp in self._events.get(key, []) if now - stamp < window]
        if len(events) >= limit:
            response = JSONResponse({"detail": "rate_limited"}, status_code=429,
                                    headers={"Cache-Control": "no-store", "Retry-After": str(int(window))})
            await response(scope, receive, send)
            return
        events.append(now)
        self._events[key] = events
        if len(self._events) > self._MAX_ENTRIES:
            oldest = sorted(self._events.items(), key=lambda item: item[1][-1] if item[1] else 0)
            for stale_key, _ in oldest[: max(1, len(oldest) - self._MAX_ENTRIES)]:
                self._events.pop(stale_key, None)
        await self.app(scope, receive, send)


def _asset_url(path: Path) -> str:
    """Version local static assets by content so WebView cannot reuse stale code."""

    try:
        # Keep the mtime fallback for missing/unreadable files, but prefer a
        # content identity: a changed file always receives a different URL even
        # when filesystem timestamp granularity is coarse.
        import hashlib
        version = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        try:
            version = str(path.stat().st_mtime_ns)
        except OSError:
            version = "0"
    try:
        rel = path.relative_to(STATIC_DIR).as_posix()
    except ValueError:
        rel = path.name
    return f"/static/{rel}?v={version}"


def _i18n_bootstrap_html() -> str:
    """Install catalogs and resolver atomically before any UI consumer.

    Only repository-owned JavaScript is included. No user/session data enters this
    bootstrap. Keeping the dependency graph in one script prevents an incomplete
    catalog load from turning every label into a raw translation key.
    """
    source = "\n;\n".join(asset.read_text(encoding="utf-8") for asset in I18N_ASSETS)
    return '<script>' + source.replace('</script', '<\\/script') + '</script>'


def _runtime_asset_identity() -> dict[str, Any]:
    """Safe local provenance for proving which source tree serves the UI.

    The values are deliberately small hashes/mtimes.  They contain no credentials,
    session data, source URLs or local absolute paths.
    """

    import hashlib
    import subprocess as _sp

    def sha(path: Path) -> str:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return ""

    def mtime(path: Path) -> int:
        try:
            return int(path.stat().st_mtime_ns)
        except OSError:
            return 0

    head = ""
    try:
        head = _sp.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=5,
            **hidden_console_options(),
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - diagnostics must never block startup
        head = ""
    return {
        "git_head": head,
        "pid": os.getpid(),
        "shell_sha256": sha(SHELL_PATH),
        "shell_mtime_ns": mtime(SHELL_PATH),
        "tradutor_ui_js_sha256": sha(TRADUTOR_UI_ASSET),
        "tradutor_ui_js_mtime_ns": mtime(TRADUTOR_UI_ASSET),
        "tradutor_ui_css_sha256": sha(TRADUTOR_CSS_ASSET),
        "tradutor_ui_css_mtime_ns": mtime(TRADUTOR_CSS_ASSET),
        "auth_ui_js_sha256": sha(AUTH_UI_ASSET),
        "auth_provider_js_sha256": sha(AUTH_PROVIDER_ASSET),
        "auth_ui_build_id": f"auth_ui:{sha(AUTH_UI_ASSET)[:16]}",
    }


def _assert_startup_port_available(host: str, port: int) -> None:
    """Fail clearly before NiceGUI starts if the chosen port is already owned.

    This prevents a hidden startup attempt from appearing successful while the
    browser keeps talking to an older process on the same loopback port.
    """

    bind_host = str(host or "127.0.0.1")
    bind_port = int(port)
    probe_host = "127.0.0.1" if bind_host in {"0.0.0.0", "::"} else bind_host
    family = socket.AF_INET6 if ":" in probe_host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind((probe_host, bind_port))
        except OSError as exc:
            raise SystemExit(
                f"port_in_use: {bind_host}:{bind_port}; stop the existing "
                "Tradutor.Ia UI process or set TRADUTOR_UI_PORT explicitly."
            ) from exc
APP_PORT = int(os.getenv("TRADUTOR_UI_PORT", "8080"))
APP_HOST = configured_bind_host()
_DESKTOP_HANDOFF_TTL_SECONDS = 600
_DESKTOP_HANDOFFS: dict[str, dict[str, Any]] = {}
_DESKTOP_HANDOFF_LOCK = threading.Lock()
_DESKTOP_CLIENT_DIAGNOSTICS: dict[str, Any] = {}
BRIDGE = UiBridge()

# Canonical, per-user diagnostics root.  These records are deliberately small,
# UTF-8 JSONL entries and never contain credentials or request bodies.
DIAGNOSTICS_ROOT = BRIDGE.runtime_root / "diagnostics"
DIAGNOSTICS_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("TRADUTOR_DIAGNOSTICS_ROOT", str(DIAGNOSTICS_ROOT))


def _append_diagnostic_log(filename: str, event: str, **fields: Any) -> None:
    now = datetime.now(timezone.utc)
    safe = {"timestamp": now.isoformat().replace("+00:00", "Z"), "at": int(now.timestamp() * 1000), "event": event}
    for key, value in fields.items():
        if isinstance(value, (str, int, float, bool)) and key.lower() not in {"token", "password", "authorization", "secret", "body"}:
            safe[key] = value
    try:
        with (DIAGNOSTICS_ROOT / filename).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(safe, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        pass


_append_diagnostic_log("app_current.jsonl", "APP_BOOT", product="Yomu Sekai", port=APP_PORT)


def _source_trace_id(payload: dict[str, Any]) -> str:
    """Return a bounded client correlation id, or create an explicit fallback."""
    raw = str((payload or {}).get("trace_id") or "").strip()
    if raw and len(raw) <= 80 and all(ch.isalnum() or ch in "-_." for ch in raw):
        return raw
    fallback = uuid.uuid4().hex
    _append_diagnostic_log("app_current.jsonl", "TRACE_ID_FALLBACK_GENERATED", trace_id=fallback, route="source")
    return fallback

COMMUNITY = CommunityApi(
    BRIDGE.store,
    community_db_path=BRIDGE.runtime_root / "community.sqlite3",
    output_root=BRIDGE.output_root,
    storage_root=BRIDGE.runtime_root / "community_storage",
)
# Profile media deliberately reuses the Community provider factory.  The desktop only
# invokes authenticated backend routes; Drive credentials remain server-side.
BRIDGE.profile_media_provider_factory = COMMUNITY._read_provider_factory
class _LazyAuthProvider:
    """Stands in for the real auth provider between import and application startup.

    Module import must never require Supabase (or any) configuration: routers and
    middleware below only need a stable object to hold onto at import time, and they
    all read auth attributes lazily, per request. The first attribute access after
    startup builds (and caches, via get_auth_provider) the real provider, so a
    misconfigured deployment still fails closed -- just at the correct lifecycle
    point (startup / first use) instead of at import.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(get_auth_provider(), name)


AUTH: AuthProvider = _LazyAuthProvider()  # type: ignore[assignment]


def _better_auth_internal_base() -> str:
    host = str(os.getenv("TRADUTOR_AUTH_SERVICE_HOST", "127.0.0.1") or "127.0.0.1").strip()
    port = str(os.getenv("TRADUTOR_AUTH_SERVICE_PORT", "8787") or "8787").strip()
    base = str(os.getenv("BETTER_AUTH_INTERNAL_URL", f"http://{host}:{port}") or "").strip().rstrip("/")
    parsed = urllib_parse.urlparse(base)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise HTTPException(status_code=503, detail="auth_service_not_configured")
    return urllib_parse.urlunparse(parsed)


def _forward_auth_headers(request: Request) -> dict[str, str]:
    allowed = {"cookie", "content-type", "accept", "origin", "referer", "user-agent"}
    forwarded: dict[str, str] = {}
    for name, value in request.headers.items():
        lower = name.lower()
        if lower in allowed:
            forwarded[name] = value
    return forwarded


def _proxied_auth_response(target: str, request: Request, body: bytes) -> Response:
    headers = _forward_auth_headers(request)
    upstream = urllib_request.Request(
        target,
        data=body if request.method.upper() not in {"GET", "HEAD"} else None,
        headers=headers,
        method=request.method.upper(),
    )
    try:
        with urllib_request.urlopen(upstream, timeout=10) as raw:  # noqa: S310 - target is loopback-only
            payload = raw.read()
            status = raw.status
            response_headers = raw.headers
    except urllib_error.HTTPError as exc:
        payload = exc.read()
        status = exc.code
        response_headers = exc.headers
    except OSError as exc:
        raise HTTPException(status_code=503, detail="auth_service_unavailable") from exc

    safe_headers = {"Cache-Control": "no-store"}
    for header in ("content-type", "location"):
        value = response_headers.get(header)
        if value:
            safe_headers[header] = value
    response = Response(content=payload, status_code=status, headers=safe_headers)
    for cookie in response_headers.get_all("Set-Cookie", []):
        response.headers.append("Set-Cookie", cookie)
    return response


def _sync_public_profile(principal: RequestPrincipal) -> dict[str, Any]:
    """Project the authenticated local profile into the community read model.

    The principal is the only identity accepted here; browser payloads never select
    the profile row.  Media keys are opaque local markers, not filesystem paths.
    """
    profile = _profile_for_principal(principal)
    avatar_ref = str(profile.get("avatar_media_path") or "")
    banner_ref = str(profile.get("banner_media_path") or "")
    return COMMUNITY.store.upsert_profile(principal.user_id, {
        "display_name": profile.get("display_name") or "Usuário",
        "avatar_object_key": avatar_ref if avatar_ref.startswith("drive:") else ("local:avatar" if BRIDGE.profile_media_path("avatar", user_id=principal.user_id) else ""),
        "banner_object_key": banner_ref if banner_ref.startswith("drive:") else ("local:banner" if BRIDGE.profile_media_path("banner", user_id=principal.user_id) else ""),
        "public_role": profile.get("title") or "",
        "pronouns": profile.get("pronouns") or "",
        "status": profile.get("status") or "online",
        "status_message": profile.get("status_text") or "",
        "bio": profile.get("bio") or "",
        "accent_color": profile.get("avatar_color") or "#c5372c",
    })


COMMUNITY._profile_sync = _sync_public_profile


def _profile_for_principal(principal: RequestPrincipal) -> dict[str, Any]:
    """Resolve profile presentation without weakening the authenticated principal."""
    profile = dict(BRIDGE.profile_for_user(principal.user_id))
    public_identity = getattr(AUTH, "public_identity", None)
    if callable(public_identity):
        identity = public_identity(principal.user_id)
        if isinstance(identity, dict) and identity.get("display_name"):
            profile["display_name"] = str(identity["display_name"])
    profile["user_id"] = principal.user_id
    return profile


def _remote_profile_for_principal(principal: RequestPrincipal, request: Request) -> dict[str, Any]:
    """Adapt the shared Supabase social DTO to the main Profile form."""
    repo = globals().get("_SOCIAL_REPO")
    if repo is None or _SOCIAL_STATUS.get("provider") != "supabase" or not _SOCIAL_STATUS.get("available"):
        raise RuntimeError("remote_profile_unavailable")
    token = _license_bearer_token(request)
    if not token:
        raise AuthenticationRequired()
    remote = repo.get_my_profile(token, principal.user_id)
    local = BRIDGE.profile_for_user(principal.user_id)
    avatar_ref = str(local.get("avatar_media_path") or "")
    banner_ref = str(local.get("banner_media_path") or "")
    # Profile media is owned by the existing Drive-backed bridge even when the
    # social/profile DTO comes from Supabase. Keep the opaque local reference in
    # the presentation payload so a remote profile refresh cannot erase it.
    avatar_media_present = bool(remote.get("avatar_object_key") or avatar_ref)
    banner_media_present = bool(remote.get("banner_object_key") or banner_ref)
    return {
        "user_id": principal.user_id,
        "display_name": remote.get("display_name") or "",
        "title": remote.get("public_role") or "",
        "pronouns": remote.get("pronouns") or "",
        "status": remote.get("status") or "online",
        "status_text": remote.get("status_message") or "",
        "bio": remote.get("bio") or "",
        "avatar_mode": "image" if avatar_media_present else "letter",
        "avatar_color": remote.get("accent_color") or "#c5372c",
        "banner": "custom" if banner_media_present else "ink",
        # Remote Drive refs do not always carry MIME metadata in the social DTO.  A
        # non-empty ref is nevertheless an image by this endpoint's contract; defaulting
        # to an image MIME lets the client fetch the authenticated bytes and validate the
        # actual response type instead of falling back to initials.
        "avatar_media_url": "/api/ui/profile/media/avatar?v=remote" if avatar_media_present else "",
        "banner_media_url": "/api/ui/profile/media/banner?v=remote" if banner_media_present else "",
        "avatar_media_type": local.get("avatar_media_type") or remote.get("avatar_media_type") or "image/jpeg",
        "banner_media_type": local.get("banner_media_type") or remote.get("banner_media_type") or "image/jpeg",
        "profile_configured": bool(remote.get("profile_configured")),
        "remote_source": True,
    }


def _remote_profile_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "display_name": payload.get("display_name"),
        "public_role": payload.get("title"),
        "pronouns": payload.get("pronouns"),
        "status": payload.get("status"),
        "status_message": payload.get("status_text"),
        "bio": payload.get("bio"),
        "accent_color": payload.get("avatar_color"),
    }


def _enrich_history_publications(history: list[dict[str, Any]]) -> None:
    """Join verified local history records to an existing community post only."""
    for record in history:
        job_id = str(record.get("job_id") or "")
        run_id = str(record.get("run_id") or "")
        pdf_sha = str(record.get("pdf_sha256") or "").casefold()
        if not job_id or not run_id or len(pdf_sha) != 64:
            continue
        post = COMMUNITY.store.post_for_any_source(job_id)
        if not post or str(post.get("source_run_id") or "") != run_id:
            continue
        file = COMMUNITY.store.file_for_post(str(post.get("id") or ""))
        if not file or str(file.get("sha256") or "").casefold() != pdf_sha:
            continue
        if str(post.get("status") or "").casefold() != "published":
            continue
        record.update({
            "publication_status": "published",
            "publication_id": str(post.get("id") or ""),
            "publication_pdf_sha256": pdf_sha,
            "published_at": post.get("published_at") or "",
        })

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(MutationRateLimitMiddleware)
app.add_middleware(CommunityNetworkBoundaryMiddleware, auth=AUTH)
app.add_middleware(
    StructuredRequestAuditMiddleware,
    destination=configured_request_audit_destination(ROOT, os.environ),
)
app.add_static_files("/static", STATIC_DIR)
app.include_router(create_community_router(COMMUNITY, AUTH))
app.include_router(create_admin_community_router(COMMUNITY, AUTH))

# Supabase social API (works/chapters/comments/…). Mounted only when the social provider
# is configured; the browser never reaches the tables directly — every call forwards the
# user's JWT to the Data API under RLS. Fails closed (skips mounting) if unconfigured.
#
# `_SOCIAL_STATUS` is the sanitized, frontend-facing record of what happened: it never
# carries the SUPABASE_URL, keys or a raw exception, only a closed set of reason codes.
# api_bootstrap() merges it into the "community" payload so the browser can pick the
# matching UI (Supabase social vs. the legacy SQLite one) instead of guessing from DOM
# elements that are always present regardless of which provider is active.
_configured_social_provider = str(
    os.environ.get("COMMUNITY_SOCIAL_PROVIDER", "supabase") or "supabase"
).strip().lower()
_SOCIAL_STATUS: dict[str, Any] = {
    "provider": _configured_social_provider,
    "available": False,
    "reason_code": "social_provider_initialization_failed",
}
try:
    from social_http import create_social_router
    from social_repository import build_social_repository
    from supabase_social import SocialConfigError

    try:
        _SOCIAL_REPO = build_social_repository()
        app.include_router(create_social_router(_SOCIAL_REPO, AUTH))

        # Explicit PDF publishing + protected reader. The chapter→publication link lives in
        # server-side SQLite (no service_role / private-schema credential); the Drive id is
        # resolved server-side and never exposed. The browser never sends a path.
        from chapter_asset_repository import ChapterAssetRepository
        from social_content import SocialContentService
        from social_pdf_publishing import SocialPdfPublishingService
        from social_pdf_http import create_social_pdf_router

        _ASSET_REPO = ChapterAssetRepository(
            BRIDGE.runtime_root / "social_assets.sqlite3", community_store=COMMUNITY.store)
        _CONTENT = SocialContentService(_SOCIAL_REPO, _ASSET_REPO)
        # Retention: a replaced/unlinked PDF is retained (never deleted) so the owner can
        # restore it. Nothing sweeps automatically here — trashing is operator-invoked via
        # social_asset_maintenance_cli.py.
        from social_asset_retention import SocialAssetRetentionService

        _RETENTION = SocialAssetRetentionService(_ASSET_REPO)
        _PUBLISHING = SocialPdfPublishingService(COMMUNITY, _ASSET_REPO, _SOCIAL_REPO, BRIDGE.store,
                                                 retention=_RETENTION)
        app.include_router(create_social_pdf_router(_PUBLISHING, _CONTENT, AUTH,
                                                    retention=_RETENTION))
        _SOCIAL_STATUS = {"provider": "supabase", "available": True, "reason_code": None}
    except SocialConfigError as exc:
        print(f"social API not mounted: {exc}")
        if _configured_social_provider == "local":
            # Expected, not an error: the legacy SQLite community UI is the intended
            # experience for this installation and stays fully functional on its own.
            _SOCIAL_STATUS = {"provider": "local", "available": True, "reason_code": None}
        elif _configured_social_provider == "supabase":
            _SOCIAL_STATUS = {
                "provider": "supabase", "available": False,
                "reason_code": "social_provider_not_configured",
            }
        else:
            _SOCIAL_STATUS = {
                "provider": "unknown", "available": False,
                "reason_code": "social_provider_unknown",
            }
except Exception as exc:  # never let an optional feature break app startup
    print(f"social API not mounted: {type(exc).__name__}")
    _SOCIAL_STATUS = {
        "provider": _configured_social_provider, "available": False,
        "reason_code": "social_provider_initialization_failed",
    }


def _api_call(callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return callback(*args, **kwargs)
    except ValueError as exc:
        raw = str(exc)
        code = raw if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,79}", raw) else "operation_failed"
        raise HTTPException(status_code=400, detail=code) from exc


def _job_principal(request: Request) -> RequestPrincipal:
    """Optionally bind a local translation job to a validly authenticated user."""
    try:
        principal = AUTH.authenticate_request(request)
    except AuthenticationRequired as exc:
        # A credential was presented (cookie or Bearer) but failed validation.
        raise HTTPException(status_code=401, detail="authentication_required") from exc
    if not principal.authenticated and request.cookies.get(SESSION_COOKIE_NAME):
        raise HTTPException(status_code=401, detail="authentication_required")
    if principal.authenticated:
        try:
            AUTH.require_csrf(request, principal)
        except CsrfRejected as exc:
            raise HTTPException(status_code=403, detail="csrf_rejected") from exc
    return principal


def _license_bearer_token(request: Request) -> str:
    """Return the current Bearer token for in-memory license RPC use only."""

    raw = str(request.headers.get("authorization", "") or "")
    scheme, _, token = raw.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


def _ui_principal(request: Request, *, mutate: bool = False) -> RequestPrincipal:
    """Require the canonical auth principal for identity-bound UI resources."""
    try:
        principal = AUTH.require_authenticated(request)
        if mutate:
            AUTH.require_csrf(request, principal)
        return principal
    except CsrfRejected as exc:
        raise HTTPException(status_code=403, detail="csrf_rejected") from exc
    except AuthenticationRequired as exc:
        raise HTTPException(status_code=401, detail="authentication_required") from exc


def _owned_ui_job(
    request: Request, job_id: str, *, mutate: bool = False
) -> RequestPrincipal:
    principal = _ui_principal(request, mutate=mutate)
    if BRIDGE.store.get_job_for_owner(principal.owner_id, str(job_id or "")) is None:
        # Deliberately indistinguishable from an absent resource.
        raise HTTPException(status_code=404, detail="not_found")
    return principal


def _local_folder_submit_allowed(request: Request) -> bool:
    """A browser may submit a filesystem folder only to a loopback-only UI server."""

    peer = str(getattr(getattr(request, "client", None), "host", "") or "")
    return local_folder_ui_allowed(bind_host=APP_HOST, peer_host=peer)


@app.get("/auth/callback")
def auth_callback(request: Request) -> Response:
    """Supabase e-mail confirmation/login landing page.

    Static file, no query/hash parameter is ever echoed back, and the page always
    returns to the fixed local root — no open redirect surface.
    """
    params = request.query_params
    desktop = params.get("desktop") == "1"
    handoff_id = str(params.get("handoff") or "")
    code = str(params.get("code") or "")
    if desktop and handoff_id and code and len(code) <= 4096:
        with _DESKTOP_HANDOFF_LOCK:
            _handoff_cleanup()
            item = _DESKTOP_HANDOFFS.get(handoff_id)
            if item and not item.get("consumed"):
                item["code"] = code
                item["ready"] = True
        # Finish server-side immediately. The external browser must not need
        # JavaScript or a PKCE client; only the WebView2 instance exchanges the
        # one-use code after polling this local handoff.
        return HTMLResponse(
            "<!doctype html><html lang='pt-BR'><meta charset='utf-8'>"
            "<title>Yomu Sekai</title><style>body{font-family:system-ui;"
            "background:#14110f;color:#f3ede4;display:grid;place-items:center;"
            "height:100vh;margin:0}.card{text-align:center;padding:2rem}"
            "</style><main class='card'><h1>Solicitação recebida</h1>"
            "<p>Você pode voltar ao Yomu Sekai para continuar.</p>"
            "<p>Esta janela pode ser fechada.</p></main></html>",
            headers={"Cache-Control": "no-store"},
        )
    return FileResponse(ROOT / "ui" / "auth_callback.html", media_type="text/html")


@app.get("/api/runtime")
def runtime_metadata() -> JSONResponse:
    """Non-sensitive identity used by the desktop launcher to avoid stale runtimes."""
    return JSONResponse({
        "mode": "desktop" if os.getenv("TRADUTOR_RUNTIME_MODE") == "desktop" else "browser",
        "instance": _RUNTIME_INSTANCE_ID,
    }, headers={"Cache-Control": "no-store"})


def _handoff_cleanup(now: float | None = None) -> None:
    cutoff = time.time() if now is None else now
    for key, value in list(_DESKTOP_HANDOFFS.items()):
        if cutoff - float(value.get("created_at", 0)) > _DESKTOP_HANDOFF_TTL_SECONDS:
            _DESKTOP_HANDOFFS.pop(key, None)


@app.post("/api/auth/desktop/handoff/start")
def desktop_handoff_start() -> JSONResponse:
    handoff_id = secrets.token_urlsafe(32)
    with _DESKTOP_HANDOFF_LOCK:
        _handoff_cleanup()
        _DESKTOP_HANDOFFS[handoff_id] = {
            "created_at": time.time(), "flow_type": "PASSWORD_RECOVERY",
            "code": None, "consumed": False,
        }
    return JSONResponse({"handoff_id": handoff_id, "ttl_seconds": _DESKTOP_HANDOFF_TTL_SECONDS}, headers={"Cache-Control": "no-store"})


@app.post("/api/auth/desktop/handoff/{handoff_id}/callback")
async def desktop_handoff_callback(handoff_id: str, request: Request) -> JSONResponse:
    body = await request.json()
    code = str(body.get("code") or "")
    if not code or len(code) > 4096:
        raise HTTPException(status_code=400, detail="invalid_callback")
    with _DESKTOP_HANDOFF_LOCK:
        _handoff_cleanup()
        item = _DESKTOP_HANDOFFS.get(handoff_id)
        if not item or item.get("consumed"):
            raise HTTPException(status_code=404, detail="handoff_not_found")
        item["code"] = code
    return JSONResponse({"status": "received"}, headers={"Cache-Control": "no-store"})


@app.get("/api/auth/desktop/handoff/{handoff_id}/status")
def desktop_handoff_status(handoff_id: str) -> JSONResponse:
    with _DESKTOP_HANDOFF_LOCK:
        _handoff_cleanup()
        item = _DESKTOP_HANDOFFS.get(handoff_id)
        if not item:
            raise HTTPException(status_code=404, detail="handoff_not_found")
        return JSONResponse({"ready": bool(item.get("code")), "flow_type": item.get("flow_type")}, headers={"Cache-Control": "no-store"})


@app.get("/api/auth/desktop/handoff/diagnostics")
def desktop_handoff_diagnostics() -> JSONResponse:
    """Sanitized DEV diagnostics; capability IDs and codes are never returned."""
    with _DESKTOP_HANDOFF_LOCK:
        _handoff_cleanup()
        now = time.time()
        values = list(_DESKTOP_HANDOFFS.values())
        ready = [item for item in values if item.get("code") and not item.get("consumed")]
        waiting = [item for item in values if not item.get("code") and not item.get("consumed")]
        return JSONResponse({
            "active_count": len(values),
            "total_count": len(values),
            "waiting_count": len(waiting),
            "ready_count": len(ready),
            "consumed_count": 0,
            "oldest_age_seconds": max((int(now - float(item.get("created_at", now))) for item in values), default=0),
            "has_auth_code": bool(ready),
            "flow_type": "PASSWORD_RECOVERY" if values else "NONE",
            "current_runtime_pending": bool(values),
            "current_runtime_state": "READY" if ready else "WAITING" if waiting else "NONE",
            "client": dict(_DESKTOP_CLIENT_DIAGNOSTICS),
        }, headers={"Cache-Control": "no-store"})


@app.post("/api/auth/desktop/client-state")
async def desktop_client_state(request: Request) -> JSONResponse:
    """Accept only sanitized client-state diagnostics in desktop development mode."""
    if os.getenv("TRADUTOR_RUNTIME_MODE") != "desktop":
        raise HTTPException(status_code=404, detail="not_found")
    body = await request.json()
    allowed = {"pending", "polling", "lastStatus", "consumed", "authMode", "sessionState", "idPresent", "ageSeconds", "error", "errorStatus"}
    snapshot: dict[str, Any] = {}
    for key in allowed:
        value = body.get(key)
        if key in {"pending", "polling", "consumed", "idPresent"}:
            snapshot[key] = bool(value)
        elif key in {"ageSeconds", "errorStatus"}:
            snapshot[key] = max(0, min(int(value or 0), _DESKTOP_HANDOFF_TTL_SECONDS))
        else:
            snapshot[key] = str(value or "")[:40]
    _DESKTOP_CLIENT_DIAGNOSTICS.clear()
    _DESKTOP_CLIENT_DIAGNOSTICS.update(snapshot)
    return JSONResponse({"status": "accepted"}, headers={"Cache-Control": "no-store"})


@app.get("/api/internal/auth-diagnostics")
def auth_diagnostics_get() -> JSONResponse:
    if not _AUTH_DIAGNOSTICS_ENABLED:
        raise HTTPException(status_code=404, detail="not_found")
    return JSONResponse({"events": list(_AUTH_DIAGNOSTIC_EVENTS)}, headers={"Cache-Control": "no-store"})


@app.post("/api/internal/auth-diagnostics")
async def auth_diagnostics_post(request: Request) -> JSONResponse:
    if not _AUTH_DIAGNOSTICS_ENABLED:
        raise HTTPException(status_code=404, detail="not_found")
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="invalid_event")
    allowed = {"event", "at", "seq", "status", "code", "name", "message", "authenticated", "source", "token_present", "token_length", "elapsed_ms", "reason", "destination", "auth_event", "session_present_before_login", "app_open_reason", "caller", "window_role", "session_fingerprint", "session_present", "user_present", "build_id", "request_trace_id", "daily", "subscription", "permanent", "reserved", "active_yk", "top_text", "rewards_text", "http_status"}
    event: dict[str, Any] = {}
    for key in allowed:
        if key not in body:
            continue
        value = body[key]
        if key in {"authenticated", "token_present", "session_present_before_login", "session_present", "user_present"}:
            event[key] = bool(value)
        elif key == "seq":
            try: event[key] = max(0, min(int(value), 2**63 - 1))
            except (TypeError, ValueError): continue
        elif key == "at":
            event[key] = str(value or "")[:40]
        elif key in {"status", "token_length", "elapsed_ms", "daily", "subscription", "permanent", "reserved", "active_yk", "http_status"}:
            try: event[key] = max(0, min(int(value), 2**63 - 1))
            except (TypeError, ValueError): continue
        else:
            event[key] = str(value or "")[:160]
    if not event.get("event"):
        raise HTTPException(status_code=400, detail="invalid_event")
    _AUTH_DIAGNOSTIC_EVENTS.append(event)
    return JSONResponse({"status": "accepted"}, headers={"Cache-Control": "no-store"})


@app.post("/api/internal/jwks-exact-diagnostics")
def jwks_exact_diagnostics() -> JSONResponse:
    """Read-only same-process JWKS timing probe, available only with auth diagnostics."""
    if not _AUTH_DIAGNOSTICS_ENABLED:
        raise HTTPException(status_code=404, detail="not_found")
    provider = get_auth_provider()
    jwks = getattr(provider, "_jwks", None)
    transport = getattr(jwks, "_transport", None)
    if jwks is None:
        raise HTTPException(status_code=503, detail="jwks_cache_unavailable")
    started = time.perf_counter()
    try:
        jwks._fetch()
        cache_status = "ok"
    except Exception as exc:
        cache_status = type(exc).__name__
    cache_ms = int((time.perf_counter() - started) * 1000)
    direct_ms = None
    direct_status = "not_run"
    if transport is not None:
        started = time.perf_counter()
        try:
            response = transport.request("GET", jwks._jwks_url, headers={"Accept": "application/json"})
            direct_status = f"http_{response.status}"
        except Exception as exc:
            direct_status = type(exc).__name__
        direct_ms = int((time.perf_counter() - started) * 1000)
    return JSONResponse({"transport_direct_ms": direct_ms, "transport_status": direct_status,
                         "jwks_cache_ms": cache_ms, "jwks_cache_status": cache_status,
                         "generation": getattr(jwks, "_generation", 0)},
                        headers={"Cache-Control": "no-store"})


@app.post("/api/auth/desktop/handoff/{handoff_id}/consume")
def desktop_handoff_consume(handoff_id: str) -> JSONResponse:
    with _DESKTOP_HANDOFF_LOCK:
        _handoff_cleanup()
        item = _DESKTOP_HANDOFFS.get(handoff_id)
        if not item or item.get("consumed") or not item.get("code"):
            raise HTTPException(status_code=404, detail="handoff_not_ready")
        code = item["code"]
        item["consumed"] = True
        _DESKTOP_HANDOFFS.pop(handoff_id, None)
    return JSONResponse({"code": code}, headers={"Cache-Control": "no-store"})


@app.api_route("/api/auth/{auth_path:path}", methods=["GET", "POST"])
async def better_auth_proxy(auth_path: str, request: Request) -> Response:
    """Same-origin bridge to the loopback-only Better Auth service.

    This is intentionally not configurable by request.  The only destination is
    BETTER_AUTH_INTERNAL_URL / TRADUTOR_AUTH_SERVICE_HOST:PORT, validated as loopback.
    """

    if getattr(AUTH, "auth_source", "") != "better_auth":
        raise HTTPException(status_code=404, detail="not_found")
    suffix = urllib_parse.quote(str(auth_path or "").lstrip("/"), safe="/-._~")
    query = request.url.query
    target = f"{_better_auth_internal_base()}/api/auth/{suffix}"
    if query:
        target = f"{target}?{query}"
    return _proxied_auth_response(target, request, await request.body())


@app.get("/api/health")
def api_health() -> dict[str, str]:
    """Liveness of this local UI server, for the page's connection indicator.

    Deliberately unauthenticated and stateless: the badge has to work before and
    after sign-in, and a health probe must never be somewhere user state leaks.
    """

    return {"status": "ok"}


@app.get("/api/ui/bootstrap")
def api_bootstrap(request: Request, cursor: int = Query(0, ge=0)) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "ready",
        "history": [],
        "queue": [],
        "resumable": [],
        "active": None,
        "latest": None,
        "latest_result": None,
        "quality_review": None,
        "logs": [],
        "log_cursor": 0,
        "profile": {},
        "settings": {},
        "standalone_source_ready": None,
        "community": {
            "authenticated": False,
            "auth_state": "unauthenticated",
            "user_id": "",
            "available": True,
            "social": _SOCIAL_STATUS,
            "requires_canonical_chapter_publication": (
                COMMUNITY.requires_canonical_publication_identity()
            ),
        },
    }
    try:
        principal = AUTH.authenticate_request(request)
        if not principal.authenticated:
            return payload
        payload = BRIDGE.bootstrap(cursor, principal=principal)
        _enrich_history_publications(payload.get("history") or [])
        payload["community"] = {
            **(payload.get("community") or {}),
            "authenticated": bool(principal.authenticated),
            "auth_state": "authenticated" if principal.authenticated else "unauthenticated",
            "user_id": principal.user_id if principal.authenticated else "",
            "available": True,
            "social": _SOCIAL_STATUS,
            "requires_canonical_chapter_publication": (
                COMMUNITY.requires_canonical_publication_identity()
            ),
        }
        if _SOCIAL_STATUS.get("provider") == "supabase" and _SOCIAL_STATUS.get("available"):
            try:
                payload["profile"] = _remote_profile_for_principal(principal, request)
            except Exception:
                payload["profile"] = {"profile_error": True, "remote_source": True}
                payload["community"]["profile_load_failed"] = True
        else:
            payload["profile"] = _profile_for_principal(principal)
        try:
            if _SOCIAL_STATUS.get("provider") != "supabase":
                _sync_public_profile(principal)
        except Exception:
            # Profile projection is optional presentation data. A duplicate display
            # name or unavailable community store must not invalidate a verified
            # authentication session.
            payload["community"]["profile_sync_failed"] = True
    except Exception:
        # Bootstrap is read-only; an expired or malformed credential must not prevent
        # the local library from loading. Publication itself remains fail-closed in
        # the community API.
        payload["community"] = {
            **(payload.get("community") or {}),
            "authenticated": False,
            "auth_state": "auth_error",
            "user_id": "",
            "available": True,
            "social": _SOCIAL_STATUS,
            "requires_canonical_chapter_publication": (
                COMMUNITY.requires_canonical_publication_identity()
            ),
        }
        payload["profile"] = {}
    return payload


@app.get("/api/ui/state")
def api_state(request: Request, cursor: int = Query(0, ge=0)) -> dict[str, Any]:
    principal = _ui_principal(request)
    return BRIDGE.runtime_state_for_owner(principal.owner_id, cursor)


@app.post("/api/ui/source/analyze")
async def api_source_analyze(
    request: Request,
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    """Validate a URL without creating a translation job or queue item."""
    from chapter_source import SourceError, supported_hosts

    trace_id = _source_trace_id(payload)
    _append_diagnostic_log("routes_current.jsonl", "START_ROUTE_ENTER", trace_id=trace_id, route="/api/ui/source/analyze", method="POST")
    _append_diagnostic_log("app_current.jsonl", "SOURCE_ACTION_BEGIN", trace_id=trace_id, action="source_analyze")

    try:
        principal = _ui_principal(request, mutate=True)
        result = await BRIDGE.analyze_source_candidate(payload, principal=principal)
        _append_diagnostic_log("routes_current.jsonl", "SOURCE_ANALYSIS_RESULT", trace_id=trace_id, route="/api/ui/source/analyze", status="ready" if result.get("ready") is True else "blocked", ready=result.get("ready") is True, reason_code=str(result.get("reason_code") or "")[:80])
        _append_diagnostic_log("app_current.jsonl", "SOURCE_ACTION_RESULT", trace_id=trace_id, action="source_analyze", result="ready" if result.get("ready") is True else "blocked")
        # Analysis is deliberately side-effect free.  Job creation belongs exclusively to
        # /api/ui/run; keeping that ownership in one route prevents a one-click request
        # from creating one job here and another when the client continues explicitly.
        return result
    except SourceError as exc:
        raise HTTPException(status_code=422, detail={
            "code": exc.code,
            "stage": "validacao_da_fonte",
            "message": "Não foi possível validar esta fonte com segurança.",
            "hosts": supported_hosts(),
            "action": "Revise a URL e tente novamente.",
        }) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={
            "code": str(exc),
            "stage": "validacao_da_fonte",
            "message": "Não foi possível validar esta fonte.",
            "action": "Revise os campos e tente novamente.",
        }) from exc
    except Exception as exc:
        # SOURCE-ANALYSIS-OBSERVABILITY-001. This failure happens before any job row
        # exists, so there is no job status, no history entry and no per-job log to carry
        # the evidence: without this branch the user saw a bare 500 and the exception was
        # lost. No job is created here - the user is simply told it is retryable, and the
        # sanitized class is recorded for diagnosis.
        from down import _pipeline_exception_code

        code = _pipeline_exception_code(exc)
        if isinstance(exc, TimeoutError) and code == "browser_startup_timeout":
            code = "source_timeout"
        # Host only: never the path or query string, which is where a signed token or a
        # session identifier would sit.
        host = urllib_parse.urlsplit(str((payload or {}).get("url") or "")).hostname or ""
        print(
            "source_analysis_failed_before_job "
            f"code={code} exception={type(exc).__name__} stage=source_analysis "
            f"host={host[:120]}",
            flush=True,
        )
        raise HTTPException(status_code=502, detail={
            "code": code,
            "stage": "analise_da_fonte",
            "message": "Não foi possível analisar esta fonte agora. Tente novamente.",
            "action": "Verifique sua conexão e tente novamente.",
        }) from exc


@app.post("/api/ui/source/report")
def api_source_report(
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    """Queue a sanitized, user-consented unsupported-source report locally."""
    from source_support_report import (
        LocalOutboxSourceSupportReporter,
        build_source_support_report,
    )

    try:
        report = build_source_support_report(payload or {})
        reporter = LocalOutboxSourceSupportReporter(
            ROOT / ".cache" / "runtime" / "source_support_reports.jsonl")
        return reporter.submit(report).public()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "code": str(exc),
            "message": "Não foi possível registrar esta fonte com segurança.",
        }) from exc


@app.post("/api/ui/run")
async def api_run(
    request: Request,
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    from chapter_source import SourceError, supported_hosts

    try:
        trace_id = _source_trace_id(payload)
        _append_diagnostic_log("routes_current.jsonl", "SOURCE_RUN_ROUTE_ENTER", trace_id=trace_id, route="/api/ui/run", method="POST")
        _append_diagnostic_log("app_current.jsonl", "WALLET_CHECK_STARTED", trace_id=trace_id, required_yk=1)
        # Wallet entitlement is enforced by the remote lifecycle RPC, not by this local
        # queue boundary. Record that fact explicitly instead of implying a local balance.
        _append_diagnostic_log("app_current.jsonl", "WALLET_CHECK_RESULT", trace_id=trace_id, result="DEFERRED_REMOTE", sufficient=None, required_yk=1)
        translation_enabled = payload.get("translation_enabled") is True
        _append_diagnostic_log(
            "app_current.jsonl", "TRANSLATION_POLICY_RESOLVED", trace_id=trace_id,
            source="control_plane_snapshot" if "translation_enabled" in payload else "fail_closed_default",
            translation_enabled=translation_enabled, allow_translation=translation_enabled,
        )
        _append_diagnostic_log(
            "app_current.jsonl", "TRANSLATION_KILL_SWITCH_CHECKED", trace_id=trace_id,
            result="ENABLED" if translation_enabled else "DISABLED",
            translation_enabled=translation_enabled, allow_translation=translation_enabled,
        )
        requested_type = str(payload.get("source_type") or "").strip().casefold()
        requests_local_folder = requested_type == "local_folder" or bool(
            str(payload.get("local_folder") or "").strip())
        if requests_local_folder and not _local_folder_submit_allowed(request):
            # Do not pass the raw folder to the bridge, error handler, or logs when this
            # server is externally bound. This feature is intentionally unavailable there.
            raise HTTPException(status_code=403, detail={
                "code": "local_folder_requires_loopback_ui",
                "stage": "validacao_da_fonte",
                "message": "A pasta local só pode ser enviada pelo painel em loopback.",
                "action": "Abra o painel local em 127.0.0.1 ou localhost.",
            })
        guarded_payload = dict(payload)
        if not requests_local_folder:
            # The normal UI now obtains this analysis inside the Start click. Direct URL
            # submissions without that fresh result still fail before job creation.
            guarded_payload["source_validation_required"] = True
        _append_diagnostic_log(
            "app_current.jsonl", "FORCE_REPROCESSING_DECISION", trace_id=trace_id,
            source="ui_payload" if "force" in payload else "backend_default",
            force=bool(payload.get("force", False)),
        )
        _append_diagnostic_log(
            "app_current.jsonl", "RUN_REQUEST_PAYLOAD_READY", trace_id=trace_id,
            route="/api/ui/run", force=bool(guarded_payload.get("force", False)),
        )
        _append_diagnostic_log("app_current.jsonl", "JOB_CREATE_BEGIN", trace_id=trace_id, route="/api/ui/run")
        _append_diagnostic_log("app_current.jsonl", "QUEUE_INSERT_BEGIN", trace_id=trace_id, mode="persistent_local_queue")
        result = await BRIDGE.start(
            guarded_payload,
            principal=_ui_principal(request, mutate=True),
            local_folder_allowed=requests_local_folder,
            license_access_token=_license_bearer_token(request),
        )
        _append_diagnostic_log(
            "routes_current.jsonl", "SOURCE_RUN_ROUTE_RESULT", trace_id=trace_id,
            route="/api/ui/run", status="ok", reason_code=str(result.get("reason_code") or "")[:80],
        )
        _append_diagnostic_log("app_current.jsonl", "JOB_CREATE_RESULT", trace_id=trace_id, status="success" if result.get("job_id") else "not_created", job_created=bool(result.get("job_id")))
        if result.get("job_id"):
            _append_diagnostic_log("app_current.jsonl", "QUEUE_INSERT_RESULT", trace_id=trace_id, status="success", job_created=True)
        return result
    except SourceError as exc:
        # Source diagnostics are coded and deliberately generic: URL fragments, headers,
        # cookies and provider responses never reach the browser.
        messages = {
            "unsupported_source": ("Esta fonte ainda não é compatível com o Tradutor IA.", "Você pode enviá-la ao desenvolvedor para análise."),
            "challenge_required": ("O site exige uma verificação interativa.", "Conclua a verificação no site ou use uma fonte sem desafio."),
            "authentication_required": ("Esta fonte exige autenticação.", "Use uma página pública, sem login."),
            "source_access_denied": ("A fonte recusou o acesso público.", "Verifique a URL ou tente novamente mais tarde."),
            "source_rate_limited": ("Não foi possível acessar essa fonte agora.", "Aguarde antes de tentar novamente."),
            "source_transport_failed": ("Não foi possível acessar essa fonte agora.", "Tente novamente em alguns minutos."),
            "source_navigation_timeout": ("Não foi possível acessar essa fonte agora.", "Tente novamente em alguns minutos."),
            "source_unavailable": ("Não foi possível acessar essa fonte agora.", "Tente novamente em alguns minutos."),
            "no_chapter_images": ("A página informada não foi encontrada.", "Confirme que a URL abre o leitor do capítulo."),
            "unsupported_low_confidence": ("Esta fonte ainda não é compatível com o Tradutor IA.", "Você pode enviá-la ao desenvolvedor para análise."),
            "unsupported_canvas_reader": ("Esta fonte ainda não é compatível com o Tradutor IA.", "Você pode enviá-la ao desenvolvedor para análise."),
            "incomplete_source_coverage": ("Não foi possível ler o capítulo inteiro na página.", "Abra o capítulo completo no navegador e tente novamente."),
            "incomplete_download": ("As páginas não puderam ser baixadas por completo.", "Revise a fonte e tente novamente mais tarde."),
        }
        if exc.code.startswith("local_"):
            messages[exc.code] = (
                "A pasta local não pôde ser usada com segurança.",
                "Use uma pasta de capítulo permitida, sem links, e tente novamente.",
            )
        message, action = messages.get(
            exc.code, ("Não foi possível analisar esta fonte com segurança.", "Revise a URL e tente novamente."))
        raise HTTPException(status_code=400, detail={
            "code": exc.code,
            "stage": "validacao_da_fonte",
            "message": message,
            "hosts": supported_hosts(),
            "action": action,
        }) from exc
    except ValueError as exc:
        reason_code = str(exc)
        _append_diagnostic_log(
            "routes_current.jsonl", "SOURCE_RUN_ROUTE_RESULT", trace_id=_source_trace_id(payload),
            route="/api/ui/run", status="error", reason_code=reason_code[:80],
        )
        _append_diagnostic_log("app_current.jsonl", "JOB_CREATE_RESULT", trace_id=_source_trace_id(payload), status="failure", reason_code=reason_code[:80])
        if reason_code == "insufficient_yk":
            _append_diagnostic_log("app_current.jsonl", "WALLET_FETCH_RESULT", result="INSUFFICIENT_BALANCE")
            raise HTTPException(status_code=402, detail={
                "code": "INSUFFICIENT_YOMU_KEYS",
                "stage": "wallet",
                "message": "Yomu Keys insuficientes para iniciar esta tradução.",
            }) from exc
        if reason_code in {
            "download_authorization_required",
            "explicit_download_request_required",
            "workspace_source_authorization_required",
            "source_validation_required",
            "pipeline_intent_required",
            "workspace_policy_hash_mismatch",
        }:
            message = {
                "download_authorization_required":
                    "É necessária uma autorização de conteúdo antes de iniciar o download.",
                "explicit_download_request_required":
                    "Use a ação separada de download autorizado para continuar.",
                "workspace_source_authorization_required":
                    "A política de fontes autorizadas está desativada.",
                "source_validation_required":
                    "Valide novamente esta origem antes de iniciar a tradução.",
                "pipeline_intent_required":
                    "A análise existente não possui uma solicitação de pipeline vinculada.",
                "workspace_policy_hash_mismatch":
                    "A política do workspace mudou; envie novamente a operação.",
            }[reason_code]
            raise HTTPException(status_code=409, detail={
                "code": reason_code,
                "stage": "autorizacao_de_conteudo",
                "message": message,
                "action": "Abra Configurações para revisar a política das fontes.",
            }) from exc
        if reason_code == "device_limit_reached":
            raise HTTPException(status_code=409, detail={
                "code": reason_code,
                "stage": "dispositivo",
                "message": "O limite de dispositivos autorizados foi atingido.",
                "action": "Gerencie os dispositivos autorizados e tente novamente.",
            }) from exc
        raise HTTPException(status_code=400, detail={
            "code": "invalid_request", "stage": "validacao",
            "message": reason_code, "action": "Corrija os campos e tente novamente.",
        }) from exc
    except BaseException as exc:
        _append_diagnostic_log(
            "routes_current.jsonl", "SOURCE_RUN_ROUTE_EXCEPTION",
            trace_id=_source_trace_id(payload), route="/api/ui/run",
            exception_type=type(exc).__name__,
        )
        _append_diagnostic_log(
            "app_current.jsonl", "JOB_CREATE_EXCEPTION",
            trace_id=_source_trace_id(payload), exception_type=type(exc).__name__,
        )
        raise


@app.post("/api/ui/cancel")
async def api_cancel(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    try:
        principal = _ui_principal(request, mutate=True)
        return await BRIDGE.cancel_for_owner(
            principal.owner_id,
            queue=bool(payload.get("queue", False)),
            job_id=str(payload.get("job_id") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "code": str(exc),
            "stage": "cancelamento",
            "message": "Não foi possível cancelar este processamento.",
            "action": "Atualize o painel e tente novamente.",
        }) from exc


@app.post("/api/ui/jobs/{job_id}/cancel")
async def api_job_cancel(request: Request, job_id: str) -> JSONResponse:
    """Cancel exactly the job named by the caller without waiting for its runner."""
    try:
        principal = _owned_ui_job(request, job_id, mutate=True)
        result = await BRIDGE.cancel_for_owner(
            principal.owner_id, job_id=str(job_id or ""))
    except ValueError as exc:
        code = str(exc)
        messages = {
            "job_not_found": "O processamento solicitado não foi encontrado.",
            "job_not_active": "Esse processamento não está ativo.",
        }
        raise HTTPException(status_code=404 if code == "job_not_found" else 409,
                            detail={"code": code, "stage": "cancelamento",
                                    "message": messages.get(code, "Não foi possível cancelar este processamento."),
                                    "action": "Atualize o painel e tente novamente."}) from exc
    status_code = 202 if result.get("status") == "cancelling" else 200
    return JSONResponse(result, status_code=status_code)


@app.get("/api/ui/quality-review/revision/{job_id}")
def api_quality_revision_status(request: Request, job_id: str) -> dict[str, Any]:
    _owned_ui_job(request, job_id)
    status = BRIDGE.quality_revision_status(job_id)
    if status is None:
        raise HTTPException(status_code=404, detail={
            "code": "quality_revision_not_available",
            "message": "Não há uma revisão iterativa disponível para este job.",
        })
    return status


@app.post("/api/ui/quality-review/revision/start")
def api_quality_revision_start(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    try:
        job_id = str(payload.get("job_id") or "")
        _owned_ui_job(request, job_id, mutate=True)
        return BRIDGE.start_quality_revision(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "code": str(exc),
            "message": "Não foi possível iniciar a revisão iterativa.",
            "action": "Confira se o capítulo possui PDF e relatório de qualidade persistidos.",
        }) from exc


@app.post("/api/ui/quality-review/revision/cancel")
def api_quality_revision_cancel(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    try:
        job_id = str(payload.get("job_id") or "")
        _owned_ui_job(request, job_id, mutate=True)
        return BRIDGE.cancel_quality_revision(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "code": str(exc),
            "message": "Não foi possível cancelar a revisão.",
            "action": "Recarregue a página e confira o estado atual da revisão.",
        }) from exc


@app.post("/api/ui/quality-review/revision/canary/start")
def api_quality_revision_canary_start(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    try:
        job_id = str(payload.get("job_id") or "")
        _owned_ui_job(request, job_id, mutate=True)
        return BRIDGE.start_quality_revision_canary(
            job_id,
            max_regions=int(payload.get("max_regions") or 10),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "code": str(exc),
            "message": "Não foi possível iniciar o canário do contrato NVIDIA.",
            "action": "Confira se o capítulo possui PDF e relatório de qualidade persistidos.",
        }) from exc


@app.post("/api/ui/review-rerun/plan")
def api_review_rerun_plan(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    try:
        job_id = str(payload.get("job_id") or "")
        principal = _owned_ui_job(request, job_id)
        return BRIDGE.review_rerun_plan_for_owner(principal.owner_id, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404 if str(exc) == "job_not_found" else 422, detail={
            "code": str(exc),
            "message": "Não foi possível preparar o rerun das pendências.",
            "action": "Atualize a revisão e confira os itens pendentes.",
        }) from exc


@app.post("/api/ui/review-rerun/start")
def api_review_rerun_start(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    try:
        job_id = str(payload.get("job_id") or "")
        principal = _owned_ui_job(request, job_id, mutate=True)
        raw_modes = payload.get("modes") or ["all_pending"]
        modes = [str(item) for item in raw_modes] if isinstance(raw_modes, list) else []
        raw_targets = payload.get("target_region_ids")
        target_region_ids = (
            [str(item) for item in raw_targets]
            if isinstance(raw_targets, list) else None
        )
        raw_parent_run = payload.get("parent_run_id")
        parent_run_id = str(raw_parent_run) if isinstance(raw_parent_run, str) and raw_parent_run else None
        return BRIDGE.start_review_rerun_for_owner(
            principal.owner_id,
            job_id,
            allow_provider=payload.get("allow_provider") is True,
            modes=modes,
            target_region_ids=target_region_ids,
            parent_run_id=parent_run_id,
        )
    except ValueError as exc:
        code = str(exc)
        raise HTTPException(status_code=409 if code == "provider_authorization_required" else 422, detail={
            "code": code,
            "message": (
                "Autorize explicitamente o uso da NVIDIA para os trechos sem tradução."
                if code == "provider_authorization_required"
                else "Não foi possível iniciar o rerun das pendências."
            ),
            "action": "Confira o resumo, as opções e tente novamente.",
        }) from exc


@app.post("/api/ui/review-rerun/status")
def api_review_rerun_status(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    job_id = str(payload.get("job_id") or "")
    principal = _owned_ui_job(request, job_id)
    try:
        return BRIDGE.review_rerun_status_for_owner(principal.owner_id, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": str(exc)}) from exc


@app.post("/api/ui/review-rerun/cancel")
async def api_review_rerun_cancel(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    job_id = str(payload.get("job_id") or "")
    principal = _owned_ui_job(request, job_id, mutate=True)
    try:
        return await BRIDGE.cancel_for_owner(principal.owner_id, job_id=job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc


def _page_revision_error(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail={
        "code": str(exc),
        "message": "Não foi possível concluir a ação da revisão da página.",
        "action": "Confira job, run e a revisão da página e tente novamente.",
    })

@app.get("/api/update/manifest")
def api_update_manifest() -> JSONResponse:
    """Read-only signed-manifest check used by the in-app Atualizações surface."""
    installed = BUILD_VERSION
    manifest_url = os.getenv("TRADUTOR_IA_UPDATE_MANIFEST_URL", DEFAULT_MANIFEST_URL).strip()
    if not manifest_url:
        return JSONResponse({"state": "not_configured", "version": installed,
                             "channel": update_manifest.CLIENT_UPDATE_CHANNEL,
                             "can_install": False}, headers={"Cache-Control": "no-store"})
    try:
        raw = update_transport.UpdateTransport().fetch_manifest(manifest_url)
        manifest = update_manifest.verify_manifest(raw, trusted_keys=update_manifest.load_trusted_keys())
        decision = update_manifest.decide_update(installed, manifest)
        return JSONResponse({"state": decision.state, "version": installed,
            "available_version": manifest.version_text, "channel": manifest.channel,
            "published_at": manifest.published_at.isoformat(),
            "package_size": manifest.package.size, "artifact_type": manifest.artifact_type,
            "release_notes": manifest.release_notes, "mandatory": decision.mandatory,
            "can_install": decision.should_install}, headers={"Cache-Control": "no-store"})
    except (update_manifest.UpdateError, update_transport.TransportError) as exc:
        return JSONResponse({"state": "error", "version": installed,
                             "channel": update_manifest.CLIENT_UPDATE_CHANNEL,
                             "can_install": False, "error": type(exc).__name__}, status_code=502,
                headers={"Cache-Control": "no-store"})


def _run_update_download(token: str, manifest: update_manifest.UpdateManifest) -> None:
    cancel_event = threading.Event()
    try:
        transport = update_transport.UpdateTransport()
        with _UPDATE_DOWNLOADS_LOCK:
            state = _UPDATE_DOWNLOADS[token]
            state.update(status="downloading", received=0, expected=manifest.package.size)
            cancel_event = state["cancel_event"]
        def progress(received: int, expected: int) -> None:
            with _UPDATE_DOWNLOADS_LOCK:
                if token in _UPDATE_DOWNLOADS:
                    _UPDATE_DOWNLOADS[token].update(received=received, expected=expected)
        path = installer_update.download_verified_installer(
            manifest, transport, progress_callback=progress, cancel_event=cancel_event)
        with _UPDATE_DOWNLOADS_LOCK:
            _UPDATE_DOWNLOADS[token].update(status="ready_to_install", path=path, version=manifest.version_text)
    except BaseException as exc:
        with _UPDATE_DOWNLOADS_LOCK:
            if token in _UPDATE_DOWNLOADS:
                _UPDATE_DOWNLOADS[token].update(status="cancelled" if cancel_event.is_set() else "error", error=type(exc).__name__)


@app.post("/api/update/download")
def api_update_download() -> JSONResponse:
    """Start a verified Windows installer download without blocking the UI thread."""
    manifest_url = os.getenv("TRADUTOR_IA_UPDATE_MANIFEST_URL", DEFAULT_MANIFEST_URL).strip()
    if not manifest_url:
        return JSONResponse({"state": "not_configured", "can_install": False}, status_code=409)
    try:
        transport = update_transport.UpdateTransport()
        raw = transport.fetch_manifest(manifest_url)
        manifest = update_manifest.verify_manifest(raw, trusted_keys=update_manifest.load_trusted_keys())
        decision = update_manifest.decide_update(BUILD_VERSION, manifest)
        if not decision.should_install:
            return JSONResponse({"state": decision.state, "can_install": False}, status_code=409)
        if manifest.artifact_type != installer_update.INSTALLER_ARTIFACT_TYPE:
            raise installer_update.InstallerUpdateError("update artifact is not a Windows installer")
        token = secrets.token_urlsafe(24)
        with _UPDATE_DOWNLOADS_LOCK:
            _UPDATE_DOWNLOADS[token] = {"status": "starting", "received": 0,
                "expected": manifest.package.size, "version": manifest.version_text,
                "cancel_event": threading.Event()}
        threading.Thread(target=_run_update_download, args=(token, manifest), daemon=True).start()
        return JSONResponse({"state": "downloading", "version": manifest.version_text,
                             "size": manifest.package.size, "download_token": token},
                            headers={"Cache-Control": "no-store"})
    except (update_manifest.UpdateError, update_transport.TransportError) as exc:
        return JSONResponse({"state": "error", "can_install": False,
                             "error": type(exc).__name__}, status_code=502,
                            headers={"Cache-Control": "no-store"})


@app.get("/api/update/download/status")
def api_update_download_status(token: str = "") -> JSONResponse:
    with _UPDATE_DOWNLOADS_LOCK:
        state = dict(_UPDATE_DOWNLOADS.get(token) or {})
    if not state:
        return JSONResponse({"state": "error", "error": "download_not_found"}, status_code=404)
    state.pop("cancel_event", None)
    path = state.pop("path", None)
    if state.get("status") == "ready_to_install" and path:
        handoff = secrets.token_urlsafe(24)
        with _UPDATE_INSTALLER_HANDOFFS_LOCK:
            _UPDATE_INSTALLER_HANDOFFS[handoff] = Path(path)
        state["handoff_token"] = handoff
    return JSONResponse({"state": state.pop("status", "error"), **state}, headers={"Cache-Control": "no-store"})


@app.post("/api/update/download/cancel")
def api_update_download_cancel(token: str = "") -> JSONResponse:
    with _UPDATE_DOWNLOADS_LOCK:
        state = _UPDATE_DOWNLOADS.get(token)
        if not state:
            return JSONResponse({"state": "error", "error": "download_not_found"}, status_code=404)
        state["cancel_event"].set()
    return JSONResponse({"state": "cancelling"}, headers={"Cache-Control": "no-store"})


@app.post("/api/update/install")
def api_update_install(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """Start only the installer previously staged by ``/api/update/download``."""
    token = payload.get("handoff_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise HTTPException(status_code=400, detail={"code": "invalid_handoff_token"})
    with _UPDATE_INSTALLER_HANDOFFS_LOCK:
        path = _UPDATE_INSTALLER_HANDOFFS.pop(token, None)
    if path is None:
        raise HTTPException(status_code=404, detail={"code": "handoff_not_found"})
    try:
        _append_diagnostic_log("app_current.jsonl", "UPDATE_INSTALLER_SPAWN_ATTEMPT", path=str(path))
        process = installer_update.spawn_verified_installer(path)
    except (OSError, update_manifest.UpdateError) as exc:
        _append_diagnostic_log("app_current.jsonl", "UPDATE_INSTALLER_SPAWN_ERROR", error=type(exc).__name__)
        return JSONResponse({"state": "error", "error": type(exc).__name__}, status_code=502,
                            headers={"Cache-Control": "no-store"})
    _append_diagnostic_log("app_current.jsonl", "UPDATE_INSTALLER_SPAWN_OK", pid=process.pid)
    time.sleep(0.25)
    if process.poll() is not None:
        _append_diagnostic_log("app_current.jsonl", "UPDATE_INSTALLER_EARLY_EXIT", exit_code=process.returncode)
        return JSONResponse({"state": "error", "error": "installer_early_exit"}, status_code=502,
                            headers={"Cache-Control": "no-store"})
    return JSONResponse({"state": "installing", "pid": process.pid, "shutdown_required": True},
                        headers={"Cache-Control": "no-store"})


@app.post("/api/ui/page-revision/regions")
def api_page_revision_regions(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    _owned_ui_job(request, str(payload.get("job_id") or ""))
    try:
        return BRIDGE.list_page_revision_regions(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""), int(payload.get("page") or 0))
    except ValueError as exc:
        raise _page_revision_error(exc) from exc


@app.post("/api/ui/page-revision/forgotten-text")
def api_page_revision_forgotten(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    _owned_ui_job(request, str(payload.get("job_id") or ""))
    try:
        return BRIDGE.page_revision_forgotten_text(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""), int(payload.get("page") or 0))
    except ValueError as exc:
        raise _page_revision_error(exc) from exc


@app.post("/api/ui/page-revision/start")
def api_page_revision_start(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    _owned_ui_job(request, str(payload.get("job_id") or ""), mutate=True)
    region_ids = payload.get("region_ids")
    region_ids = [str(r) for r in region_ids] if isinstance(region_ids, list) else None
    try:
        return BRIDGE.start_page_revision(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            int(payload.get("page") or 0), region_ids=region_ids)
    except ValueError as exc:
        raise _page_revision_error(exc) from exc


@app.post("/api/ui/page-revision/status")
def api_page_revision_status(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    _owned_ui_job(request, str(payload.get("job_id") or ""))
    try:
        return BRIDGE.page_revision_status(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            str(payload.get("page_revision_id") or ""))
    except ValueError as exc:
        raise _page_revision_error(exc) from exc


@app.post("/api/ui/page-revision/cancel")
def api_page_revision_cancel(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    _owned_ui_job(request, str(payload.get("job_id") or ""), mutate=True)
    try:
        return BRIDGE.cancel_page_revision(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            str(payload.get("page_revision_id") or ""))
    except ValueError as exc:
        raise _page_revision_error(exc) from exc


@app.post("/api/ui/page-revision/resume")
def api_page_revision_resume(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    _owned_ui_job(request, str(payload.get("job_id") or ""), mutate=True)
    try:
        return BRIDGE.resume_page_revision(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            str(payload.get("page_revision_id") or ""))
    except ValueError as exc:
        raise _page_revision_error(exc) from exc


@app.post("/api/ui/page-revision/decision")
def api_page_revision_decision(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    _owned_ui_job(request, str(payload.get("job_id") or ""), mutate=True)
    try:
        return BRIDGE.decide_page_revision(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            str(payload.get("page_revision_id") or ""), str(payload.get("outcome") or ""))
    except ValueError as exc:
        raise _page_revision_error(exc) from exc


@app.post("/api/ui/page-revision/manual-region")
def api_page_revision_manual_region(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    _owned_ui_job(request, str(payload.get("job_id") or ""), mutate=True)
    box = payload.get("box")
    if not isinstance(box, list):
        raise HTTPException(status_code=400, detail={"code": "invalid_box", "message": "Caixa inválida."})
    try:
        return BRIDGE.add_page_revision_manual_region(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            str(payload.get("page_revision_id") or ""), box=box,
            source_text=str(payload.get("source_text") or ""),
            region_type=str(payload.get("region_type") or "speech"))
    except ValueError as exc:
        raise _page_revision_error(exc) from exc


@app.get("/api/ui/page-revision/{job_id}/{page_revision_id}/draft")
def api_page_revision_draft(
    request: Request, job_id: str, page_revision_id: str, run_id: str = ""
) -> FileResponse:
    # Served to an <img> tag, which cannot carry the bearer token; like the
    # reviewed-page image endpoint, safety comes from the bridge validating the
    # job/run/page-revision linkage and confining the path to the output dir.
    _owned_ui_job(request, job_id)
    try:
        path = BRIDGE.page_revision_draft_page(job_id, run_id, page_revision_id)
    except ValueError as exc:
        raise _page_revision_error(exc) from exc
    if path is None:
        raise HTTPException(status_code=404, detail="Prévia da página não encontrada.")
    return FileResponse(path)


@app.get("/api/ui/diagnostics")
def api_diagnostics(request: Request) -> dict[str, Any]:
    """Developer-only runtime facts: which build is serving, and since when."""
    _ui_principal(request)
    import subprocess as _sp
    head = ""
    try:
        head = _sp.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT), capture_output=True,
                       text=True, timeout=5, **hidden_console_options()).stdout.strip()
    except Exception:  # noqa: BLE001 - diagnostics must never break the UI
        head = ""
    # healthy_worker is the store's real probe. The previous name did not exist,
    # so hasattr always failed and diagnostics reported the worker offline even
    # while it was serving.
    try:
        worker = BRIDGE.store.healthy_worker()
    except Exception:  # noqa: BLE001 - diagnostics must never break the UI
        worker = None
    return {
        "git_head": head,
        "pid": os.getpid(),
        "server_started_at": _SERVER_STARTED_AT,
        "worker_online": bool(worker),
        "worker_pid": (worker or {}).get("pid") if isinstance(worker, dict) else None,
        "worker_id": (worker or {}).get("worker_id") if isinstance(worker, dict) else None,
        "taxonomy_version": region_taxonomy.TAXONOMY_VERSION,
        "review_schema_version": REVIEW_SCHEMA_VERSION,
        # Schema versions of the triage layers, so a stale UI is detectable
        # instead of silently rendering a payload it does not understand.
        "gate_version": linguistic_triage.GATE_VERSION,
        "ocr_plausibility_version": linguistic_triage.OCR_PLAUSIBILITY_VERSION,
        "audit_decisions": list(audit_decisions.DECISIONS),
    }


# A refusal has to say what is blocking it, otherwise the operator cannot act.
_AUDIT_ERROR_MESSAGES = {
    "blocked_pending_editorial_decisions": (
        "Ainda há regiões aguardando decisão editorial.",
        "Abra DECISÕES EDITORIAIS PENDENTES e decida cada região antes de autorizar."),
    "empty_provider_set": (
        "Nenhuma região precisa de chamada ao provider.",
        "Não há o que autorizar: o conjunto mínimo está vazio."),
    "ambiguous_regions_need_individual_review": (
        "A seleção contém leituras ambíguas.",
        "Em massa só é possível confirmar evidência inequívoca. "
        "Decida cada leitura ambígua individualmente, olhando o recorte."),
    "font_choice_base_page_unavailable": (
        "A página base desta região não foi encontrada.",
        "Abra o capítulo revisado novamente e confirme que o artefato local ainda existe."),
    "font_candidate_not_found": (
        "A opção de tipografia não está mais disponível.",
        "Peça outras opções e escolha novamente."),
    "font_choice_region_geometry_unavailable": (
        "A região não possui geometria suficiente para comparar tipografia.",
        "Revise a região e confirme a caixa antes de escolher a fonte."),
    "mask_region_geometry_unavailable": (
        "A região não possui geometria suficiente para editar a máscara.",
        "Mantenha a reconstrução bloqueada até haver uma caixa segura."),
    "mask_base_page_unavailable": (
        "A página base desta região não foi encontrada.",
        "Abra o capítulo revisado novamente e confirme que o artefato local ainda existe."),
    "mask_empty": (
        "A máscara não possui área de texto.",
        "Inclua somente os pixels do texto antes de confirmar."),
    "mask_area_excessive": (
        "A máscara cobre uma área grande demais.",
        "Reduza a seleção para proteger a arte antes da reconstrução local."),
    "mask_protected_overlap": (
        "A máscara inclui pixels marcados como protegidos.",
        "Remova as linhas protegidas da área de texto antes de confirmar."),
    "mask_uncertain_pixels_unresolved": (
        "Ainda há pixels incertos na máscara.",
        "Resolva as áreas incertas antes de confirmar."),
    "mask_segmentation_hash_mismatch": (
        "A segmentação base mudou desde que a máscara foi editada.",
        "Restaure a segmentação automática e revise novamente."),
    "mask_source_hash_mismatch": (
        "O texto-fonte mudou desde que a máscara foi editada.",
        "Reabra a região e revise a máscara novamente."),
    "mask_combined_layer_unavailable": (
        "A segmentação não gerou uma máscara combinada confiável.",
        "Mantenha a reconstrução bloqueada e revise a região manualmente."),
}


def _audit_error(exc: ValueError) -> HTTPException:
    message, action = _AUDIT_ERROR_MESSAGES.get(str(exc), (
        "Não foi possível concluir a ação da auditoria linguística.",
        "Confira o capítulo, a revisão e a decisão, e tente novamente."))
    return HTTPException(status_code=400, detail={
        "code": str(exc), "message": message, "action": action,
    })


@app.post("/api/ui/audit/review")
def api_audit_review(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    principal = _owned_ui_job(request, str(payload.get("job_id") or ""))
    try:
        return BRIDGE.linguistic_audit_review(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            user_id=principal.user_id)
    except ValueError as exc:
        raise _audit_error(exc) from exc


@app.post("/api/ui/audit/decision")
def api_audit_decision(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    principal = _owned_ui_job(
        request, str(payload.get("job_id") or ""), mutate=True)
    try:
        return BRIDGE.record_audit_decision(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            region_id=str(payload.get("region_id") or ""), decision=str(payload.get("decision") or ""),
            user_id=principal.user_id, reason=str(payload.get("reason") or ""),
            notes=str(payload.get("notes") or ""))
    except ValueError as exc:
        raise _audit_error(exc) from exc


@app.post("/api/ui/audit/triage")
def api_audit_triage(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    principal = _owned_ui_job(request, str(payload.get("job_id") or ""))
    try:
        return BRIDGE.linguistic_triage_queue(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            user_id=principal.user_id)
    except ValueError as exc:
        raise _audit_error(exc) from exc


@app.post("/api/ui/audit/decision/bulk")
def api_audit_decision_bulk(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    principal = _owned_ui_job(
        request, str(payload.get("job_id") or ""), mutate=True)
    regions = payload.get("region_ids")
    if not isinstance(regions, list):
        raise HTTPException(status_code=400, detail={"code": "no_regions_selected",
                                                     "message": "Selecione ao menos uma região."})
    try:
        return BRIDGE.bulk_audit_decisions(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            region_ids=[str(r) for r in regions], decision=str(payload.get("decision") or ""),
            user_id=principal.user_id, reason=str(payload.get("reason") or ""),
            source_audit_hash=str(payload.get("source_audit_hash") or ""))
    except ValueError as exc:
        raise _audit_error(exc) from exc


@app.post("/api/ui/audit/provider-set")
def api_audit_provider_set(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    principal = _owned_ui_job(request, str(payload.get("job_id") or ""))
    try:
        return BRIDGE.minimal_provider_set(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            user_id=principal.user_id)
    except ValueError as exc:
        raise _audit_error(exc) from exc


@app.post("/api/ui/audit/ocr-candidates")
def api_audit_ocr_candidates(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    principal = _owned_ui_job(request, str(payload.get("job_id") or ""))
    try:
        return BRIDGE.ocr_reprocessing_candidates(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            user_id=principal.user_id)
    except ValueError as exc:
        raise _audit_error(exc) from exc


@app.post("/api/ui/audit/ocr-invalid-candidates")
def api_audit_ocr_invalid_candidates(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    # Proposes candidates only. Confirming one is a separate human decision.
    principal = _owned_ui_job(request, str(payload.get("job_id") or ""))
    try:
        return BRIDGE.ocr_invalid_candidates(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            user_id=principal.user_id)
    except ValueError as exc:
        raise _audit_error(exc) from exc


@app.post("/api/ui/human-translation/review")
def api_human_translation_review(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """The executed provider set with this user's own overrides overlaid."""
    principal = _owned_ui_job(request, str(payload.get("job_id") or ""))
    try:
        return BRIDGE.provider_execution_review(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            user_id=principal.user_id, request_id=str(payload.get("request_id") or ""))
    except ValueError as exc:
        raise _audit_error(exc) from exc


@app.post("/api/ui/human-translation/record")
def api_human_translation_record(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    principal = _owned_ui_job(
        request, str(payload.get("job_id") or ""), mutate=True)
    try:
        return BRIDGE.record_human_translation(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            region_id=str(payload.get("region_id") or ""),
            human_candidate=str(payload.get("human_candidate") or ""),
            user_id=principal.user_id, reason=str(payload.get("reason") or ""),
            request_id=str(payload.get("request_id") or ""))
    except ValueError as exc:
        raise _audit_error(exc) from exc


@app.post("/api/ui/human-translation/delete")
def api_human_translation_delete(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    principal = _owned_ui_job(
        request, str(payload.get("job_id") or ""), mutate=True)
    try:
        return BRIDGE.delete_human_translation(
            decision_id=str(payload.get("decision_id") or ""), user_id=principal.user_id)
    except ValueError as exc:
        raise _audit_error(exc) from exc


@app.post("/api/ui/human-translation/refinement")
def api_human_translation_refinement(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """Explicit, owner-scoped linguistic suggestion; never applies a translation."""
    principal = _owned_ui_job(
        request, str(payload.get("job_id") or ""), mutate=True)
    request_fields = {key: value for key, value in payload.items()
                      if key not in {"owner", "provider", "model"}}
    refinement_request = natural_ptbr_refinement.build_request(
        **request_fields, owner=principal.user_id,
        provider=str(payload.get("provider") or "nvidia"),
        model=str(payload.get("model") or "configured"))
    store = natural_ptbr_refinement.RefinementStore(
        BRIDGE.runtime_root / "natural_ptbr_refinement")
    if str(payload.get("operation") or "") == "restore":
        existing = store.get_result(
            refinement_request["request_hash"], owner=principal.user_id)
        selection_store = refinement_selection_decisions.RefinementSelectionStore(
            BRIDGE.store.db_path)
        try:
            selection = selection_store.latest_for_region(
                str(refinement_request.get("job_id") or ""),
                str(refinement_request.get("run_id") or ""),
                str(refinement_request.get("revision_id") or ""),
                str(refinement_request.get("region_id") or ""),
                owner=principal.user_id)
        finally:
            selection_store.close()
        return {"ok": True, "refinement": existing, "selection": selection,
                "restored": bool(existing)}
    if payload.get("authorized") is not True:
        raise HTTPException(
            status_code=403, detail="refinement_explicit_authorization_required")
    translator = TranslatorNvidiaBatch(operation="natural_ptbr_refinement")
    service = natural_ptbr_refinement.RefinementService(
        natural_ptbr_refinement.NvidiaRefinementProvider(translator), store=store)
    return {"ok": True, "refinement": service.refine(
        refinement_request, authorized=True)}


@app.post("/api/ui/human-translation/refinement/decision")
def api_human_translation_refinement_decision(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    principal = _owned_ui_job(
        request, str(payload.get("job_id") or ""), mutate=True)
    result = dict(payload.get("result") or {})
    request_fields = dict(result.get("request") or {})
    option = str(payload.get("option") or "")
    if option not in {"natural", "keep_current"}:
        raise HTTPException(status_code=422, detail="refinement_selection_invalid")
    action = "keep_current" if option == "keep_current" else "select_option"
    intent = {
        **{key: request_fields.get(key) for key in (
            "job_id", "run_id", "revision_id", "page_id", "region_id")},
        "owner": principal.user_id,
        "source_hash": __import__("hashlib").sha256(
            str(request_fields.get("source_text") or "").encode("utf-8")).hexdigest(),
        "current_translation_before": str(
            request_fields.get("current_translation") or ""),
        "previous_decision_id": str(payload.get("previous_decision_id") or ""),
        "selected_action": action,
        "selected_option": "current" if action == "keep_current" else "natural",
        "result": result,
        "reviewer": principal.user_id,
        "authorization": str(payload.get("authorization") or ""),
        "authorization_scope": "interactive_ui",
        "reason": str(payload.get("reason") or ""),
    }
    if intent["authorization"] != "delegated_by_user":
        raise HTTPException(
            status_code=403, detail="refinement_selection_authorization_required")
    plan_hash = refinement_selection_decisions._hash({
        "operation": "confirm_refinement_selection",
        "owner": principal.user_id,
        "request_hash": request_fields.get("request_hash"),
        "selected_action": action,
    })
    selection_store = refinement_selection_decisions.RefinementSelectionStore(
        BRIDGE.store.db_path)
    try:
        decision = selection_store.confirm_batch([intent], plan_hash=plan_hash)[0]
    except ValueError as exc:
        raise _audit_error(exc) from exc
    finally:
        selection_store.close()
    return {"ok": True, "decision": decision}


@app.post("/api/ui/human-translation/font-candidates")
def api_human_translation_font_candidates(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    principal = _owned_ui_job(request, str(payload.get("job_id") or ""))
    try:
        return BRIDGE.human_typography_candidates(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            region_id=str(payload.get("region_id") or ""), user_id=principal.user_id)
    except ValueError as exc:
        raise _audit_error(exc) from exc


@app.post("/api/ui/human-translation/font-choice")
def api_human_translation_font_choice(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    principal = _owned_ui_job(
        request, str(payload.get("job_id") or ""), mutate=True)
    try:
        return BRIDGE.choose_human_typography(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            region_id=str(payload.get("region_id") or ""),
            candidate_id=str(payload.get("candidate_id") or ""), user_id=principal.user_id)
    except ValueError as exc:
        raise _audit_error(exc) from exc


@app.get("/api/ui/human-translation/font-candidate-preview")
def api_human_translation_font_candidate_preview(
    request: Request, job_id: str = "", asset: str = ""
) -> FileResponse:
    _owned_ui_job(request, job_id)
    try:
        path = BRIDGE.human_typography_candidate_asset(asset)
    except ValueError as exc:
        raise _audit_error(exc) from exc
    return FileResponse(path, media_type="image/png")


@app.post("/api/ui/human-translation/draft")
def api_human_translation_draft(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Render one region's human line into its own draft. Never calls a provider."""
    principal = _owned_ui_job(
        request, str(payload.get("job_id") or ""), mutate=True)
    try:
        return BRIDGE.create_human_preview_draft(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            region_id=str(payload.get("region_id") or ""), user_id=principal.user_id,
            request_id=str(payload.get("request_id") or ""),
            font_choice_decision_id=str(payload.get("font_choice_decision_id") or ""))
    except ValueError as exc:
        raise _audit_error(exc) from exc


@app.post("/api/ui/human-translation/gates")
def api_human_translation_gates(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    principal = _owned_ui_job(request, str(payload.get("job_id") or ""))
    try:
        return BRIDGE.human_preview_gates(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            region_id=str(payload.get("region_id") or ""), user_id=principal.user_id)
    except ValueError as exc:
        raise _audit_error(exc) from exc


@app.post("/api/ui/human-translation/visual-review")
def api_human_translation_visual_review(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    principal = _owned_ui_job(
        request, str(payload.get("job_id") or ""), mutate=True)
    try:
        return BRIDGE.record_visual_review_decision(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            region_id=str(payload.get("region_id") or ""),
            page_revision_id=str(payload.get("page_revision_id") or ""),
            decision=str(payload.get("decision") or ""),
            user_id=principal.user_id,
            reason_codes=[str(v) for v in (payload.get("reason_codes") or [])],
            visual_evidence=payload.get("visual_evidence") if isinstance(payload.get("visual_evidence"), dict) else {},
        )
    except ValueError as exc:
        raise _audit_error(exc) from exc


@app.get("/api/ui/human-previews/pending")
def api_pending_human_previews(request: Request) -> dict[str, Any]:
    principal = _ui_principal(request)
    try:
        return BRIDGE.pending_human_previews(user_id=principal.user_id)
    except ValueError as exc:
        raise _audit_error(exc) from exc


@app.get("/api/ui/human-translation/preview-crop")
def api_human_translation_preview_crop(request: Request, job_id: str = "", run_id: str = "",
                                       region_id: str = "", kind: str = "draft") -> FileResponse:
    """The region as it is now, or as the draft would render it."""
    principal = _owned_ui_job(request, job_id)
    try:
        path = BRIDGE.human_preview_crop(job_id, run_id, region_id=region_id, kind=kind,
                                         user_id=principal.user_id)
    except ValueError as exc:
        raise _audit_error(exc) from exc
    return FileResponse(path, media_type="image/png")


@app.post("/api/ui/human-mask/editor-state")
def api_human_mask_editor_state(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    principal = _owned_ui_job(request, str(payload.get("job_id") or ""))
    try:
        return BRIDGE.human_mask_editor_state(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            region_id=str(payload.get("region_id") or ""), user_id=principal.user_id)
    except ValueError as exc:
        raise _audit_error(exc) from exc


@app.post("/api/ui/human-mask/save")
def api_human_mask_save(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    principal = _owned_ui_job(
        request, str(payload.get("job_id") or ""), mutate=True)
    try:
        return BRIDGE.save_human_mask_draft(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            region_id=str(payload.get("region_id") or ""), user_id=principal.user_id,
            payload=payload, confirm=False)
    except ValueError as exc:
        raise _audit_error(exc) from exc


@app.post("/api/ui/human-mask/confirm")
def api_human_mask_confirm(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    principal = _owned_ui_job(
        request, str(payload.get("job_id") or ""), mutate=True)
    try:
        return BRIDGE.save_human_mask_draft(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            region_id=str(payload.get("region_id") or ""), user_id=principal.user_id,
            payload=payload, confirm=True)
    except ValueError as exc:
        raise _audit_error(exc) from exc


@app.get("/api/ui/human-mask/asset")
def api_human_mask_asset(
    request: Request, job_id: str = "", asset: str = ""
) -> FileResponse:
    _owned_ui_job(request, job_id)
    try:
        path = BRIDGE.human_mask_editor_asset(asset)
    except ValueError as exc:
        raise _audit_error(exc) from exc
    return FileResponse(path, media_type="image/png")


@app.get("/api/ui/audit/region-crop")
def api_audit_region_crop(request: Request, job_id: str = "", run_id: str = "",
                          region_id: str = "") -> FileResponse:
    """Serve the region's own pixels so a verdict is given on the picture too."""
    principal = _owned_ui_job(request, job_id)
    try:
        path = BRIDGE.audit_region_crop(job_id, run_id, region_id=region_id,
                                        user_id=principal.user_id)
    except ValueError as exc:
        raise _audit_error(exc) from exc
    return FileResponse(path, media_type="image/png")


@app.post("/api/ui/audit/editorial-pending")
def api_audit_editorial_pending(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    principal = _owned_ui_job(request, str(payload.get("job_id") or ""))
    try:
        return BRIDGE.pending_editorial_decisions(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            user_id=principal.user_id)
    except ValueError as exc:
        raise _audit_error(exc) from exc


@app.post("/api/ui/audit/provider-authorization")
def api_audit_provider_authorization(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    # Records a pending request only. It never reads a credential and never
    # contacts the provider; running it stays a separate, explicit step.
    principal = _owned_ui_job(
        request, str(payload.get("job_id") or ""), mutate=True)
    try:
        return BRIDGE.request_provider_authorization(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            user_id=principal.user_id, confirm=bool(payload.get("confirm")))
    except ValueError as exc:
        raise _audit_error(exc) from exc


@app.post("/api/ui/audit/provider-authorization/cancel")
def api_audit_provider_authorization_cancel(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    principal = _owned_ui_job(
        request, str(payload.get("job_id") or ""), mutate=True)
    try:
        return BRIDGE.cancel_provider_authorization(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            request_id=str(payload.get("request_id") or ""), user_id=principal.user_id)
    except ValueError as exc:
        raise _audit_error(exc) from exc


@app.post("/api/ui/audit/decision/delete")
def api_audit_decision_delete(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    principal = _owned_ui_job(
        request, str(payload.get("job_id") or ""), mutate=True)
    try:
        return BRIDGE.delete_audit_decision(
            str(payload.get("job_id") or ""), str(payload.get("run_id") or ""),
            decision_id=str(payload.get("decision_id") or ""), user_id=principal.user_id)
    except ValueError as exc:
        raise _audit_error(exc) from exc


@app.get("/api/ui/quality-review/{job_id}")
def api_quality_review(request: Request, job_id: str) -> dict[str, Any]:
    _owned_ui_job(request, job_id)
    review = BRIDGE.quality_review(job_id)
    if review is None:
        raise HTTPException(status_code=404, detail={
            "code": "quality_review_not_available",
            "message": "Não há uma revisão de qualidade disponível.",
        })
    return review


@app.get("/api/ui/quality-review/{job_id}/page/{page_number}")
def api_quality_review_page(
    request: Request, job_id: str, page_number: int, revision: str = ""
) -> FileResponse:
    _owned_ui_job(request, job_id)
    path = BRIDGE.quality_review_page(job_id, page_number, revision=revision)
    if path is None:
        raise HTTPException(status_code=404, detail="Página de revisão não encontrada.")
    return FileResponse(path)


@app.post("/api/ui/quality-review/action")
def api_quality_review_action(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    try:
        _owned_ui_job(request, str(payload.get("job_id") or ""), mutate=True)
        return BRIDGE.quality_review_action(
            str(payload.get("job_id") or ""),
            str(payload.get("item_key") or ""),
            str(payload.get("action") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "code": str(exc),
            "message": "Não foi possível atualizar este item de revisão.",
            "action": "Tente novamente.",
        }) from exc


@app.post("/api/ui/quality-review/edit")
def api_quality_review_edit(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    principal = _owned_ui_job(
        request, str(payload.get("job_id") or ""), mutate=True)
    try:
        return BRIDGE.quality_review_edit(
            str(payload.get("job_id") or ""),
            str(payload.get("item_key") or ""),
            expected_version=int(payload.get("expected_version") or 0),
            action=str(payload.get("action") or ""),
            translation=str(payload.get("translation") or ""),
            reason=str(payload.get("reason") or ""),
            actor_id=principal.user_id,
        )
    except ValueError as exc:
        status_code = 409 if str(exc) == "review_version_conflict" else 422
        raise HTTPException(status_code=status_code, detail={
            "code": str(exc),
            "message": "Não foi possível salvar esta revisão.",
            "action": "Atualize os dados e tente novamente.",
        }) from exc


@app.post("/api/ui/quality-review/bulk-action")
def api_quality_review_bulk_action(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    try:
        _owned_ui_job(request, str(payload.get("job_id") or ""), mutate=True)
        raw_keys = payload.get("item_keys") or []
        item_keys = [str(item) for item in raw_keys] if isinstance(raw_keys, list) else []
        raw_restore = payload.get("restore_actions") or {}
        restore_actions = {str(key): str(value) for key, value in raw_restore.items()} if isinstance(raw_restore, dict) else {}
        return BRIDGE.quality_review_bulk_action(
            str(payload.get("job_id") or ""),
            item_keys,
            str(payload.get("action") or ""),
            risk_filter=str(payload.get("risk_filter") or ""),
            undo=bool(payload.get("undo") or False),
            restore_actions=restore_actions,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "code": str(exc),
            "message": "Não foi possível aplicar a ação em massa.",
            "action": "Confira os itens selecionados e tente novamente.",
        }) from exc


@app.post("/api/ui/quality-review/global-review")
def api_quality_review_global_review(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    try:
        _owned_ui_job(request, str(payload.get("job_id") or ""), mutate=True)
        return BRIDGE.translation_global_review(str(payload.get("job_id") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "code": str(exc),
            "message": "Não foi possível gerar a revisão global.",
            "action": "Confira se o job possui relatório de qualidade persistido.",
        }) from exc


@app.post("/api/ui/quality-review/confirm")
def api_quality_review_confirm(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    try:
        _owned_ui_job(request, str(payload.get("job_id") or ""), mutate=True)
        return BRIDGE.confirm_quality_review(str(payload.get("job_id") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "code": str(exc),
            "message": "Ainda há itens que precisam ser revisados.",
            "action": "Revise cada item ou mantenha o original antes de confirmar.",
        }) from exc


@app.post("/api/ui/retry")
def api_retry(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    try:
        job_id = str(payload.get("job_id") or "")
        principal = _owned_ui_job(request, job_id, mutate=True)
        return BRIDGE.retry_job_for_owner(principal.owner_id, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "code": str(exc),
            "message": "Esta execução não pode ser repetida automaticamente.",
            "action": "Envie uma nova tradução para criar uma tentativa separada.",
        }) from exc


@app.post("/api/ui/source/confirm")
def api_source_confirm(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    try:
        job_id = str(payload.get("job_id") or "")
        _owned_ui_job(request, job_id, mutate=True)
        return BRIDGE.confirm_source_pages(
            job_id,
            payload.get("candidate_ids") if isinstance(payload.get("candidate_ids"), list) else [],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "code": str(exc), "stage": "revisao_da_fonte",
            "message": "A seleção de páginas não é válida.",
            "action": "Selecione ao menos uma página encontrada e tente novamente.",
        }) from exc


@app.post("/api/ui/source/retry")
async def api_source_retry(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    try:
        job_id = str(payload.get("job_id") or "")
        _owned_ui_job(request, job_id, mutate=True)
        return await BRIDGE.retry_source_review(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "code": str(exc), "stage": "revisao_da_fonte",
            "message": "Esta revisão de fonte não pode mais ser repetida.",
            "action": "Atualize a tela e escolha a revisão atualmente exibida.",
        }) from exc


@app.post("/api/ui/source/authorization")
def api_source_authorization(
    request: Request,
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    try:
        principal = _ui_principal(request, mutate=True)
        operations = payload.get("allowed_operations")
        analysis_result_id = str(payload.get("analysis_result_id") or "")
        if analysis_result_id and not str(payload.get("job_id") or ""):
            return BRIDGE.authorize_standalone_source_analysis(
                analysis_result_id,
                principal=principal,
                rights_basis=str(payload.get("rights_basis") or ""),
                allowed_operations=operations if isinstance(operations, list) else [],
            )
        return BRIDGE.authorize_source_download(
            str(payload.get("job_id") or ""),
            principal=principal,
            rights_basis=str(payload.get("rights_basis") or ""),
            allowed_operations=operations if isinstance(operations, list) else [],
        )
    except AuthenticationRequired as exc:
        raise HTTPException(status_code=401, detail="authentication_required") from exc
    except (AuthorizationDenied, CsrfRejected) as exc:
        raise HTTPException(status_code=403, detail="authorization_denied") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={
            "code": str(exc),
            "message": "É necessária uma autorização de conteúdo antes de iniciar o download.",
        }) from exc


@app.post("/api/ui/source/continue")
def api_source_continue(
    request: Request,
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    try:
        return BRIDGE.continue_authorized_download(
            str(payload.get("job_id") or ""),
            principal=_ui_principal(request, mutate=True),
        )
    except AuthenticationRequired as exc:
        raise HTTPException(status_code=401, detail="authentication_required") from exc
    except (AuthorizationDenied, CsrfRejected) as exc:
        raise HTTPException(status_code=403, detail="authorization_denied") from exc
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=422, detail={
            "code": str(exc),
            "message": "É necessária uma autorização de conteúdo antes de iniciar o download.",
        }) from exc


@app.get("/api/ui/source-policy")
def api_source_policy(request: Request) -> dict[str, Any]:
    _ui_principal(request)
    return {"ok": True, "policy": BRIDGE.settings()["workspace_source_policy"]}


@app.post("/api/ui/source-policy")
def api_source_policy_update(
    request: Request,
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    _ui_principal(request, mutate=True)
    # `1` and `0` compare equal to `True`/`False`, so a membership test alone would let a
    # numeric payload decide an authorization. The intent must arrive as a real boolean.
    if not isinstance(payload.get("active"), bool):
        raise HTTPException(status_code=422, detail={
            "code": "invalid_workspace_policy_state",
            "message": "Informe explicitamente se a política deve ficar ativa.",
        })
    try:
        return BRIDGE.set_workspace_source_policy(active=payload["active"])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={
            "code": str(exc),
            "message": "A política de fontes autorizadas já está desativada.",
        }) from exc


@app.post("/api/ui/queue/add")
def api_queue_add(
    request: Request,
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    return {
        "ok": True,
        "item": _api_call(
            BRIDGE.add_queue_item,
            payload,
            principal=_ui_principal(request, mutate=True),
        ),
    }


@app.post("/api/ui/queue/remove")
def api_queue_remove(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    principal = _ui_principal(request, mutate=True)
    BRIDGE.remove_queue_item_for_owner(
        principal.owner_id, str(payload.get("id") or ""))
    return {"ok": True}


@app.post("/api/ui/queue/clear")
def api_queue_clear(request: Request) -> dict[str, Any]:
    principal = _ui_principal(request, mutate=True)
    BRIDGE.clear_queue_for_owner(principal.owner_id)
    return {"ok": True}


@app.post("/api/ui/queue/start")
async def api_queue_start(request: Request) -> dict[str, Any]:
    try:
        principal = _ui_principal(request, mutate=True)
        return await BRIDGE.start_queue_for_owner(principal.owner_id)
    except ValueError as exc:
        code = str(exc) if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,79}", str(exc)) else "operation_failed"
        raise HTTPException(status_code=400, detail=code) from exc


@app.post("/api/ui/resume")
def api_resume(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    job_id = str(payload.get("job_id") or payload.get("id") or "")
    principal = _owned_ui_job(request, job_id, mutate=True)
    return _api_call(
        BRIDGE.resume,
        job_id,
        principal=principal,
        license_access_token=_license_bearer_token(request),
    )


@app.post("/api/ui/resume/dismiss")
def api_resume_dismiss(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """Dismiss an interrupted card while preserving the attempt in local history."""
    job_id = str(payload.get("job_id") or payload.get("id") or "")
    principal = _owned_ui_job(request, job_id, mutate=True)
    try:
        return BRIDGE.dismiss_resumable_job_for_owner(principal.owner_id, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="job_not_dismissible") from exc


@app.post("/api/ui/profile")
def api_profile(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    principal = _ui_principal(request, mutate=True)
    try:
        if _SOCIAL_STATUS.get("provider") == "supabase" and _SOCIAL_STATUS.get("available"):
            repo = globals().get("_SOCIAL_REPO")
            if repo is None:
                raise RuntimeError("remote_profile_unavailable")
            token = _license_bearer_token(request)
            if not token:
                raise AuthenticationRequired()
            repo.update_my_profile(token, principal.user_id, _remote_profile_payload(payload))
            profile = _remote_profile_for_principal(principal, request)
        else:
            profile = BRIDGE.save_profile(payload, user_id=principal.user_id)
            _sync_public_profile(principal)
    except ValueError as exc:
        if str(exc) == "display_name_taken":
            raise HTTPException(status_code=409, detail={"code": "display_name_taken", "message": "Este nome de exibição já está em uso."}) from exc
        code = str(exc) if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,79}", str(exc)) else "profile_update_failed"
        raise HTTPException(status_code=400, detail=code) from exc
    except AuthenticationRequired as exc:
        raise HTTPException(status_code=401, detail="authentication_required") from exc
    except Exception as exc:
        social_status = int(getattr(exc, "status", 0) or 0)
        social_code = str(getattr(exc, "code", "") or "")
        if 400 <= social_status < 500:
            raise HTTPException(status_code=social_status, detail=social_code or "profile_update_failed") from exc
        raise HTTPException(status_code=503, detail="profile_remote_unavailable") from exc
    return {"ok": True, "profile": profile}


@app.post("/api/ui/profile/media/{kind}")
async def api_profile_media_upload(
    kind: str,
    request: Request,
    filename: str = Query(..., min_length=1, max_length=180),
    content_type: str = Query(..., min_length=3, max_length=80),
) -> dict[str, Any]:
    principal = _ui_principal(request, mutate=True)
    if kind not in _PROFILE_MEDIA_MAX_BYTES:
        raise HTTPException(status_code=422, detail="invalid_profile_media_kind")
    max_bytes = _PROFILE_MEDIA_MAX_BYTES[kind]
    content_length = request.headers.get("content-length")
    try:
        if content_length is not None and int(content_length) > max_bytes:
            raise HTTPException(status_code=413, detail="profile_media_too_large")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid_content_length") from exc
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="profile_media_too_large")
        chunks.append(chunk)
    content = b"".join(chunks)
    bearer = _license_bearer_token(request)
    remote_base = str(os.getenv("SUPABASE_URL", "") or "").rstrip("/")
    if bearer and remote_base:
        endpoint = f"{remote_base}/functions/v1/profile-media?kind={urllib_parse.quote(kind)}"
        upstream = urllib_request.Request(endpoint, data=content, method="POST", headers={"Authorization": f"Bearer {bearer}", "Content-Type": content_type})
        try:
            with urllib_request.urlopen(upstream, timeout=20, context=_https_context()) as raw:  # noqa: S310 - configured Supabase origin
                payload = json.loads(raw.read(64 * 1024).decode("utf-8"))
                _sync_public_profile(principal)
                return {"ok": True, "profile": {**BRIDGE.profile_for_user(principal.user_id), **payload}}
        except urllib_error.HTTPError as exc:
            try:
                remote_error = json.loads(exc.read(16 * 1024).decode("utf-8"))
                remote_detail = str(remote_error.get("error") or "profile_media_upload_unavailable")
                remote_stage = str(remote_error.get("stage") or "")
                if remote_stage: remote_detail = f"{remote_detail}:{remote_stage}"
            except Exception:
                remote_detail = "profile_media_upload_unavailable"
            if exc.code in {400, 413}: raise HTTPException(status_code=exc.code, detail="invalid_profile_media") from exc
            if exc.code in {401, 403}: raise HTTPException(status_code=401, detail="authentication_required") from exc
            raise HTTPException(status_code=502, detail=remote_detail) from exc
        except (urllib_error.URLError, TimeoutError, ValueError) as exc:
            raise HTTPException(status_code=502, detail="profile_media_upload_unavailable") from exc
    profile = _api_call(
        BRIDGE.save_profile_media,
        kind,
        filename=filename,
        content_type=content_type,
        content=content,
        user_id=principal.user_id,
    )
    _sync_public_profile(principal)
    return {
        "ok": True,
        "profile": profile,
    }


@app.post("/api/ui/profile/media-trace")
async def api_profile_media_trace(request: Request) -> dict[str, bool]:
    """Store a bounded, sanitized diagnostic trace for the current UI session."""
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid_trace")
    _append_diagnostic_log(
        "app_current.jsonl", "UI_TRACE_HTTP_REQUEST_RECEIVED",
        event_name=str(payload.get("event") or "")[:80],
        method=str(getattr(request, "method", "POST")),
        content_type=str(getattr(request, "headers", {}).get("content-type", "application/json") if getattr(request, "headers", None) is not None else "application/json")[:80],
        has_json_body=True,
        top_level_keys=','.join(sorted(str(key)[:40] for key in payload.keys())[:30]),
    )
    allowed = {"trace_id", "step", "kind", "status", "route", "reason", "mime", "size", "auth_present", "file_present", "success", "avatar_ref_present", "banner_ref_present"}
    safe = {k: payload[k] for k in allowed if k in payload and isinstance(payload[k], (str, int, bool, float))}
    safe["at"] = int(time.time() * 1000)
    # Trace output is mutable runtime state.  In a frozen install ROOT/_internal is
    # read-only and must contain assets only; keep diagnostics under the per-user
    # runtime root instead.
    trace_dir = DIAGNOSTICS_ROOT
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_path = trace_dir / "profile_media_current.jsonl"
    with trace_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe, ensure_ascii=False, separators=(",", ":")) + "\n")
    return {"ok": True}


@app.post("/api/ui/source-trace")
async def api_source_trace(request: Request) -> dict[str, bool]:
    """Persist a sanitized source-action trace, starting at the click."""
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid_trace")
    trace_id = str(payload.get("trace_id") or "")
    if not trace_id or len(trace_id) > 80:
        raise HTTPException(status_code=400, detail="invalid_trace_id")
    safe = {"timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "trace_id": trace_id, "event": str(payload.get("event") or "SOURCE_UI_CLICK")[:80]}
    for key in ("status", "reason_code", "stage", "authenticated", "license_ready", "source_type", "policy_present", "policy_status", "all_submitted_sources_authorized", "guard_result", "route", "ready", "duration_ms"):
        value = payload.get(key)
        if isinstance(value, (str, int, bool, float)):
            safe[key] = value
    path = DIAGNOSTICS_ROOT / f"source_{trace_id}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe, ensure_ascii=False, separators=(",", ":")) + "\n")
    return {"ok": True}


@app.post("/api/ui/control-plane-trace")
async def api_control_plane_trace(request: Request) -> dict[str, bool]:
    """Persist a bounded, allow-listed control-plane event from the desktop UI.

    This endpoint is intentionally best-effort: diagnostics must never change the
    authentication/device-registration flow.  Credentials, request bodies and
    cryptographic material are not accepted fields.
    """
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid_trace")
    allowed = {
        "step", "status", "http_status", "reason_code", "error_code", "error_name",
        "user_id", "license_id", "license_status", "device_uuid", "device_id_prefix", "public_key_present",
        "identity_exists", "bridge_available", "authenticated", "attempt", "duration_ms",
        "ok", "license_present", "verified", "challenge_id", "ttl_available",
        "daily", "subscription", "permanent", "reserved", "active_yk", "top_text", "rewards_text", "source",
        "project_ref", "function_slug", "translation_enabled", "maintenance_mode",
        "keys_present", "snapshot_updated", "policy_valid", "top_level_keys",
        "data_keys", "payload_keys", "body_keys", "feature_flags_present",
        "feature_flags_type", "policy_source", "policy_resolved_at", "resolved_at",
        "event_name", "event_target", "target", "window_realm_id", "is_top_window", "pathname",
        "has_detail", "detail_translation_enabled", "detail_source", "detail_resolved_at", "probe",
    }
    event = str(payload.get("event") or "CONTROL_PLANE_EVENT")[:80]
    safe: dict[str, Any] = {"event": event}
    for key in allowed:
        value = payload.get(key)
        if isinstance(value, str):
            safe[key] = value[:120]
        elif isinstance(value, (int, float, bool)):
            safe[key] = value
    _append_diagnostic_log("app_current.jsonl", event, **{k: v for k, v in safe.items() if k != "event"})
    return {"ok": True}


@app.get("/api/ui/profile/media/{kind}")
def api_profile_media(request: Request, kind: str) -> Response:
    route_started = time.perf_counter()
    principal = _ui_principal(request)
    if kind not in {"avatar", "banner"}:
        raise HTTPException(status_code=404, detail="Mídia não encontrada.")
    # Drive-backed media is served by the authenticated Edge Function.  The
    # desktop process forwards only the user's bearer token; Drive credentials
    # never enter the client or the packaged application.
    profile = BRIDGE.profile_for_user(principal.user_id)
    # Remote profile rows are authoritative for Drive-backed media.  The local
    # bridge profile may intentionally contain only presentation metadata after a
    # restart, which previously made this route return a misleading local 404.
    remote_profile: dict[str, Any] = {}
    social_repo = globals().get("_SOCIAL_REPO")
    if (_SOCIAL_STATUS.get("provider") == "supabase"
            and _SOCIAL_STATUS.get("available") and social_repo is not None):
        token = _license_bearer_token(request)
        if token:
            try:
                remote_profile = social_repo.get_my_profile(token, principal.user_id) or {}
            except Exception:
                remote_profile = {}
    stored_ref = str(
        remote_profile.get(f"{kind}_object_key")
        or profile.get(f"{kind}_media_path")
        or profile.get(f"{kind}_object_key")
        or ""
    )
    bearer = _license_bearer_token(request)
    remote_base = str(os.getenv("SUPABASE_URL", "") or "").rstrip("/")
    if stored_ref.startswith("drive:") and bearer and remote_base:
        endpoint = f"{remote_base}/functions/v1/profile-media?kind={urllib_parse.quote(kind)}"
        upstream = urllib_request.Request(endpoint, headers={"Authorization": f"Bearer {bearer}"})
        try:
            host = urllib_parse.urlparse(remote_base).hostname or ""
            _append_diagnostic_log("routes_current.jsonl", "PROFILE_MEDIA_NETWORK", route=f"/api/ui/profile/media/{kind}", remote_scheme="https", remote_host=host, remote_port=443, dns_resolution="started")
            socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            with urllib_request.urlopen(upstream, timeout=15, context=_https_context()) as raw:  # noqa: S310 - configured Supabase origin
                content = raw.read(12 * 1024 * 1024 + 1)
                if len(content) > 12 * 1024 * 1024:
                    raise HTTPException(status_code=502, detail="media_too_large")
                media_type = raw.headers.get_content_type() or profile.get(f"{kind}_media_type") or "application/octet-stream"
                _append_diagnostic_log("routes_current.jsonl", "PROFILE_MEDIA_ROUTE", route=f"/api/ui/profile/media/{kind}", method="GET", status=200, duration_ms=int((time.perf_counter() - route_started) * 1000), edge_http_status=200)
                return Response(content=content, media_type=media_type, headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
        except urllib_error.HTTPError as exc:
            _append_diagnostic_log("routes_current.jsonl", "PROFILE_MEDIA_ROUTE", route=f"/api/ui/profile/media/{kind}", method="GET", status=exc.code, duration_ms=int((time.perf_counter() - route_started) * 1000), safe_error_code="profile_media_upstream_http")
            if exc.code in {401, 403}:
                raise HTTPException(status_code=401, detail="authentication_required") from exc
            if exc.code == 404:
                raise HTTPException(status_code=404, detail="Mídia não encontrada.") from exc
            raise HTTPException(status_code=502, detail="profile_media_unavailable") from exc
        except (urllib_error.URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            _append_diagnostic_log("routes_current.jsonl", "PROFILE_MEDIA_ROUTE", route=f"/api/ui/profile/media/{kind}", method="GET", status=502, duration_ms=int((time.perf_counter() - route_started) * 1000), safe_error_code="profile_media_network", exception_class=type(exc).__name__, exception_module=type(exc).__module__, safe_message=type(reason).__name__, timeout=isinstance(exc, TimeoutError), dns_error=isinstance(reason, socket.gaierror), connection_error=isinstance(reason, OSError))
            raise HTTPException(status_code=502, detail="profile_media_unavailable") from exc
    path = BRIDGE.profile_media_path(kind, user_id=principal.user_id)
    if not path:
        stream = BRIDGE.profile_media_stream(kind, user_id=principal.user_id)
        if stream:
            return StreamingResponse(stream.iter_chunks(), media_type=profile.get(f"{kind}_media_type") or "application/octet-stream", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
        _append_diagnostic_log("routes_current.jsonl", "PROFILE_MEDIA_ROUTE", route=f"/api/ui/profile/media/{kind}", method="GET", status=404, duration_ms=int((time.perf_counter() - route_started) * 1000), safe_error_code="media_ref_missing")
        raise HTTPException(status_code=404, detail="Mídia não encontrada.")
    return FileResponse(path, media_type=profile.get(f"{kind}_media_type") or None)


@app.delete("/api/ui/profile/media/{kind}")
def api_profile_media_remove(request: Request, kind: str) -> dict[str, Any]:
    principal = _ui_principal(request, mutate=True)
    profile = _api_call(BRIDGE.remove_profile_media, kind, user_id=principal.user_id)
    _sync_public_profile(principal)
    return {"ok": True, "profile": profile}


@app.get("/api/community/profiles/{user_id}/{kind}")
def api_community_profile_media(user_id: str, kind: str, request: Request) -> FileResponse:
    """Serve only an authenticated author's local avatar/banner media."""
    _ui_principal(request)
    if kind not in {"avatar", "banner"} or len(user_id) > 128:
        raise HTTPException(status_code=404, detail="media_not_found")
    public = COMMUNITY.store.profile_public(user_id)
    if not public.get(f"{kind}_object_key"):
        raise HTTPException(status_code=404, detail="media_not_found")
    path = BRIDGE.profile_media_path(kind, user_id=user_id)
    if not path:
        stream = BRIDGE.profile_media_stream(kind, user_id=user_id)
        if stream:
            profile = BRIDGE.profile_for_user(user_id)
            return StreamingResponse(stream.iter_chunks(), media_type=profile.get(f"{kind}_media_type") or "application/octet-stream", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
        raise HTTPException(status_code=404, detail="media_not_found")
    profile = BRIDGE.profile_for_user(user_id)
    return FileResponse(path, media_type=profile.get(f"{kind}_media_type") or None,
                        headers={"Cache-Control": "private, no-store",
                                 "X-Content-Type-Options": "nosniff"})


# ---- in-app chapter reader ---------------------------------------------------
#
# Every route below identifies the chapter by the opaque job id the browser already
# holds. `_owned_ui_job` proves ownership in SQL first, and the bridge resolves the
# one artifact field bound to that run. No client value ever reaches the filesystem,
# so traversal, absolute paths and "fetch me the manifest instead" have no entry.

_READER_HEADERS = {"Cache-Control": "private, max-age=300",
                   "X-Content-Type-Options": "nosniff"}


def _reader_error(exc: ValueError) -> HTTPException:
    code = str(exc)
    messages = {
        "artifact_unavailable": "O PDF desta execução não está disponível.",
        "page_not_found": "Esta página não existe neste capítulo.",
        "page_not_available": "Não foi possível abrir esta página.",
    }
    if code == "artifact_not_found":
        return HTTPException(status_code=404, detail="not_found")
    return HTTPException(status_code=404, detail={
        "code": code, "message": messages.get(code, "Não foi possível abrir este PDF.")})


@app.get("/api/ui/reader/{job_id}")
def api_reader_document(request: Request, job_id: str) -> dict[str, Any]:
    principal = _owned_ui_job(request, job_id)
    try:
        return BRIDGE.reader_document_for_owner(principal.owner_id, job_id)
    except ValueError as exc:
        raise _reader_error(exc) from exc


@app.get("/api/ui/reader/{job_id}/page/{page_number}")
def api_reader_page(request: Request, job_id: str, page_number: int) -> Response:
    principal = _owned_ui_job(request, job_id)
    try:
        payload = BRIDGE.reader_page_for_owner(principal.owner_id, job_id, page_number)
    except ValueError as exc:
        raise _reader_error(exc) from exc
    return Response(content=payload, media_type="image/jpeg", headers=_READER_HEADERS)


@app.get("/api/ui/reader/{job_id}/thumb/{page_number}")
def api_reader_thumbnail(request: Request, job_id: str, page_number: int) -> Response:
    principal = _owned_ui_job(request, job_id)
    try:
        payload = BRIDGE.reader_thumbnail_for_owner(
            principal.owner_id, job_id, page_number)
    except ValueError as exc:
        raise _reader_error(exc) from exc
    return Response(content=payload, media_type="image/jpeg", headers=_READER_HEADERS)


@app.get("/api/ui/reader/{job_id}/pdf")
def api_reader_pdf(request: Request, job_id: str) -> FileResponse:
    """Serve the canonical PDF itself, for the browser viewer fallback.

    FileResponse answers Range requests, so a large chapter is not re-sent in full
    for every seek. Only this run's own `pdf_path` can ever be returned.
    """

    principal = _owned_ui_job(request, job_id)
    try:
        path = BRIDGE.reader_pdf_for_owner(principal.owner_id, job_id)
    except ValueError as exc:
        raise _reader_error(exc) from exc
    return FileResponse(path, media_type="application/pdf", headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": "inline",
    })


@app.post("/api/ui/open")
def api_open(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    principal = _ui_principal(request, mutate=True)
    _api_call(
        BRIDGE.open_artifact_for_owner,
        principal.owner_id,
        str(payload.get("job_id") or ""),
        str(payload.get("artifact") or ""),
        select=bool(payload.get("select", False)),
    )
    return {"ok": True}


@app.post("/api/ui/history/delete")
def api_history_delete(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    try:
        record_id = str(
            payload.get("local_artifact_id") or payload.get("record_id") or "")
        _owned_ui_job(request, record_id, mutate=True)
        return BRIDGE.delete_local_artifact(
            record_id,
            delete_files=bool(payload.get("delete_files", False)),
            confirm=str(payload.get("confirmation") or payload.get("confirm") or ""),
        )
    except ValueError as exc:
        code = str(exc)
        messages = {
            "local_artifact_not_found": "Este capítulo não foi encontrado no histórico local.",
            "local_artifact_in_use": "Este capítulo está em uso por um processamento ativo.",
            "local_artifact_published": "Este capítulo possui publicação na Comunidade; os arquivos locais foram preservados.",
            "local_artifact_path_invalid": "A pasta deste capítulo não é segura para exclusão automática.",
            "local_artifact_delete_failed": "Não foi possível apagar os arquivos locais.",
            "confirmation_invalid": "Digite EXCLUIR para confirmar.",
        }
        raise HTTPException(status_code=400, detail={
            "code": code,
            "message": messages.get(code, "Não foi possível excluir este capítulo local."),
        }) from exc


@ui.page("/")
def index() -> None:
    shell = SHELL_PATH.read_text(encoding="utf-8")
    runtime_identity = dumps_json(_runtime_asset_identity(), ensure_ascii=False)
    visual_test_enabled = os.getenv("TRADUTOR_UI_VISUAL_TEST", "").strip() == "1"
    # The local UI must remain offline-capable: system font fallbacks in the
    # stylesheet are sufficient, and remote font hosts would make a localhost
    # page perform an unexpected external request on every reload.
    ui.add_head_html(f'<link rel="stylesheet" href="{_asset_url(TRADUTOR_CSS_ASSET)}">')
    ui.add_head_html(f'<link rel="stylesheet" href="{_asset_url(LOADING_SURFACE_CSS_ASSET)}">')
    ui.add_head_html(f'<link rel="icon" type="image/x-icon" href="{_asset_url(FAVICON_ASSET)}">')
    ui.add_body_html(shell)
    ui.add_body_html(
        "<script>"
        f"window.__tradutorRuntimeIdentity = {runtime_identity};"
        f"window.__tradutorBuildVersion = {dumps_json(BUILD_VERSION)};"
        f"window.__yomuPassiveAdsProviderEnabled = {dumps_json(os.getenv('YOMU_PASSIVE_ADS_PROVIDER_ENABLED', '').strip().lower() not in {'0', 'false', 'no', 'off'})};"
        "window.__yomuPassiveAdsPreference = false;"
        f"window.__yomuAdsNativePoc = {dumps_json(os.getenv('YOMU_ADS_NATIVE_POC', '').strip().lower() in {'1', 'true', 'yes', 'on'})};"
        f"window.__yomuNativeAdsDiagnosticBuildId = {dumps_json('native-home-slot-probe-local-r1')};"
        f"window.__tradutorVisualTestEnabled = {'true' if visual_test_enabled else 'false'};"
        f"window.__tradutorAuthDiagnosticsEnabled = {'true' if _AUTH_DIAGNOSTICS_ENABLED else 'false'};"
        "</script>"
    )
    ui.add_body_html(_i18n_bootstrap_html())
    # The view model must exist before the renderer, and both before the main
    # bundle. All are deferred, so source order is execution order.
    ui.add_body_html(f'<script src="{_asset_url(LOADING_VIEW_ASSET)}" defer></script>')
    ui.add_body_html(f'<script src="{_asset_url(PROCESSING_SURFACE_ASSET)}" defer></script>')
    ui.add_body_html(f'<script src="{_asset_url(TRADUTOR_UI_ASSET)}" defer></script>')
    if visual_test_enabled:
        ui.add_body_html(f'<script src="{_asset_url(PIPELINE_HARNESS_ASSET)}" defer></script>')
    ui.add_body_html(f'<script type="module" src="{_asset_url(SERVICE_HEALTH_ASSET)}"></script>')
    ui.add_head_html(f'<script type="module" src="{_asset_url(STATIC_DIR / "control_plane_client.js")}"></script>')
    ui.add_head_html(f'<script src="{_asset_url(STATIC_DIR / "passive_ad_slot.js")}"></script>')
    # Keep the auth module in the document head so it is parsed/executed by the
    # browser independently of body-fragment hydration and the NiceGUI websocket.
    # Unlike add_body_html, this is part of the page head before the shell starts.
    ui.add_head_html(f'<script type="module" src="{_asset_url(AUTH_UI_ASSET)}"></script>')
    ui.add_body_html(f'<script type="module" src="{_asset_url(SOCIAL_COMMUNITY_ASSET)}"></script>')
    ui.add_body_html(f'<script type="module" src="{_asset_url(CHAPTER_READER_ASSET)}"></script>')


async def _build_auth_provider_at_startup() -> None:
    """Build (and validate) the real auth provider before the app serves traffic.

    Runs whenever the ASGI server actually starts -- not at import, and not only
    under the `__main__` bind-security check below (which can return early for a
    loopback bind without touching the provider). A misconfigured deployment must
    fail closed here, before any request is served, never silently on first use.

    Registered via a plain call (not `@app.on_startup` sugar): that decorator
    returns None, which would otherwise clobber this module-level name and make
    it impossible for tests to invoke the handler directly.
    """
    _append_diagnostic_log("app_current.jsonl", "LOCAL_SERVER_READY", port=APP_PORT)
    get_auth_provider()
    _append_diagnostic_log("app_current.jsonl", "LICENSE_PROVIDER_READY", available=True)


app.on_startup(_build_auth_provider_at_startup)


@app.on_shutdown
async def shutdown_processes() -> None:
    _append_diagnostic_log("app_current.jsonl", "APP_SHUTDOWN_BEGIN")
    await BRIDGE.shutdown()
    _append_diagnostic_log("app_current.jsonl", "APP_SHUTDOWN_END")


def main() -> int:
    try:
        bind_host = validate_bind_security(APP_HOST, AUTH)
    except AuthConfigurationError as exc:
        raise SystemExit(f"configuration_error: {exc}") from exc
    _assert_startup_port_available(bind_host, APP_PORT)
    _append_diagnostic_log("app_current.jsonl", "LOCAL_SERVER_START_BEGIN", host=bind_host, port=APP_PORT)
    ui.run(
        host=bind_host,
        port=APP_PORT,
        title="Tradutor.Ia · Painel local",
        dark=True,
        language="pt-BR",
        show=False,
        reload=False,
        show_welcome_message=False,
    )
    return 0


if __name__ in {"__main__", "__mp_main__"}:
    raise SystemExit(main())
