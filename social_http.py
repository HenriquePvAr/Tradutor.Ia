"""Authenticated FastAPI router for the Supabase social layer.

The browser never reaches the social tables directly: it calls these endpoints, the
backend validates the JWT (via the auth provider) and forwards the *same* user token to
the Supabase Data API, where RLS re-checks every row. Ownership is always taken from the
validated principal; owner_id/author_id/user_id/reporter_id/recipient_id in a client body
are ignored. Errors are mapped to safe categories that never leak SQL, tokens or internals.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from community_auth import AuthenticationRequired, AuthorizationDenied, RequestPrincipal
from supabase_social import SocialConfigError, SocialError

_NO_STORE = {"Cache-Control": "private, no-store", "Vary": "Authorization"}
_MUT = ("owner_id", "author_id", "user_id", "reporter_id", "recipient_id",
        "id", "role", "roles", "admin", "moderator", "actor_id", "status_forced")


def _bearer(request: Request) -> str:
    raw = str(request.headers.get("authorization", "") or "")
    scheme, _, token = raw.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="authentication_required", headers=_NO_STORE)
    return token.strip()


def create_social_router(repo, auth) -> APIRouter:
    router = APIRouter(prefix="/api/community/social", tags=["social"])

    def ctx(request: Request) -> tuple[RequestPrincipal, str]:
        try:
            principal = auth.require_authenticated(request)
        except AuthenticationRequired as exc:
            raise HTTPException(status_code=401, detail="authentication_required",
                                headers={"WWW-Authenticate": "Bearer", **_NO_STORE}) from exc
        except AuthorizationDenied as exc:
            raise HTTPException(status_code=403, detail="forbidden", headers=_NO_STORE) from exc
        return principal, _bearer(request)

    def run(fn, *args, **kwargs) -> Any:
        try:
            return fn(*args, **kwargs)
        except SocialConfigError as exc:
            raise HTTPException(status_code=503, detail="social_backend_unavailable",
                                headers=_NO_STORE) from exc
        except SocialError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.code, headers=_NO_STORE) from exc

    def clean_body(payload: Any) -> dict[str, Any]:
        if payload is None:
            return {}
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="invalid_body", headers=_NO_STORE)
        # Ownership/identity/role fields are never accepted from the client.
        if any(k in payload for k in _MUT):
            raise HTTPException(status_code=422, detail="client_identity_not_allowed", headers=_NO_STORE)
        return payload

    def ok(content: Any) -> JSONResponse:
        return JSONResponse(content, headers=_NO_STORE)

    # ---- profiles ------------------------------------------------------------
    @router.get("/profile/me")
    def get_my_profile(request: Request):
        p, t = ctx(request)
        return ok(run(repo.get_my_profile, t, p.user_id))

    @router.patch("/profile/me")
    def update_my_profile(request: Request, payload: dict = Body(default={})):
        p, t = ctx(request)
        return ok(run(repo.update_my_profile, t, p.user_id, clean_body(payload)))

    @router.get("/profiles/{username}")
    def get_profile(request: Request, username: str):
        _, t = ctx(request)
        return ok(run(repo.get_profile_by_username, t, username))

    # ---- works ---------------------------------------------------------------
    @router.get("/feed")
    def feed(request: Request, cursor: str = Query(""), limit: int = Query(20, ge=1, le=100)):
        _, t = ctx(request)
        return ok(run(repo.feed, t, cursor=cursor, limit=limit))

    @router.get("/my-works")
    def my_works(request: Request, cursor: str = Query(""), limit: int = Query(20, ge=1, le=100)):
        p, t = ctx(request)
        return ok(run(repo.my_works, t, p.user_id, cursor=cursor, limit=limit))

    @router.post("/works")
    def create_work(request: Request, payload: dict = Body(default={})):
        p, t = ctx(request)
        return ok(run(repo.create_work, t, p.user_id, clean_body(payload)))

    @router.get("/works/{work_id}")
    def get_work(request: Request, work_id: str):
        _, t = ctx(request)
        return ok(run(repo.get_work, t, work_id))

    @router.patch("/works/{work_id}")
    def update_work(request: Request, work_id: str, payload: dict = Body(default={})):
        _, t = ctx(request)
        return ok(run(repo.update_work, t, work_id, clean_body(payload)))

    @router.delete("/works/{work_id}")
    def delete_work(request: Request, work_id: str):
        _, t = ctx(request)
        run(repo.soft_delete_work, t, work_id)
        return ok({"ok": True})

    # ---- chapters ------------------------------------------------------------
    @router.get("/works/{work_id}/chapters")
    def list_chapters(request: Request, work_id: str, cursor: str = Query(""), limit: int = Query(20, ge=1, le=100)):
        _, t = ctx(request)
        return ok(run(repo.list_chapters, t, work_id, cursor=cursor, limit=limit))

    @router.post("/works/{work_id}/chapters")
    def create_chapter(request: Request, work_id: str, payload: dict = Body(default={})):
        _, t = ctx(request)
        return ok(run(repo.create_chapter, t, work_id, clean_body(payload)))

    @router.get("/chapters/{chapter_id}")
    def get_chapter(request: Request, chapter_id: str):
        _, t = ctx(request)
        return ok(run(repo.get_chapter, t, chapter_id))

    @router.patch("/chapters/{chapter_id}")
    def update_chapter(request: Request, chapter_id: str, payload: dict = Body(default={})):
        _, t = ctx(request)
        return ok(run(repo.update_chapter, t, chapter_id, clean_body(payload)))

    @router.delete("/chapters/{chapter_id}")
    def delete_chapter(request: Request, chapter_id: str):
        _, t = ctx(request)
        run(repo.soft_delete_chapter, t, chapter_id)
        return ok({"ok": True})

    # ---- comments ------------------------------------------------------------
    @router.get("/chapters/{chapter_id}/comments")
    def list_comments(request: Request, chapter_id: str, cursor: str = Query(""), limit: int = Query(20, ge=1, le=100)):
        _, t = ctx(request)
        return ok(run(repo.list_comments, t, chapter_id, cursor=cursor, limit=limit))

    @router.post("/chapters/{chapter_id}/comments")
    def create_comment(request: Request, chapter_id: str, payload: dict = Body(default={})):
        p, t = ctx(request)
        body = clean_body(payload)
        return ok(run(repo.create_comment, t, p.user_id, chapter_id,
                      body.get("content"), body.get("parent_id")))

    @router.patch("/comments/{comment_id}")
    def update_comment(request: Request, comment_id: str, payload: dict = Body(default={})):
        _, t = ctx(request)
        body = clean_body(payload)
        return ok(run(repo.update_comment, t, comment_id, body.get("content")))

    @router.delete("/comments/{comment_id}")
    def delete_comment(request: Request, comment_id: str):
        _, t = ctx(request)
        run(repo.soft_delete_comment, t, comment_id)
        return ok({"ok": True})

    # ---- likes ---------------------------------------------------------------
    @router.put("/chapters/{chapter_id}/like")
    def like_chapter(request: Request, chapter_id: str):
        p, t = ctx(request)
        run(repo.like_chapter, t, p.user_id, chapter_id)
        return ok({"liked": True})

    @router.delete("/chapters/{chapter_id}/like")
    def unlike_chapter(request: Request, chapter_id: str):
        p, t = ctx(request)
        run(repo.unlike_chapter, t, p.user_id, chapter_id)
        return ok({"liked": False})

    @router.put("/comments/{comment_id}/like")
    def like_comment(request: Request, comment_id: str):
        p, t = ctx(request)
        run(repo.like_comment, t, p.user_id, comment_id)
        return ok({"liked": True})

    @router.delete("/comments/{comment_id}/like")
    def unlike_comment(request: Request, comment_id: str):
        p, t = ctx(request)
        run(repo.unlike_comment, t, p.user_id, comment_id)
        return ok({"liked": False})

    # ---- favorites -----------------------------------------------------------
    @router.get("/favorites")
    def favorites(request: Request, cursor: str = Query(""), limit: int = Query(20, ge=1, le=100)):
        p, t = ctx(request)
        return ok(run(repo.list_favorites, t, p.user_id, cursor=cursor, limit=limit))

    @router.put("/works/{work_id}/favorite")
    def favorite(request: Request, work_id: str):
        p, t = ctx(request)
        run(repo.favorite_work, t, p.user_id, work_id)
        return ok({"favorited": True})

    @router.delete("/works/{work_id}/favorite")
    def unfavorite(request: Request, work_id: str):
        p, t = ctx(request)
        run(repo.unfavorite_work, t, p.user_id, work_id)
        return ok({"favorited": False})

    # ---- reading history -----------------------------------------------------
    @router.get("/history")
    def history(request: Request, cursor: str = Query(""), limit: int = Query(20, ge=1, le=100)):
        p, t = ctx(request)
        return ok(run(repo.get_history, t, p.user_id, cursor=cursor, limit=limit))

    @router.put("/chapters/{chapter_id}/history")
    def upsert_history(request: Request, chapter_id: str, payload: dict = Body(default={})):
        p, t = ctx(request)
        body = clean_body(payload)
        allowed = {"progress_value", "last_position", "completed"}
        if set(body) - allowed:
            raise HTTPException(status_code=422, detail="unknown_fields", headers=_NO_STORE)
        return ok(run(repo.upsert_history, t, p.user_id, chapter_id, body))

    # ---- reports -------------------------------------------------------------
    @router.get("/reports/my")
    def my_reports(request: Request, cursor: str = Query(""), limit: int = Query(20, ge=1, le=100)):
        p, t = ctx(request)
        return ok(run(repo.my_reports, t, p.user_id, cursor=cursor, limit=limit))

    @router.post("/reports")
    def create_report(request: Request, payload: dict = Body(default={})):
        p, t = ctx(request)
        body = clean_body(payload)
        if "status" in body:
            raise HTTPException(status_code=422, detail="status_not_allowed", headers=_NO_STORE)
        return ok(run(repo.create_report, t, p.user_id, body))

    # ---- notifications -------------------------------------------------------
    @router.get("/notifications")
    def notifications(request: Request, cursor: str = Query(""), limit: int = Query(20, ge=1, le=100)):
        p, t = ctx(request)
        return ok(run(repo.list_notifications, t, p.user_id, cursor=cursor, limit=limit))

    @router.patch("/notifications/{notification_id}/read")
    def mark_read(request: Request, notification_id: str):
        p, t = ctx(request)
        return ok(run(repo.mark_notification_read, t, p.user_id, notification_id))

    return router
