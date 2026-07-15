"""NiceGUI host for the custom Tradutor.Ia local frontend."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from fastapi import Body, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from nicegui import app, ui

from community_api import CommunityApi
from community_service import CommunityError
from community_storage import StorageError
from ui_bridge import UiBridge


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
SHELL_PATH = ROOT / "ui" / "ui_shell.html"
APP_PORT = int(os.getenv("TRADUTOR_UI_PORT", "8080"))
BRIDGE = UiBridge()
COMMUNITY = CommunityApi(BRIDGE.store)

app.add_static_files("/static", STATIC_DIR)


def _community_call(callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return callback(*args, **kwargs)
    except CommunityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=502, detail="storage_unavailable") from exc


def _api_call(callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return callback(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/ui/bootstrap")
def api_bootstrap(cursor: int = Query(0, ge=0)) -> dict[str, Any]:
    return BRIDGE.bootstrap(cursor)


@app.get("/api/ui/state")
def api_state(cursor: int = Query(0, ge=0)) -> dict[str, Any]:
    return BRIDGE.runtime_state(cursor)


@app.post("/api/ui/run")
async def api_run(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    try:
        return await BRIDGE.start(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/ui/cancel")
async def api_cancel(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    return await BRIDGE.cancel(queue=bool(payload.get("queue", False)))


@app.post("/api/ui/queue/add")
def api_queue_add(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    return {"ok": True, "item": _api_call(BRIDGE.add_queue_item, payload)}


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


# ---- community ------------------------------------------------------------
@app.post("/api/community/publish")
def api_community_publish(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    return _community_call(COMMUNITY.publish, payload)


@app.get("/api/community/posts")
def api_community_feed(series_slug: str = Query(""), q: str = Query(""),
                       limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    return _community_call(COMMUNITY.feed, series_slug=series_slug, query=q, limit=limit, offset=offset)


@app.get("/api/community/my-posts")
def api_community_my_posts() -> dict[str, Any]:
    return _community_call(COMMUNITY.my_posts)


@app.post("/api/community/posts/{post_id}/unpublish")
def api_community_unpublish(post_id: str) -> dict[str, Any]:
    return _community_call(COMMUNITY.unpublish, post_id)


@app.api_route("/api/community/posts/{post_id}/pdf", methods=["GET", "HEAD"])
def api_community_pdf(post_id: str, request: Request):
    range_header = request.headers.get("range", "") if request.method == "GET" else ""
    meta, stream = _community_call(COMMUNITY.open_pdf, post_id, range_header=range_header)
    headers = {
        "Content-Length": str(meta["content_length"]),
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'inline; filename="{meta["filename"]}"',
        "X-Content-Type-Options": "nosniff",
    }
    if request.method == "HEAD":
        headers["Content-Length"] = str(meta["total_size"])
        return Response(status_code=200, media_type="application/pdf", headers=headers)
    if meta["partial"]:
        headers["Content-Range"] = f'bytes {meta["start"]}-{meta["end"]}/{meta["total_size"]}'
        status = 206
    else:
        status = 200
    return StreamingResponse(stream.iter_chunks(), status_code=status,
                             media_type="application/pdf", headers=headers)


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
    ui.run(
        host="0.0.0.0",
        port=APP_PORT,
        title="Tradutor.Ia · Painel local",
        dark=True,
        language="pt-BR",
        show=False,
        reload=False,
        show_welcome_message=False,
    )
