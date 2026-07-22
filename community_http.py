"""FastAPI router for the authenticated community boundary.

Kept independent from NiceGUI so security behavior can be exercised with a hermetic
ASGI app and injected offline storage/auth providers.
"""

from __future__ import annotations

import hmac
from typing import Any, Callable

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from community_auth import (
    AuthenticationRequired,
    AuthorizationDenied,
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    CsrfRejected,
    LocalSessionAuthProvider,
    RequestPrincipal,
    ResourceNotFound,
    SESSION_COOKIE_NAME,
    clear_session_cookies,
    external_bind_is_explicitly_allowed,
    peer_is_loopback,
    request_is_loopback,
    set_session_cookies,
)
from community_api import ArtifactBindingError, RangeNotSatisfiable
from community_service import CommunityError
from community_storage import StorageError

_NO_STORE_HEADERS = {"Cache-Control": "private, no-store", "Vary": "Cookie"}


class CommunityNetworkBoundaryMiddleware:
    """Runner-independent HTTP/WebSocket defense against accidental external exposure."""

    def __init__(self, app, *, auth, allow_external: bool | None = None):
        self.app = app
        self.auth = auth
        self.allow_external = allow_external

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        client = scope.get("client") or ("", 0)
        explicit = (
            external_bind_is_explicitly_allowed()
            if self.allow_external is None
            else self.allow_external
        )
        strong_external_auth = bool(
            explicit
            and
            getattr(self.auth, "configured", False)
            and getattr(self.auth, "supports_external_bind", False)
        )
        if not strong_external_auth and not peer_is_loopback(str(client[0])):
            if scope["type"] == "websocket":
                await send({
                    "type": "websocket.close",
                    "code": 1008,
                    "reason": "external_bind_not_authorized",
                })
            else:
                response = JSONResponse(
                    {"detail": "external_bind_not_authorized"}, status_code=503)
                await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _community_call(callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return callback(*args, **kwargs)
    except AuthenticationRequired as exc:
        raise HTTPException(
            status_code=401,
            detail="authentication_required",
            headers={"WWW-Authenticate": "Session", **_NO_STORE_HEADERS},
        ) from exc
    except ResourceNotFound as exc:
        code = str(exc or "not_found")
        messages = {
            "chapter_not_found": "Não encontramos este capítulo no histórico local. Atualize a biblioteca e tente novamente.",
            "output_not_found": "O PDF ou processamento deste capítulo não foi encontrado.",
            "pdf_not_found": "O PDF deste capítulo não foi encontrado.",
            "manifest_not_found": "O manifesto do capítulo não foi encontrado.",
            "post_not_found": "A publicação anterior não existe mais. Você pode criar uma nova publicação.",
            "user_not_found": "Seu perfil da comunidade não foi localizado. Saia e entre novamente.",
            "artifact_invalid": "O artefato não passou pela validação necessária para publicação.",
            "resolver_error": "Não foi possível validar os arquivos deste capítulo.",
        }
        raise HTTPException(
            status_code=404,
            detail={"code": code, "message": messages.get(
                code, "Não foi possível localizar o recurso necessário para publicar."),
                    "action": "Atualize o histórico e tente novamente."},
            headers=_NO_STORE_HEADERS,
        ) from exc
    except CsrfRejected as exc:
        raise HTTPException(
            status_code=403, detail="csrf_rejected", headers=_NO_STORE_HEADERS
        ) from exc
    except AuthorizationDenied as exc:
        raise HTTPException(
            status_code=403, detail="forbidden", headers=_NO_STORE_HEADERS
        ) from exc
    except RangeNotSatisfiable as exc:
        raise HTTPException(
            status_code=416,
            detail="range_not_satisfiable",
            headers={
                "Content-Range": f"bytes */{exc.total_size}",
                **_NO_STORE_HEADERS,
            },
        ) from exc
    except ArtifactBindingError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
            headers=_NO_STORE_HEADERS,
        ) from exc
    except CommunityError as exc:
        raise HTTPException(
            status_code=400, detail=str(exc), headers=_NO_STORE_HEADERS
        ) from exc
    except StorageError as exc:
        raise HTTPException(
            status_code=502,
            detail="storage_unavailable",
            headers=_NO_STORE_HEADERS,
        ) from exc


