"""Supabase social repository: backend operations over the Data API with the user JWT.

Every ownership column (owner_id/author_id/user_id/reporter_id) is set from the trusted
principal, never from a client payload. RLS re-checks each call with the same token. DTOs
are enforced by explicit ``select=`` column lists — private data (storage_file_id,
checksums) lives in a different schema and is never selected here.
"""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Mapping

from supabase_social import (
    CHAPTER_FIELDS,
    COMMENT_FIELDS,
    DEFAULT_PAGE_LIMIT,
    FAVORITE_FIELDS,
    HISTORY_FIELDS,
    MAX_PAGE_LIMIT,
    NOTIFICATION_FIELDS,
    PROFILE_FIELDS,
    PROFILE_PUBLIC_FIELDS,
    REPORT_FIELDS,
    REPORT_TARGETS,
    SocialConfig,
    SocialConfigError,
    SocialNotFound,
    SocialValidationError,
    SupabaseDataClient,
    WORK_FIELDS,
    WORK_STATUSES,
)

_USERNAME_RE = re.compile(r"^[a-z0-9_]{3,32}$")
_DISPLAY_NAME_RE = re.compile(r"^[\w][\w .'-]{0,59}$", re.UNICODE)
_RESERVED_DISPLAY_NAMES = frozenset({"admin", "administrator", "moderator", "support", "system", "root", "official"})
_SLUG_RE = re.compile(r"^[a-z0-9-]{1,120}$")
_UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require(cond: bool, code: str) -> None:
    if not cond:
        raise SocialValidationError(code)


def _valid_uuid(value: str) -> str:
    value = str(value or "")
    if not _UUID_RE.fullmatch(value):
        raise SocialNotFound()
    return value


