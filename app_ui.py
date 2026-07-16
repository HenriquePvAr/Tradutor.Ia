"""NiceGUI host for the custom Tradutor.Ia local frontend."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from fastapi import Body, HTTPException, Query, Request
from fastapi.responses import FileResponse
from nicegui import app, ui

from community_auth import (
    AuthConfigurationError,
    CsrfRejected,
    RequestPrincipal,
    SESSION_COOKIE_NAME,
    build_auth_provider,
    configured_bind_host,
    validate_bind_security,
)
from community_api import CommunityApi
from community_http import CommunityNetworkBoundaryMiddleware, create_community_router
from local_environment import load_local_environment_for_entrypoint
from ui_bridge import UiBridge


if not load_local_environment_for_entrypoint():
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
SHELL_PATH = ROOT / "ui" / "ui_shell.html"
APP_PORT = int(os.getenv("TRADUTOR_UI_PORT", "8080"))
APP_HOST = configured_bind_host()
BRIDGE = UiBridge()
COMMUNITY = CommunityApi(BRIDGE.store)
AUTH = build_auth_provider()

app.add_middleware(CommunityNetworkBoundaryMiddleware, auth=AUTH)
app.add_static_files("/static", STATIC_DIR)
app.include_router(create_community_router(COMMUNITY, AUTH))


def _api_call(callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return callback(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _job_principal(request: Request) -> RequestPrincipal:
    """Optionally bind a local translation job to a valid cookie-authenticated user."""
    principal = AUTH.authenticate_request(request)
    if not principal.authenticated and request.cookies.get(SESSION_COOKIE_NAME):
        raise HTTPException(status_code=401, detail="authentication_required")
    if principal.authenticated:
        try:
            AUTH.require_csrf(request, principal)
        except CsrfRejected as exc:
            raise HTTPException(status_code=403, detail="csrf_rejected") from exc
    return principal


@app.get("/api/ui/bootstrap")
def api_bootstrap(cursor: int = Query(0, ge=0)) -> dict[str, Any]:
    return BRIDGE.bootstrap(cursor)


@app.get("/api/ui/state")
def api_state(cursor: int = Query(0, ge=0)) -> dict[str, Any]:
    return BRIDGE.runtime_state(cursor)


@app.post("/api/ui/run")
async def api_run(
    request: Request,
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    try:
        return await BRIDGE.start(payload, principal=_job_principal(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/ui/cancel")
async def api_cancel(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    return await BRIDGE.cancel(queue=bool(payload.get("queue", False)))


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
            principal=_job_principal(request),
        ),
    }


@app.post("/api/ui/queue/remove")
def api_queue_remove(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    BRIDGE.remove_queue_item(str(payload.get("id") or ""))
    return {"ok": True}


@app.post("/api/ui/queue/clear")
def api_queue_clear() -> dict[str, Any]:
    BRIDGE.clear_queue()
    return {"ok": True}


@app.post("/api/ui/queue/start")
async def api_queue_start() -> dict[str, Any]:
    try:
        return await BRIDGE.start_queue()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/ui/resume")
def api_resume(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    return _api_call(BRIDGE.resume, str(payload.get("job_id") or payload.get("id") or ""))


@app.post("/api/ui/profile")
def api_profile(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    return {"ok": True, "profile": BRIDGE.save_profile(payload)}


@app.post("/api/ui/profile/media/{kind}")
async def api_profile_media_upload(
    kind: str,
    request: Request,
    filename: str = Query(..., min_length=1, max_length=180),
    content_type: str = Query(..., min_length=3, max_length=80),
) -> dict[str, Any]:
    content = await request.body()
    return {
        "ok": True,
        "profile": _api_call(
            BRIDGE.save_profile_media,
            kind,
            filename=filename,
            content_type=content_type,
            content=content,
        ),
    }


@app.get("/api/ui/profile/media/{kind}")
def api_profile_media(kind: str) -> FileResponse:
    path = BRIDGE.profile_media_path(kind)
    if not path:
        raise HTTPException(status_code=404, detail="Mídia não encontrada.")
    return FileResponse(path, media_type=BRIDGE.profile.get(f"{kind}_media_type") or None)


@app.delete("/api/ui/profile/media/{kind}")
def api_profile_media_remove(kind: str) -> dict[str, Any]:
    return {"ok": True, "profile": _api_call(BRIDGE.remove_profile_media, kind)}


@app.post("/api/ui/open")
def api_open(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    _api_call(
        BRIDGE.open_artifact,
        str(payload.get("path") or ""),
        select=bool(payload.get("select", False)),
    )
    return {"ok": True}


@ui.page("/")
def index() -> None:
    shell = SHELL_PATH.read_text(encoding="utf-8")
    ui.add_head_html(
        """
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link href="https://fonts.googleapis.com/css2?family=Black+Han+Sans&family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
        <link rel="stylesheet" href="/static/tradutor_ui.css">
        """
    )
    ui.add_body_html(shell)
    ui.add_body_html('<script src="/static/tradutor_ui.js" defer></script>')


@app.on_shutdown
async def shutdown_processes() -> None:
    await BRIDGE.shutdown()


if __name__ in {"__main__", "__mp_main__"}:
    try:
        bind_host = validate_bind_security(APP_HOST, AUTH)
    except AuthConfigurationError as exc:
        raise SystemExit(f"configuration_error: {exc}") from exc
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
