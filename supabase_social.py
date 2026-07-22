"""Server-side integration with the Supabase social Postgres via the Data API (PostgREST).

The browser never touches the social tables. Instead:

    frontend -> Tradutor.Ia backend -> user JWT -> Supabase Data API -> Postgres + RLS

Every request carries the *user's own* access token as ``Authorization: Bearer`` plus the
public ``apikey`` (publishable key). The backend holds no admin privilege: it uses no
service_role, no secret key, no database password, and never bypasses RLS. Ownership is
always derived from the validated ``RequestPrincipal`` (the JWT ``sub``) — never from a
client-supplied field. Two layers guard every call: the backend authorizes, and Postgres
re-checks with RLS using the same token.

Networking is injectable so unit tests run with a fake transport and zero real I/O; the
module performs no network access at import time.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


# ---------------------------------------------------------------------------
# Errors — mapped to stable HTTP-ish categories, never leaking SQL or tokens.
# ---------------------------------------------------------------------------
class SocialError(Exception):
    """Base error carrying a stable status and a safe, non-internal code."""

    status = 500
    code = "internal_error"

    def __init__(self, code: str | None = None, *, status: int | None = None):
        super().__init__(code or self.code)
        if code:
            self.code = code
        if status is not None:
            self.status = status


class SocialAuthRequired(SocialError):
    status = 401
    code = "authentication_required"


class SocialNotFound(SocialError):
    status = 404
    code = "not_found"


class SocialConflict(SocialError):
    status = 409
    code = "conflict"


class SocialValidationError(SocialError):
    status = 422
    code = "validation_error"


class SocialRateLimited(SocialError):
    status = 429
    code = "rate_limited"


class SocialUnavailable(SocialError):
    status = 503
    code = "social_backend_unavailable"


class SocialConfigError(RuntimeError):
    """Configuration is missing or unsafe; fail closed, never fall back silently."""


MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 100
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 20.0


@dataclass(frozen=True, slots=True)
class SocialConfig:
    """Trusted, backend-only configuration; the publishable key is public by design."""

    url: str
    publishable_key: str

    def __repr__(self) -> str:  # the key identifies the project; keep it out of logs
        return "SocialConfig(<redacted>)"

    @property
    def rest_url(self) -> str:
        return f"{self.url}/rest/v1"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SocialConfig":
        values = os.environ if env is None else env
        url = str(values.get("SUPABASE_URL", "") or "").strip().rstrip("/")
        key = str(values.get("SUPABASE_PUBLISHABLE_KEY", "") or "").strip()
        missing = [n for n, v in (("SUPABASE_URL", url), ("SUPABASE_PUBLISHABLE_KEY", key)) if not v]
        if missing:
            raise SocialConfigError(f"supabase social not configured; missing: {', '.join(missing)}")
        if not url.startswith("https://") and "127.0.0.1" not in url and "localhost" not in url:
            raise SocialConfigError("SUPABASE_URL must use https (localhost allowed only for tests)")
        return cls(url=url, publishable_key=key)


def _default_transport():
    # Reuse the audited requests transport (timeouts, TLS verify, no redirects, no retry).
    from google_drive_transport import RequestsHttpTransport

    return RequestsHttpTransport(connect_timeout=CONNECT_TIMEOUT, read_timeout=READ_TIMEOUT)


class SupabaseDataClient:
    """Minimal PostgREST client scoped to one authenticated user.

    Constructed per request with the caller's validated access token. It only ever talks
    to the configured project's ``/rest/v1`` base — never a client-supplied URL — and maps
    PostgREST failures to safe categories without echoing SQL back to the browser.
    """

    def __init__(self, config: SocialConfig, access_token: str, *, transport=None):
        if not access_token:
            raise SocialAuthRequired()
        self._config = config
        self._token = access_token
        self._transport = transport or _default_transport()

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {
            "apikey": self._config.publishable_key,
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    def _send(self, method: str, path: str, *, query: str = "",
              body: Any = None, prefer: str | None = None) -> tuple[int, Any]:
        # path/query are built by this module from constants + validated inputs, never
        # from a raw client URL. allow_redirects is off in the transport.
        url = f"{self._config.rest_url}/{path}"
        if query:
            url = f"{url}?{query}"
        extra = {}
        data = None
        if body is not None:
            extra["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        if prefer:
            extra["Prefer"] = prefer
        try:
            resp = self._transport.request(method, url, headers=self._headers(extra), data=data)
        except Exception as exc:  # transport/TLS/timeout -> unavailable, fail closed
            raise SocialUnavailable() from exc
        raw = resp.content or b""
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SocialUnavailable("social_response_too_large")
        parsed: Any = None
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                parsed = None
        self._raise_for_status(resp.status, parsed)
        return resp.status, parsed

    @staticmethod
    def _raise_for_status(status: int, parsed: Any) -> None:
        if status < 400:
            return
        # PostgREST returns {"code": "<sqlstate>", ...}; use only the class, never the text.
        sqlstate = ""
        if isinstance(parsed, dict):
            sqlstate = str(parsed.get("code") or "")
        if status == 401:
            # A rejected/absent token: genuine authentication failure.
            raise SocialAuthRequired()
        if status in (403, 404):
            # RLS denial (403) or an invisible row (404): never reveal existence — a user
            # who lacks access to someone else's content gets a uniform not-found.
            raise SocialNotFound()
        if status == 409 or sqlstate == "23505":  # unique_violation
            raise SocialConflict()
        if status == 429:
            raise SocialRateLimited()
        if status in (400, 422) or sqlstate.startswith("23") or sqlstate == "22P02":
            # integrity/constraint/invalid-input -> validation, without SQL text
            raise SocialValidationError()
        if status >= 500:
            raise SocialUnavailable()
        raise SocialError()

    # ---- verb helpers --------------------------------------------------------
    def select(self, table: str, *, query: str) -> list[dict[str, Any]]:
        _, parsed = self._send("GET", table, query=query)
        return parsed if isinstance(parsed, list) else []

    def insert(self, table: str, row: dict[str, Any], *, returning: str) -> dict[str, Any]:
        _, parsed = self._send(
            "POST", table, query=f"select={returning}", body=row,
            prefer="return=representation")
        rows = parsed if isinstance(parsed, list) else []
        if not rows:
            raise SocialUnavailable("insert_returned_no_row")
        return rows[0]

    def insert_ignore(self, table: str, row: dict[str, Any], *, on_conflict: str) -> None:
        # Idempotent insert (ON CONFLICT DO NOTHING). Needs no UPDATE policy, so it suits
        # insert/delete-only relations like likes and favorites. A repeat is a safe no-op.
        self._send("POST", table, query=f"on_conflict={on_conflict}", body=row,
                   prefer="resolution=ignore-duplicates,return=minimal")

    def upsert(self, table: str, row: dict[str, Any], *, on_conflict: str, returning: str) -> dict[str, Any]:
        _, parsed = self._send(
            "POST", table, query=f"on_conflict={on_conflict}&select={returning}", body=row,
            prefer="resolution=merge-duplicates,return=representation")
        rows = parsed if isinstance(parsed, list) else []
        if not rows:
            raise SocialUnavailable("upsert_returned_no_row")
        return rows[0]

    def update(self, table: str, *, match: str, row: dict[str, Any], returning: str) -> list[dict[str, Any]]:
        _, parsed = self._send(
            "PATCH", table, query=f"{match}&select={returning}", body=row,
            prefer="return=representation")
        return parsed if isinstance(parsed, list) else []

    def delete(self, table: str, *, match: str) -> int:
        status, parsed = self._send("DELETE", table, query=match, prefer="return=representation")
        if isinstance(parsed, list):
            return len(parsed)
        return 0 if status == 204 else 0


# ---------------------------------------------------------------------------
# DTO field whitelists — never select storage_file_id / checksum / private data.
# ---------------------------------------------------------------------------
PROFILE_FIELDS = ("id,username,display_name,display_name_normalized,bio,theme_color,"
                  "avatar_object_key,banner_object_key,public_role,pronouns,status,"
                  "status_message,accent_color,show_favorites,show_history,"
                  "allow_profile_comments,created_at,updated_at")
PROFILE_PUBLIC_FIELDS = ("id,username,display_name,bio,public_role,pronouns,status,"
                         "status_message,accent_color,created_at")
WORK_FIELDS = ("id,owner_id,title,slug,synopsis,status,created_at,updated_at,published_at")
CHAPTER_FIELDS = ("id,work_id,chapter_number,title,status,created_at,updated_at,published_at")
COMMENT_FIELDS = ("id,chapter_id,author_id,parent_id,content,moderation_status,"
                  "created_at,updated_at,edited_at,deleted_at")
FAVORITE_FIELDS = "user_id,work_id,created_at"
HISTORY_FIELDS = "user_id,chapter_id,progress_value,last_position,started_at,last_read_at,completed_at"
REPORT_FIELDS = "id,reporter_id,target_type,target_id,reason,details,status,created_at,resolved_at"
NOTIFICATION_FIELDS = ("id,recipient_id,actor_id,notification_type,entity_type,entity_id,"
                       "payload,created_at,read_at")

WORK_STATUSES = ("draft", "private", "community", "archived")
REPORT_TARGETS = ("work", "chapter", "comment", "profile")
