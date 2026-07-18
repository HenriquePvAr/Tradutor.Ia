"""Authenticated endpoints for explicit PDF publishing, asset lifecycle and protected reads.

Mounted alongside the social router. Every handler authenticates the JWT, derives ownership
from the principal, and rejects any client-supplied path/id/status field. The content
endpoint authorizes before touching Drive and never returns a Drive id, path or filename.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from community_auth import AuthenticationRequired, AuthorizationDenied, RequestPrincipal
from community_storage import StorageError
from chapter_asset_repository import AssetNotFound, ChapterAssetError
from social_asset_retention import RetentionConflict, RetentionError
from social_content import RangeNotSatisfiable
from supabase_social import SocialConfigError, SocialError

_NO_STORE = {"Cache-Control": "private, no-store", "Vary": "Authorization"}
_FORBIDDEN = ("path", "filename", "owner_id", "user_id", "actor_id", "storage_file_id",
              "drive_file_id", "file_id", "provider", "checksum", "mime_type", "role",
              "admin", "moderator", "recipient_id", "reporter_id")


def _bearer(request: Request) -> str:
    raw = str(request.headers.get("authorization", "") or "")
    scheme, _, token = raw.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="authentication_required", headers=_NO_STORE)
    return token.strip()


def create_social_pdf_router(publishing, content_service, auth, retention=None) -> APIRouter:
    router = APIRouter(prefix="/api/community/social", tags=["social-pdf"])

    def ctx(request: Request) -> tuple[RequestPrincipal, str]:
        try:
            principal = auth.require_authenticated(request)
        except AuthenticationRequired as exc:
            raise HTTPException(status_code=401, detail="authentication_required",
                                headers={"WWW-Authenticate": "Bearer", **_NO_STORE}) from exc
        except AuthorizationDenied as exc:
            raise HTTPException(status_code=403, detail="forbidden", headers=_NO_STORE) from exc
        return principal, _bearer(request)

    def run(fn, *a, **k) -> Any:
        try:
            return fn(*a, **k)
        except (AssetNotFound, SocialError) as exc:
            status = getattr(exc, "status", 404) if isinstance(exc, SocialError) else 404
            code = getattr(exc, "code", "not_found")
            raise HTTPException(status_code=status, detail=code, headers=_NO_STORE) from exc
        except RetentionConflict as exc:
            raise HTTPException(status_code=409, detail="conflict", headers=_NO_STORE) from exc
        except RetentionError as exc:
            raise HTTPException(status_code=409, detail=str(exc) or "conflict", headers=_NO_STORE) from exc
        except ChapterAssetError as exc:
            raise HTTPException(status_code=409, detail="conflict", headers=_NO_STORE) from exc
        except AuthenticationRequired as exc:
            raise HTTPException(status_code=401, detail="authentication_required", headers=_NO_STORE) from exc
        except SocialConfigError as exc:
            raise HTTPException(status_code=503, detail="social_backend_unavailable", headers=_NO_STORE) from exc

    def clean(payload: Any, allowed: set[str]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="invalid_body", headers=_NO_STORE)
        if any(k in payload for k in _FORBIDDEN):
            raise HTTPException(status_code=422, detail="client_identity_not_allowed", headers=_NO_STORE)
        if set(payload) - allowed:
            raise HTTPException(status_code=422, detail="unknown_fields", headers=_NO_STORE)
        return payload

    def ok(content_obj: Any) -> JSONResponse:
        return JSONResponse(content_obj, headers=_NO_STORE)

    # ---- publishing ----------------------------------------------------------
    @router.get("/local-pdf-results")
    def local_pdf_results(request: Request):
        p, _ = ctx(request)
        return ok(run(publishing.list_local_results, p))

    @router.post("/chapters/{chapter_id}/publish-pdf")
    def publish_pdf(request: Request, chapter_id: str, payload: dict = Body(default={})):
        p, t = ctx(request)
        body = clean(payload, {"source_job_id", "target_status", "idempotency_key"})
        return ok(run(publishing.publish_pdf, t, p, chapter_id,
                      body.get("source_job_id"), body.get("target_status"),
                      idempotency_key=str(body.get("idempotency_key") or "")))

    @router.get("/chapters/{chapter_id}/publish-status")
    def publish_status(request: Request, chapter_id: str):
        p, t = ctx(request)
        return ok(run(publishing.publish_status, t, p, chapter_id))

    # ---- asset lifecycle -----------------------------------------------------
    @router.get("/chapters/{chapter_id}/asset")
    def get_asset(request: Request, chapter_id: str):
        p, t = ctx(request)
        return ok(run(publishing.get_asset, t, p, chapter_id))

    @router.post("/chapters/{chapter_id}/asset/replace")
    def replace_asset(request: Request, chapter_id: str, payload: dict = Body(default={})):
        p, t = ctx(request)
        body = clean(payload, {"source_job_id", "idempotency_key"})
        return ok(run(publishing.replace_asset, t, p, chapter_id,
                      body.get("source_job_id"), idempotency_key=str(body.get("idempotency_key") or "")))

    @router.delete("/chapters/{chapter_id}/asset")
    def unlink_asset(request: Request, chapter_id: str):
        p, t = ctx(request)
        return ok(run(publishing.unlink_asset, t, p, chapter_id))

    # ---- retention (owner-only) ----------------------------------------------
    def _retention_or_503():
        if retention is None:
            raise HTTPException(status_code=503, detail="retention_unavailable", headers=_NO_STORE)
        return retention

    @router.get("/retained-assets")
    def retained_assets(request: Request):
        p, _ = ctx(request)
        return ok(run(_retention_or_503().list_owner_retained_assets, p.user_id))

    @router.get("/chapters/{chapter_id}/asset/retention")
    def asset_retention(request: Request, chapter_id: str):
        p, t = ctx(request)
        run(publishing.get_asset, t, p, chapter_id)  # visibility/ownership gate first
        return ok(run(_retention_or_503().get_retention_status, chapter_id, p.user_id))

    @router.post("/chapters/{chapter_id}/asset/restore")
    def restore_asset(request: Request, chapter_id: str, payload: dict = Body(default={})):
        p, t = ctx(request)
        clean(payload, set())  # no body fields accepted at all
        run(publishing.require_owner, t, p, chapter_id)
        return ok(run(_retention_or_503().restore_asset, chapter_id, p.user_id))

    # ---- protected content (HEAD/GET/Range) ----------------------------------
    def _content_headers(meta: dict[str, Any], *, partial: bool) -> dict[str, str]:
        h = {
            "Content-Length": str(meta["content_length"] if partial else meta["total_size"]),
            "Accept-Ranges": "bytes",
            "Content-Disposition": 'inline; filename="capitulo.pdf"',
            "X-Content-Type-Options": "nosniff",
            **_NO_STORE,
        }
        if partial:
            h["Content-Range"] = f'bytes {meta["start"]}-{meta["end"]}/{meta["total_size"]}'
        return h

    @router.api_route("/chapters/{chapter_id}/content", methods=["GET", "HEAD"])
    def content(request: Request, chapter_id: str):
        p, t = ctx(request)
        if request.method == "HEAD":
            meta = run(content_service.head_content, t, p, chapter_id)
            headers = {
                "Content-Length": str(meta["total_size"]),
                "Accept-Ranges": "bytes",
                "Content-Disposition": 'inline; filename="capitulo.pdf"',
                "X-Content-Type-Options": "nosniff",
                **_NO_STORE,
            }
            return Response(status_code=200, media_type="application/pdf", headers=headers)
        range_header = request.headers.get("range", "") or ""
        try:
            meta, stream = content_service.open_content(t, p, chapter_id, range_header=range_header)
        except RangeNotSatisfiable as exc:
            raise HTTPException(status_code=416, detail="range_not_satisfiable",
                                headers={"Content-Range": f"bytes */{exc.total_size}", **_NO_STORE}) from exc
        except StorageError as exc:
            raise HTTPException(status_code=503, detail="storage_unavailable", headers=_NO_STORE) from exc
        except (AssetNotFound, SocialError, AuthenticationRequired) as exc:
            status = getattr(exc, "status", 404) if isinstance(exc, SocialError) else (
                401 if isinstance(exc, AuthenticationRequired) else 404)
            raise HTTPException(status_code=status, detail="not_found", headers=_NO_STORE) from exc
        headers = _content_headers(meta, partial=meta["partial"])
        return StreamingResponse(stream.iter_chunks(), status_code=206 if meta["partial"] else 200,
                                 media_type="application/pdf", headers=headers)

    return router
