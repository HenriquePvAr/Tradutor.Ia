"""NiceGUI host for the custom Tradutor.Ia local frontend."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from fastapi import Body, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from nicegui import app, ui

from community_auth import (
    AuthConfigurationError,
    AuthenticationRequired,
    AuthorizationDenied,
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
import audit_decisions
import linguistic_triage
import region_taxonomy
import natural_ptbr_refinement
import refinement_selection_decisions
from translator_nvidia import TranslatorNvidiaBatch
from chapter_quality_revision import REVIEW_SCHEMA_VERSION
from local_environment import load_local_environment_for_entrypoint
from process_options import hidden_console_options
from ui_bridge import UiBridge, local_folder_ui_allowed


if not load_local_environment_for_entrypoint():
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parent
_SERVER_STARTED_AT = __import__("datetime").datetime.now(
    __import__("datetime").timezone.utc).isoformat()
STATIC_DIR = ROOT / "static"
SHELL_PATH = ROOT / "ui" / "ui_shell.html"
AUTH_UI_ASSET = ROOT / "static" / "auth_ui.js"
AUTH_PROVIDER_ASSET = ROOT / "static" / "auth_provider.js"
TRADUTOR_UI_ASSET = ROOT / "static" / "tradutor_ui.js"
TRADUTOR_CSS_ASSET = ROOT / "static" / "tradutor_ui.css"
LOADING_SURFACE_CSS_ASSET = ROOT / "static" / "loading_surface.css"
LOADING_VIEW_ASSET = ROOT / "static" / "loading_view.js"
PROCESSING_SURFACE_ASSET = ROOT / "static" / "processing_surface.js"
SOCIAL_COMMUNITY_ASSET = ROOT / "static" / "social_community.js"
I18N_ASSETS = [
    ROOT / "static" / "i18n" / "pt-BR.js",
    ROOT / "static" / "i18n" / "en-US.js",
    ROOT / "static" / "i18n" / "es-ES.js",
    ROOT / "static" / "i18n" / "fr-FR.js",
    ROOT / "static" / "i18n" / "ja-JP.js",
    ROOT / "static" / "i18n" / "ko-KR.js",
    ROOT / "static" / "i18n" / "index.js",
]


def _asset_url(path: Path) -> str:
    """Version local static assets so a restarted UI cannot reuse stale auth code."""

    try:
        version = str(path.stat().st_mtime_ns)
    except OSError:
        version = "0"
    try:
        rel = path.relative_to(STATIC_DIR).as_posix()
    except ValueError:
        rel = path.name
    return f"/static/{rel}?v={version}"
APP_PORT = int(os.getenv("TRADUTOR_UI_PORT", "8080"))
APP_HOST = configured_bind_host()
BRIDGE = UiBridge()
COMMUNITY = CommunityApi(
    BRIDGE.store,
    community_db_path=BRIDGE.runtime_root / "community.sqlite3",
    output_root=BRIDGE.output_root,
    storage_root=BRIDGE.runtime_root / "community_storage",
)
AUTH = build_auth_provider()


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
def auth_callback() -> FileResponse:
    """Supabase e-mail confirmation/login landing page.

    Static file, no query/hash parameter is ever echoed back, and the page always
    returns to the fixed local root — no open redirect surface.
    """
    return FileResponse(ROOT / "ui" / "auth_callback.html", media_type="text/html")


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
        }
        payload["profile"] = _profile_for_principal(principal)
        try:
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
        }
        payload["profile"] = {}
    return payload


@app.get("/api/ui/state")
def api_state(request: Request, cursor: int = Query(0, ge=0)) -> dict[str, Any]:
    principal = _ui_principal(request)
    return BRIDGE.runtime_state_for_owner(principal.owner_id, cursor)


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
            principal=_ui_principal(request, mutate=True),
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
        reason_code = str(exc)
        if reason_code in {
            "download_authorization_required",
            "explicit_download_request_required",
            "workspace_source_authorization_required",
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
        raise HTTPException(status_code=400, detail={
            "code": "invalid_request", "stage": "validacao",
            "message": reason_code, "action": "Corrija os campos e tente novamente.",
        }) from exc


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


def _page_revision_error(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail={
        "code": str(exc),
        "message": "Não foi possível concluir a ação da revisão da página.",
        "action": "Confira job, run e a revisão da página e tente novamente.",
    })


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
    if payload.get("active") not in {True, False}:
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
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/ui/resume")
def api_resume(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    job_id = str(payload.get("job_id") or payload.get("id") or "")
    _owned_ui_job(request, job_id, mutate=True)
    return _api_call(BRIDGE.resume, job_id)


@app.post("/api/ui/profile")
def api_profile(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    principal = _ui_principal(request, mutate=True)
    try:
        profile = BRIDGE.save_profile(payload, user_id=principal.user_id)
        _sync_public_profile(principal)
    except ValueError as exc:
        if str(exc) == "display_name_taken":
            raise HTTPException(status_code=409, detail={"code": "display_name_taken", "message": "Este nome de exibição já está em uso."}) from exc
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
    visual_test_enabled = os.getenv("TRADUTOR_UI_VISUAL_TEST", "").strip() == "1"
    # The local UI must remain offline-capable: system font fallbacks in the
    # stylesheet are sufficient, and remote font hosts would make a localhost
    # page perform an unexpected external request on every reload.
    ui.add_head_html(f'<link rel="stylesheet" href="{_asset_url(TRADUTOR_CSS_ASSET)}">')
    ui.add_head_html(f'<link rel="stylesheet" href="{_asset_url(LOADING_SURFACE_CSS_ASSET)}">')
    ui.add_body_html(shell)
    ui.add_body_html(
        "<script>"
        f"window.__tradutorVisualTestEnabled = {'true' if visual_test_enabled else 'false'};"
        "</script>"
    )
    for asset in I18N_ASSETS:
        ui.add_body_html(f'<script src="{_asset_url(asset)}" defer></script>')
    # The view model must exist before the renderer, and both before the main
    # bundle. All are deferred, so source order is execution order.
    ui.add_body_html(f'<script src="{_asset_url(LOADING_VIEW_ASSET)}" defer></script>')
    ui.add_body_html(f'<script src="{_asset_url(PROCESSING_SURFACE_ASSET)}" defer></script>')
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
