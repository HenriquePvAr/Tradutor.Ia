"""Trusted request principals and temporary localhost-only session authentication.

The local provider is intentionally small and server-side.  A browser receives a random
opaque cookie, while user ids and roles remain in process memory.  The interface is also
the seam for a future Supabase provider that verifies JWTs with a maintained library.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol, runtime_checkable
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request


SESSION_COOKIE_NAME = "tradutor_community_session"
CSRF_COOKIE_NAME = "tradutor_community_csrf"
CSRF_HEADER_NAME = "X-Tradutor-CSRF"
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_SESSION_TTL_SECONDS = 15 * 60
MIN_BOOTSTRAP_SECRET_LENGTH = 32
MAX_SECRET_LENGTH = 512
MAX_ACTIVE_SESSIONS = 64
MAX_ROLES = 16
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_SAFE_ROLE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,512}$")
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class AuthenticationRequired(PermissionError):
    """No valid trusted session authenticated the request."""


class AuthorizationDenied(PermissionError):
    """The authenticated principal lacks the required permission."""


class ResourceNotFound(PermissionError):
    """A resource is absent or intentionally concealed from this principal."""


class CsrfRejected(PermissionError):
    """A cookie-authenticated mutation did not prove same-origin intent."""


class AuthConfigurationError(RuntimeError):
    """Authentication or network exposure configuration is unsafe."""


def normalize_user_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_ID.fullmatch(normalized):
        raise ValueError("invalid_user_id")
    return normalized


def normalize_roles(values) -> frozenset[str]:
    if isinstance(values, str):
        values = (values,)
    elif isinstance(values, bytes):
        raise ValueError("invalid_role")
    roles = frozenset(str(value or "").strip().lower() for value in values)
    if (
        "" in roles
        or len(roles) > MAX_ROLES
        or any(not _SAFE_ROLE.fullmatch(role) for role in roles)
    ):
        raise ValueError("invalid_role")
    return roles


@dataclass(frozen=True, slots=True)
class RequestPrincipal:
    """Identity established by a trusted provider, never directly by request fields."""

    user_id: str
    authenticated: bool
    roles: frozenset[str] = field(default_factory=frozenset)
    auth_source: str = "anonymous"
    session_id: str | None = None

    def __post_init__(self) -> None:
        source = str(self.auth_source or "").strip().lower()
        if not _SAFE_ROLE.fullmatch(source):
            raise ValueError("invalid_auth_source")
        object.__setattr__(self, "auth_source", source)
        object.__setattr__(self, "roles", normalize_roles(self.roles))
        if self.authenticated:
            object.__setattr__(self, "user_id", normalize_user_id(self.user_id))
            if self.session_id is not None:
                object.__setattr__(self, "session_id", normalize_user_id(self.session_id))
        elif self.user_id or self.roles or self.session_id:
            raise ValueError("anonymous_principal_cannot_carry_identity")

    @classmethod
    def anonymous(cls) -> "RequestPrincipal":
        return cls(user_id="", authenticated=False, auth_source="anonymous")

    def has_role(self, role: str) -> bool:
        return str(role).lower() in self.roles


@runtime_checkable
class AuthProvider(Protocol):
    """Authentication seam implemented locally now and by verified JWTs in the future."""

    auth_source: str
    supports_external_bind: bool
    configured: bool

    def authenticate_request(self, request) -> RequestPrincipal: ...

    def require_authenticated(self, request) -> RequestPrincipal: ...

    def require_role(self, request, role: str) -> RequestPrincipal: ...

    def require_csrf(self, request, principal: RequestPrincipal) -> None: ...


@dataclass(slots=True)
class _SessionRecord:
    token_digest: bytes
    csrf_digest: bytes
    session_id: str
    user_id: str
    roles: frozenset[str]
    expires_at: float
    revoked: bool = False


@dataclass(frozen=True, slots=True)
class IssuedSession:
    session_token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    principal: RequestPrincipal
    expires_at: float
    ttl_seconds: int


class LocalSessionAuthProvider:
    """Ephemeral server-side sessions for an explicitly bootstrapped local operator."""

    auth_source = "local_session"
    supports_external_bind = False

    def __init__(
        self,
        *,
        bootstrap_secret: str = "",
        bootstrap_user_id: str = "",
        bootstrap_roles=(),
        session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[int], str] = secrets.token_urlsafe,
    ) -> None:
        raw_bootstrap_secret = str(bootstrap_secret or "")
        self._bootstrap_user_id = str(bootstrap_user_id or "").strip()
        self._bootstrap_roles = normalize_roles(bootstrap_roles)
        self._clock = clock
        self._token_factory = token_factory
        self._lock = threading.RLock()
        self.session_ttl_seconds = int(session_ttl_seconds)
        self._sessions: list[_SessionRecord] = []
        if self.session_ttl_seconds < 60 or self.session_ttl_seconds > 24 * 60 * 60:
            raise AuthConfigurationError("local session TTL must be between 60s and 24h")
        partial = bool(raw_bootstrap_secret) != bool(self._bootstrap_user_id)
        if partial:
            raise AuthConfigurationError("local bootstrap secret and user id must be configured together")
        encoded_secret = raw_bootstrap_secret.encode("utf-8")
        if raw_bootstrap_secret and not (
            MIN_BOOTSTRAP_SECRET_LENGTH <= len(encoded_secret) <= MAX_SECRET_LENGTH
        ):
            raise AuthConfigurationError("local bootstrap secret must contain 32 to 512 bytes")
        self._bootstrap_secret_digest = (
            hashlib.sha256(encoded_secret).digest() if raw_bootstrap_secret else b""
        )
        if self._bootstrap_user_id:
            self._bootstrap_user_id = normalize_user_id(self._bootstrap_user_id)

    @property
    def configured(self) -> bool:
        return bool(self._bootstrap_secret_digest and self._bootstrap_user_id)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "LocalSessionAuthProvider":
        values = os.environ if env is None else env
        role_text = values.get("COMMUNITY_LOCAL_BOOTSTRAP_ROLES", "")
        roles = tuple(part.strip() for part in role_text.split(",") if part.strip())
        try:
            ttl = int(values.get("COMMUNITY_LOCAL_SESSION_TTL_SECONDS", str(DEFAULT_SESSION_TTL_SECONDS)))
        except ValueError as exc:
            raise AuthConfigurationError("invalid local session TTL") from exc
        return cls(
            bootstrap_secret=values.get("COMMUNITY_LOCAL_BOOTSTRAP_SECRET", ""),
            bootstrap_user_id=values.get("COMMUNITY_LOCAL_BOOTSTRAP_USER_ID", ""),
            bootstrap_roles=roles,
            session_ttl_seconds=ttl,
        )

    @staticmethod
    def _digest(value: str) -> bytes:
        return hashlib.sha256(value.encode("utf-8")).digest()

    def _find_record(self, token: str) -> _SessionRecord | None:
        if not token or len(token) > 512:
            return None
        candidate = self._digest(token)
        match = None
        for record in self._sessions:
            if hmac.compare_digest(record.token_digest, candidate):
                match = record
        return match

    def _purge_sessions(self) -> None:
        now = self._clock()
        self._sessions = [
            record for record in self._sessions
            if not record.revoked and record.expires_at > now
        ]

    def _new_secret(self, disallowed: list[bytes]) -> tuple[str, bytes]:
        for _ in range(8):
            value = self._token_factory(32)
            if not isinstance(value, str) or not _SAFE_TOKEN.fullmatch(value):
                raise AuthConfigurationError("session token generator returned an unsafe value")
            digest = self._digest(value)
            if not any(hmac.compare_digest(digest, other) for other in disallowed):
                return value, digest
        raise AuthConfigurationError("session token generator produced repeated values")

    def issue_session(self, *, user_id: str, roles=(), ttl_seconds: int | None = None) -> IssuedSession:
        with self._lock:
            return self._issue_session(
                user_id=user_id, roles=roles, ttl_seconds=ttl_seconds)

    def _issue_session(self, *, user_id: str, roles=(),
                       ttl_seconds: int | None = None) -> IssuedSession:
        """Trusted backend seam; no HTTP request can choose these identity fields."""
        normalized_user = normalize_user_id(user_id)
        normalized_roles = normalize_roles(roles)
        ttl = self.session_ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        if ttl < 1 or ttl > 24 * 60 * 60:
            raise ValueError("invalid_session_ttl")
        self._purge_sessions()
        if len(self._sessions) >= MAX_ACTIVE_SESSIONS:
            raise AuthConfigurationError("local session limit reached")
        token, token_digest = self._new_secret([record.token_digest for record in self._sessions])
        csrf_token, csrf_digest = self._new_secret(
            [token_digest, *(record.csrf_digest for record in self._sessions)]
        )
        while True:
            session_id = secrets.token_hex(16)
            if all(record.session_id != session_id for record in self._sessions):
                break
        expires_at = self._clock() + ttl
        record = _SessionRecord(
            token_digest=token_digest,
            csrf_digest=csrf_digest,
            session_id=session_id,
            user_id=normalized_user,
            roles=normalized_roles,
            expires_at=expires_at,
        )
        self._sessions.append(record)
        principal = RequestPrincipal(
            user_id=normalized_user,
            authenticated=True,
            roles=normalized_roles,
            auth_source=self.auth_source,
            session_id=session_id,
        )
        return IssuedSession(token, csrf_token, principal, expires_at, ttl)

    def bootstrap_session(self, request, supplied_secret: str) -> IssuedSession:
        with self._lock:
            return self._bootstrap_session(request, supplied_secret)

    def _bootstrap_session(self, request, supplied_secret: str) -> IssuedSession:
        if not self.configured:
            raise AuthenticationRequired("local_bootstrap_not_configured")
        if not request_is_loopback(request):
            raise AuthorizationDenied("local_bootstrap_requires_loopback")
        supplied = str(supplied_secret or "").encode("utf-8")
        if len(supplied) > MAX_SECRET_LENGTH or not hmac.compare_digest(
            self._bootstrap_secret_digest,
            hashlib.sha256(supplied).digest(),
        ):
            raise AuthenticationRequired("invalid_local_bootstrap_secret")
        for record in self._sessions:
            if record.user_id == self._bootstrap_user_id:
                record.revoked = True
        return self.issue_session(user_id=self._bootstrap_user_id, roles=self._bootstrap_roles)

    def authenticate_request(self, request) -> RequestPrincipal:
        with self._lock:
            return self._authenticate_request(request)

    def _authenticate_request(self, request) -> RequestPrincipal:
        self._purge_sessions()
        token = str(request.cookies.get(SESSION_COOKIE_NAME, "") or "")
        record = self._find_record(token)
        if record is None or record.revoked or record.expires_at <= self._clock():
            return RequestPrincipal.anonymous()
        return RequestPrincipal(
            user_id=record.user_id,
            authenticated=True,
            roles=record.roles,
            auth_source=self.auth_source,
            session_id=record.session_id,
        )

    def require_authenticated(self, request) -> RequestPrincipal:
        principal = self.authenticate_request(request)
        if not principal.authenticated:
            raise AuthenticationRequired("authentication_required")
        return principal

    def require_role(self, request, role: str) -> RequestPrincipal:
        principal = self.require_authenticated(request)
        normalized_role = str(role or "").strip().lower()
        if normalized_role not in principal.roles:
            raise AuthorizationDenied("role_required")
        return principal

    def require_csrf(self, request, principal: RequestPrincipal) -> None:
        with self._lock:
            self._require_csrf(request, principal)

    def _require_csrf(self, request, principal: RequestPrincipal) -> None:
        if str(request.method).upper() not in _MUTATING_METHODS:
            return
        if not principal.authenticated:
            raise AuthenticationRequired("authentication_required")
        token = str(request.cookies.get(SESSION_COOKIE_NAME, "") or "")
        record = self._find_record(token)
        supplied_header = str(request.headers.get(CSRF_HEADER_NAME, "") or "")
        supplied_cookie = str(request.cookies.get(CSRF_COOKIE_NAME, "") or "")
        if (
            not record
            or record.revoked
            or record.expires_at <= self._clock()
            or principal.session_id != record.session_id
            or principal.user_id != record.user_id
            or principal.roles != record.roles
            or principal.auth_source != self.auth_source
            or not supplied_header
            or not supplied_cookie
            or len(supplied_header) > 512
            or len(supplied_cookie) > 512
        ):
            raise CsrfRejected("csrf_required")
        header_digest = self._digest(supplied_header)
        cookie_digest = self._digest(supplied_cookie)
        if not (
            hmac.compare_digest(record.csrf_digest, header_digest)
            and hmac.compare_digest(record.csrf_digest, cookie_digest)
        ):
            raise CsrfRejected("csrf_invalid")

    def revoke_session(self, session_id: str | None) -> None:
        with self._lock:
            for record in self._sessions:
                if session_id and hmac.compare_digest(record.session_id, session_id):
                    record.revoked = True


class BetterAuthProvider:
    """Canonical-session provider backed by the local Better Auth service.

    The browser never chooses identity fields for Python routes.  It presents the
    Better Auth cookie on the same origin, and Python asks the loopback-only service
    for the sanitized session contract.
    """

    auth_source = "better_auth"
    supports_external_bind = True

    def __init__(
        self,
        *,
        internal_url: str,
        public_base_path: str = "/api/auth",
        timeout_seconds: float = 5.0,
        allowed_origins: tuple[str, ...] = ("http://127.0.0.1:8080",),
    ) -> None:
        parsed = urllib_parse.urlparse(str(internal_url or "").rstrip("/"))
        if parsed.scheme != "http" or not bind_is_loopback(parsed.hostname or ""):
            raise AuthConfigurationError("Better Auth internal URL must be loopback HTTP")
        self.internal_url = urllib_parse.urlunparse(parsed)
        self.public_base_path = str(public_base_path or "/api/auth")
        self.timeout_seconds = float(timeout_seconds)
        self.allowed_origins = tuple(origin.rstrip("/") for origin in allowed_origins if origin)

    @property
    def configured(self) -> bool:
        return True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "BetterAuthProvider":
        values = os.environ if env is None else env
        host = str(values.get("TRADUTOR_AUTH_SERVICE_HOST", "127.0.0.1") or "127.0.0.1").strip()
        port = str(values.get("TRADUTOR_AUTH_SERVICE_PORT", "8787") or "8787").strip()
        base = str(values.get("BETTER_AUTH_INTERNAL_URL", f"http://{host}:{port}") or "").strip()
        ui_origin = str(values.get("BETTER_AUTH_URL", "http://127.0.0.1:8080") or "").strip().rstrip("/")
        extra = [
            value.strip().rstrip("/")
            for value in str(values.get("BETTER_AUTH_TRUSTED_ORIGINS", "") or "").split(",")
            if value.strip()
        ]
        return cls(internal_url=base, allowed_origins=tuple([ui_origin, *extra]))

    def public_config(self) -> dict[str, object]:
        return {
            "provider": self.auth_source,
            "auth_base_path": self.public_base_path,
            "google_enabled": bool(os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET")),
        }

    def _session_url(self) -> str:
        return f"{self.internal_url}/internal/auth/session"

    def _request_headers(self, request) -> dict[str, str]:
        headers: dict[str, str] = {}
        cookie = str(request.headers.get("cookie", "") or "")
        if cookie:
            headers["Cookie"] = cookie
        authorization = str(request.headers.get("authorization", "") or "")
        if authorization:
            headers["Authorization"] = authorization
        return headers

    def authenticate_request(self, request) -> RequestPrincipal:
        req = urllib_request.Request(self._session_url(), headers=self._request_headers(request))
        try:
            with urllib_request.urlopen(req, timeout=self.timeout_seconds) as response:  # noqa: S310 - loopback URL validated above
                payload = json.loads(response.read().decode("utf-8"))
        except urllib_error.HTTPError as exc:
            if exc.code == 401:
                return RequestPrincipal.anonymous()
            raise AuthenticationRequired("better_auth_session_rejected") from exc
        except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            raise AuthenticationRequired("better_auth_session_unavailable") from exc
        if not isinstance(payload, dict) or not payload.get("authenticated"):
            return RequestPrincipal.anonymous()
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        return RequestPrincipal(
            user_id=str(user.get("id") or ""),
            authenticated=True,
            roles=frozenset(),
            auth_source=self.auth_source,
            session_id=None,
        )

    def require_authenticated(self, request) -> RequestPrincipal:
        principal = self.authenticate_request(request)
        if not principal.authenticated:
            raise AuthenticationRequired("authentication_required")
        return principal

    def require_role(self, request, role: str) -> RequestPrincipal:
        principal = self.require_authenticated(request)
        if not principal.has_role(role):
            raise AuthorizationDenied("role_required")
        return principal

    def require_csrf(self, request, principal: RequestPrincipal) -> None:
        if str(request.method).upper() not in _MUTATING_METHODS:
            return
        origin = str(request.headers.get("origin", "") or "").rstrip("/")
        referer = str(request.headers.get("referer", "") or "").rstrip("/")
        if origin and origin in self.allowed_origins:
            return
        if referer:
            try:
                parsed = urllib_parse.urlparse(referer)
                candidate = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
                if candidate in self.allowed_origins:
                    return
            except ValueError:
                pass
        raise CsrfRejected("origin_required")


def build_auth_provider(
    env: Mapping[str, str] | None = None,
    *,
    clock: Callable[[], float] = time.time,
) -> AuthProvider:
    values = os.environ if env is None else env
    provider_name = str(
        values.get("AUTH_PROVIDER") or values.get("COMMUNITY_AUTH_PROVIDER") or "supabase"
    ).strip().lower()
    if provider_name == "local":
        return LocalSessionAuthProvider.from_env(values)
    if provider_name == "supabase":
        # Fail closed on incomplete configuration; never fall back to the local
        # provider silently.  Imported lazily so local-only setups need no JWT stack.
        from supabase_auth import SupabaseAuthConfig, SupabaseAuthProvider

        return SupabaseAuthProvider(SupabaseAuthConfig.from_env(values))
    if provider_name == "better_auth":
        return BetterAuthProvider.from_env(values)
    if provider_name == "local_test":
        from local_test_auth import LocalTestAuthProvider

        return LocalTestAuthProvider.from_env(values, clock=clock)
    raise AuthConfigurationError(f"unsupported community auth provider: {provider_name}")


def require_admin(principal: RequestPrincipal) -> None:
    if not principal.authenticated:
        raise AuthenticationRequired("authentication_required")
    if not principal.has_role("admin"):
        raise AuthorizationDenied("admin_required")


def require_moderator(principal: RequestPrincipal) -> None:
    if not principal.authenticated:
        raise AuthenticationRequired("authentication_required")
    if not (principal.has_role("admin") or principal.has_role("moderator")):
        raise AuthorizationDenied("moderator_required")


def set_session_cookies(response, issued: IssuedSession, *, request) -> None:
    """Set the secret session cookie and a readable double-submit CSRF cookie."""
    response.set_cookie(
        SESSION_COOKIE_NAME,
        issued.session_token,
        max_age=issued.ttl_seconds,
        path="/",
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        issued.csrf_token,
        max_age=issued.ttl_seconds,
        path="/",
        secure=request.url.scheme == "https",
        httponly=False,
        samesite="strict",
    )


def clear_session_cookies(response, *, request) -> None:
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        CSRF_COOKIE_NAME,
        path="/",
        secure=request.url.scheme == "https",
        httponly=False,
        samesite="strict",
    )


def peer_is_loopback(host: str) -> bool:
    host = str(host or "")
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def request_is_loopback(request) -> bool:
    host = str(getattr(getattr(request, "client", None), "host", "") or "")
    return peer_is_loopback(host)


def configured_bind_host(env: Mapping[str, str] | None = None) -> str:
    values = os.environ if env is None else env
    return str(values.get("TRADUTOR_UI_HOST", DEFAULT_BIND_HOST) or DEFAULT_BIND_HOST).strip()


def bind_is_loopback(host: str) -> bool:
    candidate = str(host or "").strip().lower()
    if candidate == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def external_bind_is_explicitly_allowed(
    env: Mapping[str, str] | None = None,
) -> bool:
    values = os.environ if env is None else env
    return str(values.get("TRADUTOR_ALLOW_EXTERNAL_BIND", "")).strip() == "1"


def validate_bind_security(
    host: str,
    provider: AuthProvider,
    *,
    allow_external: bool | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    if bind_is_loopback(host):
        return host
    explicit = (
        external_bind_is_explicitly_allowed(env)
        if allow_external is None
        else allow_external
    )
    if not explicit:
        raise AuthConfigurationError("external bind requires TRADUTOR_ALLOW_EXTERNAL_BIND=1")
    if not provider.configured or not provider.supports_external_bind:
        raise AuthConfigurationError("external bind requires a configured strong auth provider")
    return host
