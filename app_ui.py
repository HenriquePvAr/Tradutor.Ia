"""NiceGUI host for the custom Tradutor.Ia local frontend."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from fastapi import Body, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from nicegui import app, ui

from community_auth import (
    AuthConfigurationError,
    AuthenticationRequired,
    CsrfRejected,
    RequestPrincipal,
    SESSION_COOKIE_NAME,
    build_auth_provider,
    configured_bind_host,
    validate_bind_security,
)
from community_api import CommunityApi
from community_http import (
    CommunityNetworkBoundaryMiddleware,
    create_admin_community_router,
    create_community_router,
)
from local_environment import load_local_environment_for_entrypoint
from ui_bridge import UiBridge, local_folder_ui_allowed


if not load_local_environment_for_entrypoint():
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
SHELL_PATH = ROOT / "ui" / "ui_shell.html"
AUTH_UI_ASSET = ROOT / "static" / "auth_ui.js"
TRADUTOR_UI_ASSET = ROOT / "static" / "tradutor_ui.js"
TRADUTOR_CSS_ASSET = ROOT / "static" / "tradutor_ui.css"
SOCIAL_COMMUNITY_ASSET = ROOT / "static" / "social_community.js"


def _asset_url(path: Path) -> str:
    """Version local static assets so a restarted UI cannot reuse stale auth code."""

    try:
        version = str(path.stat().st_mtime_ns)
    except OSError:
        version = "0"
    return f"/static/{path.name}?v={version}"
APP_PORT = int(os.getenv("TRADUTOR_UI_PORT", "8080"))
APP_HOST = configured_bind_host()
BRIDGE = UiBridge()
COMMUNITY = CommunityApi(BRIDGE.store)
AUTH = build_auth_provider()


def _sync_public_profile(principal: RequestPrincipal) -> dict[str, Any]:
    """Project the authenticated local profile into the community read model.

    The principal is the only identity accepted here; browser payloads never select
    the profile row.  Media keys are opaque local markers, not filesystem paths.
    """
    profile = BRIDGE.profile_for_user(principal.user_id)
    return COMMUNITY.store.upsert_profile(principal.user_id, {
        "display_name": profile.get("display_name") or "Usuário",
        "avatar_object_key": "local:avatar" if BRIDGE.profile_media_path("avatar", user_id=principal.user_id) else "",
        "banner_object_key": "local:banner" if BRIDGE.profile_media_path("banner", user_id=principal.user_id) else "",
        "public_role": profile.get("title") or "",
        "pronouns": profile.get("pronouns") or "",
        "status": profile.get("status") or "online",
        "status_message": profile.get("status_text") or "",
        "bio": profile.get("bio") or "",
        "accent_color": profile.get("avatar_color") or "#c5372c",
    })


COMMUNITY._profile_sync = _sync_public_profile


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

app.add_middleware(CommunityNetworkBoundaryMiddleware, auth=AUTH)
app.add_static_files("/static", STATIC_DIR)
app.include_router(create_community_router(COMMUNITY, AUTH))
app.include_router(create_admin_community_router(COMMUNITY, AUTH))

# Supabase social API (works/chapters/comments/…). Mounted only when the social provider
# is configured; the browser never reaches the tables directly — every call forwards the
# user's JWT to the Data API under RLS. Fails closed (skips mounting) if unconfigured.
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
            ROOT / ".cache" / "runtime" / "social_assets.sqlite3", community_store=COMMUNITY.store)
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
    except SocialConfigError as exc:
        print(f"social API not mounted: {exc}")
except Exception as exc:  # never let an optional feature break app startup
    print(f"social API not mounted: {type(exc).__name__}")


def _api_call(callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return callback(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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


def _local_folder_submit_allowed(request: Request) -> bool:
    """A browser may submit a filesystem folder only to a loopback-only UI server."""

    peer = str(getattr(getattr(request, "client", None), "host", "") or "")
    return local_folder_ui_allowed(bind_host=APP_HOST, peer_host=peer)


@app.get("/auth/callback")
def auth_callback() -> FileResponse:
    """Supabase e-mail confirmation/login landing page.

    Static file, no query/hash parameter is ever echoed back, and the page always
    returns to the fixed local root — no open redirect surface.
    """
    return FileResponse(ROOT / "ui" / "auth_callback.html", media_type="text/html")


@app.get("/api/ui/bootstrap")
def api_bootstrap(request: Request, cursor: int = Query(0, ge=0)) -> dict[str, Any]:
    payload = BRIDGE.bootstrap(cursor)
    _enrich_history_publications(payload.get("history") or [])
    try:
        principal = AUTH.authenticate_request(request)
        payload["community"] = {
            **(payload.get("community") or {}),
            "authenticated": bool(principal.authenticated),
            "auth_state": "authenticated" if principal.authenticated else "unauthenticated",
            "user_id": principal.user_id if principal.authenticated else "",
            "available": True,
        }
        payload["profile"] = BRIDGE.profile_for_user(principal.user_id)
        _sync_public_profile(principal)
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
        }
        payload["profile"] = {}
    return payload


@app.get("/api/ui/state")
def api_state(cursor: int = Query(0, ge=0)) -> dict[str, Any]:
    return BRIDGE.runtime_state(cursor)


@app.post("/api/ui/run")
async def api_run(
    request: Request,
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    from chapter_source import SourceError, supported_hosts

    try:
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
        return await BRIDGE.start(
            payload,
            principal=_job_principal(request),
            local_folder_allowed=requests_local_folder,
        )
    except SourceError as exc:
        # Source diagnostics are coded and deliberately generic: URL fragments, headers,
        # cookies and provider responses never reach the browser.
        messages = {
            "unsupported_source": ("Esta URL não pode ser aberta com segurança.", "Use uma URL pública HTTP(S) de capítulo."),
            "challenge_required": ("O site exige uma verificação interativa.", "Conclua a verificação no site ou use uma fonte sem desafio."),
            "authentication_required": ("Esta fonte exige autenticação.", "Use uma página pública, sem login."),
            "source_access_denied": ("A fonte recusou o acesso público.", "Verifique a URL ou tente novamente mais tarde."),
            "source_rate_limited": ("A fonte limitou temporariamente o acesso.", "Aguarde antes de tentar novamente."),
            "no_chapter_images": ("Nenhuma página de capítulo foi encontrada.", "Confirme que a URL abre o leitor do capítulo."),
            "unsupported_low_confidence": ("Não foi possível reconhecer o leitor com segurança.", "Use uma fonte com leitor visível ou um adapter específico."),
            "unsupported_canvas_reader": ("O leitor em canvas não pôde ser capturado com integridade.", "Use uma fonte que exponha páginas visíveis sem proteção interativa."),
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
        raise HTTPException(status_code=400, detail={
            "code": "invalid_request", "stage": "validacao",
            "message": str(exc), "action": "Corrija os campos e tente novamente.",
        }) from exc


@app.post("/api/ui/cancel")
async def api_cancel(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    try:
        return await BRIDGE.cancel(
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
async def api_job_cancel(job_id: str) -> JSONResponse:
    """Cancel exactly the job named by the caller without waiting for its runner."""
    try:
        result = await BRIDGE.cancel(job_id=str(job_id or ""))
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


@app.get("/api/ui/quality-review/{job_id}")
def api_quality_review(job_id: str) -> dict[str, Any]:
    review = BRIDGE.quality_review(job_id)
    if review is None:
        raise HTTPException(status_code=404, detail={
            "code": "quality_review_not_available",
            "message": "Não há uma revisão de qualidade disponível.",
        })
    return review


@app.get("/api/ui/quality-review/{job_id}/page/{page_number}")
def api_quality_review_page(job_id: str, page_number: int) -> FileResponse:
    path = BRIDGE.quality_review_page(job_id, page_number)
    if path is None:
        raise HTTPException(status_code=404, detail="Página de revisão não encontrada.")
    return FileResponse(path)


@app.post("/api/ui/quality-review/action")
def api_quality_review_action(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    try:
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


@app.post("/api/ui/quality-review/confirm")
def api_quality_review_confirm(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    try:
        return BRIDGE.confirm_quality_review(str(payload.get("job_id") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "code": str(exc),
            "message": "Ainda há itens que precisam ser revisados.",
            "action": "Revise cada item ou mantenha o original antes de confirmar.",
        }) from exc


@app.post("/api/ui/retry")
def api_retry(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    try:
        return BRIDGE.retry_job(str(payload.get("job_id") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "code": str(exc),
            "message": "Esta execução não pode ser repetida automaticamente.",
            "action": "Envie uma nova tradução para criar uma tentativa separada.",
        }) from exc


@app.post("/api/ui/source/confirm")
def api_source_confirm(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    try:
        return BRIDGE.confirm_source_pages(
            str(payload.get("job_id") or ""),
            payload.get("candidate_ids") if isinstance(payload.get("candidate_ids"), list) else [],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "code": str(exc), "stage": "revisao_da_fonte",
            "message": "A seleção de páginas não é válida.",
            "action": "Selecione ao menos uma página encontrada e tente novamente.",
        }) from exc


@app.post("/api/ui/source/retry")
async def api_source_retry(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    try:
        return await BRIDGE.retry_source_review(str(payload.get("job_id") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "code": str(exc), "stage": "revisao_da_fonte",
            "message": "Esta revisÃ£o de fonte nÃ£o pode mais ser repetida.",
            "action": "Atualize a tela e escolha a revisÃ£o atualmente exibida.",
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
def api_profile(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    principal = _ui_principal(request, mutate=True)
    try:
        profile = BRIDGE.save_profile(payload, user_id=principal.user_id)
        _sync_public_profile(principal)
    except ValueError as exc:
        if str(exc) == "display_name_taken":
            raise HTTPException(status_code=409, detail={"code": "display_name_taken", "message": "Este nome de exibiÃ§Ã£o jÃ¡ estÃ¡ em uso."}) from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "profile": profile}


@app.post("/api/ui/profile/media/{kind}")
async def api_profile_media_upload(
    kind: str,
    request: Request,
    filename: str = Query(..., min_length=1, max_length=180),
    content_type: str = Query(..., min_length=3, max_length=80),
) -> dict[str, Any]:
    principal = _ui_principal(request, mutate=True)
    content = await request.body()
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


@app.get("/api/ui/profile/media/{kind}")
def api_profile_media(request: Request, kind: str) -> FileResponse:
    principal = _ui_principal(request)
    path = BRIDGE.profile_media_path(kind, user_id=principal.user_id)
    if not path:
        raise HTTPException(status_code=404, detail="Mídia não encontrada.")
    profile = BRIDGE.profile_for_user(principal.user_id)
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
        raise HTTPException(status_code=404, detail="media_not_found")
    profile = BRIDGE.profile_for_user(user_id)
    return FileResponse(path, media_type=profile.get(f"{kind}_media_type") or None,
                        headers={"Cache-Control": "private, no-store",
                                 "X-Content-Type-Options": "nosniff"})


@app.post("/api/ui/open")
def api_open(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    _api_call(
        BRIDGE.open_artifact,
        str(payload.get("path") or ""),
        select=bool(payload.get("select", False)),
    )
    return {"ok": True}


@app.post("/api/ui/history/delete")
def api_history_delete(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    try:
        return BRIDGE.delete_local_artifact(
            str(payload.get("local_artifact_id") or payload.get("record_id") or ""),
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
    ui.add_head_html(
        """
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link href="https://fonts.googleapis.com/css2?family=Black+Han+Sans&family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
        """
        + f'<link rel="stylesheet" href="{_asset_url(TRADUTOR_CSS_ASSET)}">'
    )
    ui.add_body_html(shell)
    ui.add_body_html(f'<script src="{_asset_url(TRADUTOR_UI_ASSET)}" defer></script>')
    ui.add_body_html(f'<script type="module" src="{_asset_url(AUTH_UI_ASSET)}"></script>')
    ui.add_body_html(f'<script type="module" src="{_asset_url(SOCIAL_COMMUNITY_ASSET)}"></script>')


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