def encode_cursor(created_at: Any, row_id: Any) -> str:
    payload = json.dumps({"c": created_at, "i": row_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return str(payload["c"]), _valid_uuid(str(payload["i"]))
    except (ValueError, KeyError, TypeError, SocialNotFound):
        raise SocialValidationError("invalid_cursor")


def clamp_limit(limit: Any) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_LIMIT
    return max(1, min(MAX_PAGE_LIMIT, n))


def _keyset(select: str, base_filters: list[str], limit: int, cursor: str,
            *, id_col: str = "id", ts_col: str = "created_at") -> str:
    parts = [f"select={select}", *base_filters,
             f"order={ts_col}.desc,{id_col}.desc", f"limit={limit + 1}"]
    if cursor:
        ts, row_id = decode_cursor(cursor)
        parts.append(f"or=({ts_col}.lt.{ts},and({ts_col}.eq.{ts},{id_col}.lt.{row_id}))")
    return "&".join(parts)


def _page(rows: list[dict[str, Any]], limit: int, *, id_col: str = "id", ts_col: str = "created_at") -> dict[str, Any]:
    has_more = len(rows) > limit
    items = rows[:limit]
    nxt = encode_cursor(items[-1][ts_col], items[-1][id_col]) if has_more and items else None
    return {"items": items, "next_cursor": nxt}


def _clean_patch(fields: Mapping[str, Any], allowed: set[str]) -> dict[str, Any]:
    if not isinstance(fields, Mapping):
        raise SocialValidationError("invalid_body")
    if set(fields) - allowed:
        raise SocialValidationError("unknown_fields")
    return {k: v for k, v in fields.items() if k in allowed}


def _shape_comment(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    if out.get("deleted_at") is not None:
        out["content"] = ""
        out["is_deleted"] = True
    else:
        out["is_deleted"] = False
    return out


class SupabaseSocialRepository:
    """Backend social operations over the Data API. RLS is the source of truth."""

    provider = "supabase"

    def __init__(self, config: SocialConfig, *, transport=None):
        self._config = config
        self._transport = transport

    def _c(self, token: str) -> SupabaseDataClient:
        return SupabaseDataClient(self._config, token, transport=self._transport)

    # ---- profiles ------------------------------------------------------------
    def get_my_profile(self, token, user_id):
        rows = self._c(token).select(
            "profiles", query=f"select={PROFILE_FIELDS}&id=eq.{user_id}&deleted_at=is.null&limit=1")
        if not rows:
            raise SocialNotFound()
        return rows[0]

    def update_my_profile(self, token, user_id, fields):
        allowed = {"username", "display_name", "bio", "theme_color",
                   "avatar_object_key", "banner_object_key", "public_role", "pronouns",
                   "status", "status_message", "accent_color", "show_favorites",
                   "show_history", "allow_profile_comments"}
        patch = _clean_patch(fields, allowed)
        if patch.get("username") is not None and "username" in patch:
            _require(bool(_USERNAME_RE.fullmatch(str(patch["username"]))), "invalid_username")
        if patch.get("display_name") is not None:
            display_name = " ".join(str(patch["display_name"] or "").split())
            _require(bool(_DISPLAY_NAME_RE.fullmatch(display_name)), "invalid_display_name")
            _require(display_name.casefold() not in _RESERVED_DISPLAY_NAMES, "display_name_reserved")
            patch["display_name"] = display_name
        if patch.get("bio") is not None:
            _require(len(str(patch["bio"])) <= 500, "invalid_bio")
        for text_field, maximum in (("public_role", 80), ("pronouns", 30), ("status_message", 120)):
            if text_field in patch:
                _require(len(str(patch[text_field] or "")) <= maximum, f"invalid_{text_field}")
        if "status" in patch:
            _require(str(patch["status"]) in {"online", "away", "busy", "offline"}, "invalid_status")
        if "accent_color" in patch:
            _require(bool(re.fullmatch(r"#[0-9a-fA-F]{6}", str(patch["accent_color"]))), "invalid_accent_color")
        if patch.get("theme_color") is not None:
            _require(bool(re.fullmatch(r"#[0-9a-fA-F]{6}", str(patch["theme_color"]))), "invalid_theme_color")
        for boolean in ("show_favorites", "show_history", "allow_profile_comments"):
            if boolean in patch:
                _require(isinstance(patch[boolean], bool), "invalid_boolean")
        if not patch:
            return self.get_my_profile(token, user_id)
        rows = self._c(token).update(
            "profiles", match=f"id=eq.{user_id}&deleted_at=is.null", row=patch, returning=PROFILE_FIELDS)
        if not rows:
            raise SocialNotFound()
        return rows[0]

    def get_profile_by_username(self, token, username):
        _require(bool(_USERNAME_RE.fullmatch(str(username or ""))), "invalid_username")
        rows = self._c(token).select(
            "profiles", query=f"select={PROFILE_PUBLIC_FIELDS}&username=eq.{username}&deleted_at=is.null&limit=1")
        if not rows:
            raise SocialNotFound()
        return rows[0]

    # ---- works ---------------------------------------------------------------
    def feed(self, token, *, cursor="", limit=DEFAULT_PAGE_LIMIT):
        limit = clamp_limit(limit)
        q = _keyset(WORK_FIELDS, ["status=eq.community", "deleted_at=is.null"], limit, cursor)
        return _page(self._c(token).select("works", query=q), limit)

    def my_works(self, token, user_id, *, cursor="", limit=DEFAULT_PAGE_LIMIT):
        limit = clamp_limit(limit)
        q = _keyset(WORK_FIELDS, [f"owner_id=eq.{user_id}", "deleted_at=is.null"], limit, cursor)
        return _page(self._c(token).select("works", query=q), limit)

    def get_work(self, token, work_id):
        work_id = _valid_uuid(work_id)
        rows = self._c(token).select(
            "works", query=f"select={WORK_FIELDS}&id=eq.{work_id}&deleted_at=is.null&limit=1")
        if not rows:
            raise SocialNotFound()
        return rows[0]

    def create_work(self, token, user_id, fields):
        title = str(fields.get("title") or "").strip()
        slug = str(fields.get("slug") or "").strip()
        synopsis = fields.get("synopsis")
        _require(1 <= len(title) <= 200, "invalid_title")
        _require(bool(_SLUG_RE.fullmatch(slug)), "invalid_slug")
        if synopsis is not None:
            _require(len(str(synopsis)) <= 4000, "invalid_synopsis")
        row = {"owner_id": user_id, "title": title, "slug": slug, "status": "draft"}
        if synopsis is not None:
            row["synopsis"] = str(synopsis)
        return self._c(token).insert("works", row, returning=WORK_FIELDS)

    def update_work(self, token, work_id, fields):
        work_id = _valid_uuid(work_id)
        patch = _clean_patch(fields, {"title", "slug", "synopsis", "status"})
        if "title" in patch:
            _require(1 <= len(str(patch["title"] or "")) <= 200, "invalid_title")
        if "slug" in patch:
            _require(bool(_SLUG_RE.fullmatch(str(patch["slug"] or ""))), "invalid_slug")
        if patch.get("synopsis") is not None:
            _require(len(str(patch["synopsis"])) <= 4000, "invalid_synopsis")
        if "status" in patch:
            _require(str(patch["status"]) in WORK_STATUSES, "invalid_status")
            if patch["status"] == "community":
                patch["published_at"] = _now_iso()
        _require(bool(patch), "empty_update")
        rows = self._c(token).update(
            "works", match=f"id=eq.{work_id}&deleted_at=is.null", row=patch, returning=WORK_FIELDS)
        if not rows:
            raise SocialNotFound()
        return rows[0]

    def soft_delete_work(self, token, work_id):
        work_id = _valid_uuid(work_id)
        rows = self._c(token).update(
            "works", match=f"id=eq.{work_id}&deleted_at=is.null",
            row={"deleted_at": _now_iso()}, returning="id")
        if not rows:
            raise SocialNotFound()

    # ---- chapters ------------------------------------------------------------
    def list_chapters(self, token, work_id, *, cursor="", limit=DEFAULT_PAGE_LIMIT):
        work_id = _valid_uuid(work_id)
        limit = clamp_limit(limit)
        q = _keyset(CHAPTER_FIELDS, [f"work_id=eq.{work_id}", "deleted_at=is.null"], limit, cursor)
        return _page(self._c(token).select("chapters", query=q), limit)

    def get_chapter(self, token, chapter_id):
        chapter_id = _valid_uuid(chapter_id)
        rows = self._c(token).select(
            "chapters", query=f"select={CHAPTER_FIELDS}&id=eq.{chapter_id}&deleted_at=is.null&limit=1")
        if not rows:
            raise SocialNotFound()
        return rows[0]

    def create_chapter(self, token, work_id, fields):
        work_id = _valid_uuid(work_id)
        try:
            number = float(fields.get("chapter_number"))
        except (TypeError, ValueError):
            raise SocialValidationError("invalid_chapter_number")
        _require(number > 0, "invalid_chapter_number")
        row = {"work_id": work_id, "chapter_number": number, "status": "draft"}
        if fields.get("title") is not None:
            _require(len(str(fields["title"])) <= 200, "invalid_title")
            row["title"] = str(fields["title"])
        return self._c(token).insert("chapters", row, returning=CHAPTER_FIELDS)

    def update_chapter(self, token, chapter_id, fields):
        chapter_id = _valid_uuid(chapter_id)
        patch = _clean_patch(fields, {"title", "status", "chapter_number"})
        if patch.get("title") is not None:
            _require(len(str(patch["title"])) <= 200, "invalid_title")
        if "chapter_number" in patch:
            try:
                patch["chapter_number"] = float(patch["chapter_number"])
            except (TypeError, ValueError):
                raise SocialValidationError("invalid_chapter_number")
            _require(patch["chapter_number"] > 0, "invalid_chapter_number")
        if "status" in patch:
            _require(str(patch["status"]) in WORK_STATUSES, "invalid_status")
            if patch["status"] == "community":
                patch["published_at"] = _now_iso()
        _require(bool(patch), "empty_update")
        rows = self._c(token).update(
            "chapters", match=f"id=eq.{chapter_id}&deleted_at=is.null", row=patch, returning=CHAPTER_FIELDS)
        if not rows:
            raise SocialNotFound()
        return rows[0]

    def soft_delete_chapter(self, token, chapter_id):
        chapter_id = _valid_uuid(chapter_id)
        rows = self._c(token).update(
            "chapters", match=f"id=eq.{chapter_id}&deleted_at=is.null",
            row={"deleted_at": _now_iso()}, returning="id")
        if not rows:
            raise SocialNotFound()

    # ---- comments ------------------------------------------------------------
    def list_comments(self, token, chapter_id, *, cursor="", limit=DEFAULT_PAGE_LIMIT):
        chapter_id = _valid_uuid(chapter_id)
        limit = clamp_limit(limit)
        q = _keyset(COMMENT_FIELDS, [f"chapter_id=eq.{chapter_id}"], limit, cursor)
        rows = self._c(token).select("comments", query=q)
        page = _page(rows, limit)
        page["items"] = [_shape_comment(r) for r in page["items"]]
        return page

    def create_comment(self, token, user_id, chapter_id, content, parent_id):
        chapter_id = _valid_uuid(chapter_id)
        content = str(content or "")
        _require(1 <= len(content) <= 4000, "invalid_content")
        row = {"chapter_id": chapter_id, "author_id": user_id, "content": content}
        if parent_id:
            row["parent_id"] = _valid_uuid(parent_id)
        return _shape_comment(self._c(token).insert("comments", row, returning=COMMENT_FIELDS))

    def update_comment(self, token, comment_id, content):
        comment_id = _valid_uuid(comment_id)
        content = str(content or "")
        _require(1 <= len(content) <= 4000, "invalid_content")
        rows = self._c(token).update(
            "comments", match=f"id=eq.{comment_id}&deleted_at=is.null",
            row={"content": content}, returning=COMMENT_FIELDS)
        if not rows:
            raise SocialNotFound()
        return _shape_comment(rows[0])

    def soft_delete_comment(self, token, comment_id):
        comment_id = _valid_uuid(comment_id)
        rows = self._c(token).update(
            "comments", match=f"id=eq.{comment_id}&deleted_at=is.null",
            row={"deleted_at": _now_iso()}, returning="id")
        if not rows:
            raise SocialNotFound()

    # ---- likes (idempotent) --------------------------------------------------
    def like_chapter(self, token, user_id, chapter_id):
        chapter_id = _valid_uuid(chapter_id)
        self._c(token).insert_ignore("chapter_likes", {"chapter_id": chapter_id, "user_id": user_id},
                                     on_conflict="chapter_id,user_id")

    def unlike_chapter(self, token, user_id, chapter_id):
        chapter_id = _valid_uuid(chapter_id)
        self._c(token).delete("chapter_likes", match=f"chapter_id=eq.{chapter_id}&user_id=eq.{user_id}")

    def like_comment(self, token, user_id, comment_id):
        comment_id = _valid_uuid(comment_id)
        self._c(token).insert_ignore("comment_likes", {"comment_id": comment_id, "user_id": user_id},
                                     on_conflict="comment_id,user_id")

    def unlike_comment(self, token, user_id, comment_id):
        comment_id = _valid_uuid(comment_id)
        self._c(token).delete("comment_likes", match=f"comment_id=eq.{comment_id}&user_id=eq.{user_id}")

    # ---- favorites -----------------------------------------------------------
    def list_favorites(self, token, user_id, *, cursor="", limit=DEFAULT_PAGE_LIMIT):
        limit = clamp_limit(limit)
        parts = [f"select={FAVORITE_FIELDS}", f"user_id=eq.{user_id}",
                 "order=created_at.desc,work_id.desc", f"limit={limit + 1}"]
        if cursor:
            ts, row_id = decode_cursor(cursor)
            parts.append(f"or=(created_at.lt.{ts},and(created_at.eq.{ts},work_id.lt.{row_id}))")
        rows = self._c(token).select("favorites", query="&".join(parts))
        return _page(rows, limit, id_col="work_id")

    def favorite_work(self, token, user_id, work_id):
        work_id = _valid_uuid(work_id)
        self._c(token).insert_ignore("favorites", {"user_id": user_id, "work_id": work_id},
                                     on_conflict="user_id,work_id")

    def unfavorite_work(self, token, user_id, work_id):
        work_id = _valid_uuid(work_id)
        self._c(token).delete("favorites", match=f"user_id=eq.{user_id}&work_id=eq.{work_id}")

    # ---- reading history -----------------------------------------------------
    def get_history(self, token, user_id, *, cursor="", limit=DEFAULT_PAGE_LIMIT):
        limit = clamp_limit(limit)
        parts = [f"select={HISTORY_FIELDS}", f"user_id=eq.{user_id}",
                 "order=last_read_at.desc,chapter_id.desc", f"limit={limit + 1}"]
        if cursor:
            ts, row_id = decode_cursor(cursor)
            parts.append(f"or=(last_read_at.lt.{ts},and(last_read_at.eq.{ts},chapter_id.lt.{row_id}))")
        rows = self._c(token).select("reading_history", query="&".join(parts))
        return _page(rows, limit, id_col="chapter_id", ts_col="last_read_at")

    def upsert_history(self, token, user_id, chapter_id, body):
        chapter_id = _valid_uuid(chapter_id)
        try:
            progress = float(body.get("progress_value"))
        except (TypeError, ValueError):
            raise SocialValidationError("invalid_progress")
        _require(0 <= progress <= 100, "invalid_progress")
        row = {"user_id": user_id, "chapter_id": chapter_id, "progress_value": progress,
               "last_read_at": _now_iso()}
        if body.get("last_position") is not None:
            _require(len(str(body["last_position"])) <= 200, "invalid_last_position")
            row["last_position"] = str(body["last_position"])
        if bool(body.get("completed")):
            row["completed_at"] = _now_iso()
        return self._c(token).upsert("reading_history", row,
                                     on_conflict="user_id,chapter_id", returning=HISTORY_FIELDS)

    # ---- reports -------------------------------------------------------------
    def my_reports(self, token, user_id, *, cursor="", limit=DEFAULT_PAGE_LIMIT):
        limit = clamp_limit(limit)
        q = _keyset(REPORT_FIELDS, [f"reporter_id=eq.{user_id}"], limit, cursor)
        return _page(self._c(token).select("reports", query=q), limit)

    def create_report(self, token, user_id, body):
        target_type = str(body.get("target_type") or "")
        _require(target_type in REPORT_TARGETS, "invalid_target_type")
        target_id = _valid_uuid(str(body.get("target_id") or ""))
        reason = str(body.get("reason") or "").strip()
        _require(1 <= len(reason) <= 100, "invalid_reason")
        row = {"reporter_id": user_id, "target_type": target_type, "target_id": target_id,
               "reason": reason, "status": "open"}
        if body.get("details") is not None:
            _require(len(str(body["details"])) <= 2000, "invalid_details")
            row["details"] = str(body["details"])
        return self._c(token).insert("reports", row, returning=REPORT_FIELDS)

    # ---- notifications -------------------------------------------------------
    def list_notifications(self, token, user_id, *, cursor="", limit=DEFAULT_PAGE_LIMIT):
        limit = clamp_limit(limit)
        q = _keyset(NOTIFICATION_FIELDS, [f"recipient_id=eq.{user_id}", "deleted_at=is.null"], limit, cursor)
        return _page(self._c(token).select("notifications", query=q), limit)

    def mark_notification_read(self, token, user_id, notification_id):
        notification_id = _valid_uuid(notification_id)
        rows = self._c(token).update(
            "notifications",
            match=f"id=eq.{notification_id}&recipient_id=eq.{user_id}&deleted_at=is.null",
            row={"read_at": _now_iso()}, returning=NOTIFICATION_FIELDS)
        if not rows:
            raise SocialNotFound()
        return rows[0]


def build_social_repository(env: Mapping[str, str] | None = None, *, transport=None):
    """Select the social provider explicitly; fail closed on unknown/misconfigured.

    ``supabase`` is the real implementation. ``local`` is recognised but not implemented in
    this phase (the social entities have no pre-existing local store; the existing SQLite
    PDF-community is separate and untouched). It fails closed rather than silently falling
    back — unit tests exercise the Supabase repository through an injected fake transport,
    and RLS is validated end-to-end against the local Supabase Postgres.
    """
    values = os.environ if env is None else env
    provider = str(values.get("COMMUNITY_SOCIAL_PROVIDER", "supabase") or "supabase").strip().lower()
    if provider == "supabase":
        return SupabaseSocialRepository(SocialConfig.from_env(values), transport=transport)
    if provider == "local":
        raise SocialConfigError("local social provider is not implemented in this phase")
    raise SocialConfigError(f"unsupported community social provider: {provider}")