def create_community_router(community, auth) -> APIRouter:
    router = APIRouter(prefix="/api/community", tags=["community"])
    no_store_headers = dict(_NO_STORE_HEADERS)

    def request_principal(request: Request) -> RequestPrincipal:
        return _community_call(auth.authenticate_request, request)

    def authenticated_mutation(request: Request) -> RequestPrincipal:
        principal = _community_call(auth.require_authenticated, request)
        _community_call(auth.require_csrf, request, principal)
        return principal

    def stale_logout_mutation(request: Request) -> None:
        """Prove same-origin intent when only stale client cookies remain."""
        session_cookie = str(request.cookies.get(SESSION_COOKIE_NAME, "") or "")
        csrf_cookie = str(request.cookies.get(CSRF_COOKIE_NAME, "") or "")
        csrf_header = str(request.headers.get(CSRF_HEADER_NAME, "") or "")
        if not (
            session_cookie
            and 32 <= len(csrf_cookie) <= 512
            and 32 <= len(csrf_header) <= 512
            and hmac.compare_digest(csrf_cookie, csrf_header)
        ):
            raise CsrfRejected("csrf_required")

    @router.post("/auth/local-session")
    def local_session(
        request: Request,
        bootstrap_secret: str = Header("", alias="X-Tradutor-Bootstrap-Secret"),
    ) -> JSONResponse:
        if not isinstance(auth, LocalSessionAuthProvider):
            raise HTTPException(
                status_code=404,
                detail="not_found",
                headers=_NO_STORE_HEADERS,
            )
        issued = _community_call(auth.bootstrap_session, request, bootstrap_secret)
        response = JSONResponse({
            "authenticated": True,
            "user_id": issued.principal.user_id,
            "roles": sorted(issued.principal.roles),
            "auth_source": issued.principal.auth_source,
            "expires_at": issued.expires_at,
        }, headers=no_store_headers)
        set_session_cookies(response, issued, request=request)
        return response

    @router.get("/auth/config")
    def auth_config() -> JSONResponse:
        """Browser-safe provider configuration; never secrets or JWKS internals."""
        public = getattr(auth, "public_config", None)
        content = public() if callable(public) else {"provider": getattr(auth, "auth_source", "local")}
        content = {**content, "build_version": "local-ui"}
        return JSONResponse(content, headers=no_store_headers)

    @router.get("/auth/session")
    def session(request: Request) -> JSONResponse:
        principal = request_principal(request)
        return JSONResponse({
            "authenticated": principal.authenticated,
            "user_id": principal.user_id if principal.authenticated else "",
            "roles": sorted(principal.roles),
            "auth_source": principal.auth_source,
        }, headers=no_store_headers)

    @router.post("/auth/logout")
    def logout(request: Request) -> JSONResponse:
        principal = request_principal(request)
        if principal.authenticated:
            _community_call(auth.require_csrf, request, principal)
            if isinstance(auth, LocalSessionAuthProvider):
                auth.revoke_session(principal.session_id)
        else:
            _community_call(stale_logout_mutation, request)
        response = JSONResponse({"ok": True}, headers=no_store_headers)
        clear_session_cookies(response, request=request)
        return response

    @router.post("/publish")
    def publish(request: Request,
                payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        principal = authenticated_mutation(request)
        return _community_call(community.publish, payload, principal=principal)

    @router.post("/artifacts/{job_id}/claim")
    def claim_artifact(
        job_id: str,
        request: Request,
        payload: dict[str, Any] = Body(default={}),
    ) -> dict[str, Any]:
        """Claim one eligible ownerless local artifact for the current session."""
        principal = authenticated_mutation(request)
        return _community_call(community.claim_legacy_artifact, job_id, payload,
                               principal=principal)

    @router.get("/posts")
    def feed(
        request: Request,
        series_slug: str = Query(""),
        q: str = Query(""),
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        principal = request_principal(request)
        return _community_call(
            community.feed,
            principal=principal,
            series_slug=series_slug,
            query=q,
            limit=limit,
            offset=offset,
        )

    @router.get("/my-posts")
    def my_posts(request: Request) -> JSONResponse:
        principal = _community_call(auth.require_authenticated, request)
        content = _community_call(community.my_posts, principal=principal)
        return JSONResponse(content, headers=no_store_headers)

    @router.post("/posts/{post_id}/unpublish")
    def unpublish(post_id: str, request: Request) -> dict[str, Any]:
        principal = authenticated_mutation(request)
        return _community_call(community.unpublish, post_id, principal=principal)

    @router.api_route("/posts/{post_id}/pdf", methods=["GET", "HEAD"])
    def pdf(post_id: str, request: Request):
        principal = request_principal(request)
        if request.method == "HEAD":
            meta = _community_call(community.head_pdf, post_id, principal=principal)
            headers = {
                "Content-Length": str(meta["total_size"]),
                "Accept-Ranges": "bytes",
                "Content-Disposition": f'inline; filename="{meta["filename"]}"',
                "X-Content-Type-Options": "nosniff",
                **no_store_headers,
            }
            return Response(status_code=200, media_type="application/pdf", headers=headers)
        range_header = request.headers.get("range", "") if request.method == "GET" else ""
        meta, stream = _community_call(
            community.open_pdf,
            post_id,
            principal=principal,
            range_header=range_header,
        )
        headers = {
            "Content-Length": str(meta["content_length"]),
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'inline; filename="{meta["filename"]}"',
            "X-Content-Type-Options": "nosniff",
            **no_store_headers,
        }
        if meta["partial"]:
            headers["Content-Range"] = (
                f'bytes {meta["start"]}-{meta["end"]}/{meta["total_size"]}'
            )
            status = 206
        else:
            status = 200
        return StreamingResponse(
            stream.iter_chunks(),
            status_code=status,
            media_type="application/pdf",
            headers=headers,
        )

    return router


def create_admin_community_router(community, auth) -> APIRouter:
    """Return the separate, explicit admin router for legacy artifact migration."""
    router = APIRouter(prefix="/api/admin/community", tags=["community-admin"])

    @router.post("/artifacts/{job_id}/bind-owner")
    def bind_legacy_artifact_owner(
        job_id: str,
        request: Request,
        payload: dict[str, Any] = Body(default={}),
    ) -> dict[str, Any]:
        principal = _community_call(auth.authenticate_request, request)
        if not principal.authenticated:
            raise HTTPException(
                status_code=401,
                detail="authentication_required",
                headers={"WWW-Authenticate": "Bearer", **_NO_STORE_HEADERS},
            )
        _community_call(auth.require_csrf, request, principal)
        if not principal.has_role("admin"):
            # A trusted authenticated refusal is still auditable; no credential or
            # request headers are copied into the event.
            try:
                community._record_binding_audit(
                    principal=principal,
                    target_user_id=str(payload.get("target_user_id") or "")[:128],
                    job_id=str(job_id or "")[:64],
                    run_id=str(payload.get("expected_run_id") or "")[:128],
                    pdf_sha256=str(payload.get("expected_pdf_sha256") or "")[:64],
                    reason=str(payload.get("reason") or "")[:1000],
                    result="admin_required",
                )
            finally:
                raise HTTPException(
                    status_code=403, detail="admin_required", headers=_NO_STORE_HEADERS)
        return _community_call(
            community.bind_legacy_artifact_owner,
            job_id,
            payload,
            principal=principal,
        )

    return router
