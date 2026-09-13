"""Supabase Auth provider: cryptographic JWT verification against the project JWKS.

This provider only turns a *verified* access token into a ``RequestPrincipal``.  All
authorization decisions stay in ``community_authorization``.  The Supabase secret key is
never read here — token verification needs only the public JWKS document.

Bearer tokens are not ambient credentials: browsers never attach them automatically the
way they attach cookies, so CSRF double-submit does not apply to this provider.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import sys
import threading
import time
import uuid
from urllib.parse import urlparse
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import jwt as pyjwt
from jwt import InvalidTokenError, PyJWK

from community_auth import (
    AuthConfigurationError,
    AuthenticationRequired,
    AuthorizationDenied,
    RequestPrincipal,
    normalize_user_id,
)

ALLOWED_ALGORITHMS = frozenset({"ES256", "RS256"})
MAX_TOKEN_LENGTH = 4096
JWT_CLOCK_SKEW_SECONDS = 30
# Backwards-compatible alias for callers/tests that imported the old name.
CLOCK_LEEWAY_SECONDS = JWT_CLOCK_SKEW_SECONDS
JWKS_MAX_BYTES = 64 * 1024
JWKS_CACHE_TTL_SECONDS = 300.0
JWKS_MIN_REFRESH_INTERVAL_SECONDS = 30.0
JWKS_CONNECT_TIMEOUT_SECONDS = 2.0
JWKS_READ_TIMEOUT_SECONDS = 4.0
JWKS_WAIT_TIMEOUT_SECONDS = 5.0
JWKS_TOTAL_DEADLINE_SECONDS = 5.0
_JWS_COMPACT = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_LOGGER = logging.getLogger(__name__)


def _emit_auth_event(event: str, **fields) -> None:
    """Emit sanitized auth timing to logger and the existing local diagnostics sink."""
    safe = {k: v for k, v in fields.items() if k not in {"token", "authorization", "cookie", "password"}}
    _LOGGER.info("%s %s", event, " ".join(f"{k}={v}" for k, v in safe.items()))
    try:
        # Never import the heavyweight UI/pipeline module synchronously from the
        # auth/deadline path.  The server imports app_ui during normal startup; if
        # it is not loaded yet, the logger remains the diagnostic sink for this
        # early event rather than extending a JWKS wall-clock deadline.
        app_ui = sys.modules.get("app_ui")
        append = getattr(app_ui, "_append_diagnostic_log", None)
        if append is not None:
            append("app_current.jsonl", event, **safe)
    except Exception:
        pass


def _jwt_validation_reason(exc: InvalidTokenError) -> str:
    """Map PyJWT failures to a safe, non-sensitive diagnostic category."""
    name = type(exc).__name__
    if name == "ExpiredSignatureError":
        return "expired"
    if name == "ImmatureSignatureError":
        return "immature"
    if name == "InvalidIssuerError":
        return "invalid_issuer"
    if name == "InvalidAudienceError":
        return "invalid_audience"
    if name == "InvalidSignatureError":
        return "invalid_signature"
    if name in {"InvalidAlgorithmError", "InvalidKeyError"}:
        return "invalid_algorithm"
    if name == "MissingRequiredClaimError" and "sub" in str(exc).lower():
        return "missing_subject"
    if name in {"DecodeError", "InvalidTokenError"}:
        return "malformed"
    return "other"


@dataclass(frozen=True, slots=True)
class SupabaseAuthConfig:
    """Trusted server-side configuration; never derived from any request field."""

    url: str
    jwks_url: str
    issuer: str
    audience: str
    publishable_key: str = ""  # public by design; safe for the browser

    def __repr__(self) -> str:  # keys/urls identify the project; keep them out of logs
        return "SupabaseAuthConfig(<redacted>)"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SupabaseAuthConfig":
        import os

        values = os.environ if env is None else env
        url = str(values.get("SUPABASE_URL", "") or "").strip().rstrip("/")
        jwks_url = str(values.get("SUPABASE_JWKS_URL", "") or "").strip()
        issuer = str(values.get("SUPABASE_EXPECTED_ISSUER", "") or "").strip()
        audience = str(values.get("SUPABASE_EXPECTED_AUDIENCE", "") or "").strip()
        publishable = str(values.get("SUPABASE_PUBLISHABLE_KEY", "") or "").strip()
        if url and not jwks_url:
            jwks_url = f"{url}/auth/v1/.well-known/jwks.json"
        if url and not issuer:
            issuer = f"{url}/auth/v1"
        missing = [
            name for name, value in (
                ("SUPABASE_URL", url),
                ("SUPABASE_JWKS_URL", jwks_url),
                ("SUPABASE_EXPECTED_ISSUER", issuer),
                ("SUPABASE_EXPECTED_AUDIENCE", audience),
            ) if not value
        ]
        if missing:
            raise AuthConfigurationError(
                f"supabase auth not configured; missing: {', '.join(missing)}"
            )
        if not jwks_url.startswith("https://"):
            raise AuthConfigurationError("supabase JWKS URL must use https")
        return cls(url=url, jwks_url=jwks_url, issuer=issuer, audience=audience,
                   publishable_key=publishable)


def _default_transport():
    # Reuse the project's audited requests transport (timeouts, TLS verification, no
    # retries).  Constructed lazily so importing this module never touches the network.
    from google_drive_transport import RequestsHttpTransport
    import requests
    session = requests.Session()
    session.trust_env = False
    adapter = requests.adapters.HTTPAdapter(max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    return RequestsHttpTransport(
        connect_timeout=JWKS_CONNECT_TIMEOUT_SECONDS,
        read_timeout=JWKS_READ_TIMEOUT_SECONDS,
        session=session,
    )


class JwksCache:
    """Fetch and cache the project's JWKS; keys are selected only by ``kid``.

    The document URL comes exclusively from trusted configuration — ``jku``/``x5u``
    headers inside a token are never honored.  An unknown ``kid`` triggers at most one
    rate-limited refresh (key rotation); everything else fails closed.
    """

    def __init__(self, jwks_url: str, *, transport=None, clock=time.monotonic,
                 ttl_seconds: float = JWKS_CACHE_TTL_SECONDS):
        self._jwks_url = jwks_url
        self._transport = transport
        self._clock = clock
        self._ttl = float(ttl_seconds)
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at: float | None = None
        self._last_refresh_attempt: float | None = None
        self._refreshing = False
        self._physical_fetch_in_flight = False
        self._generation = 0

    def _fetch(self) -> None:
        started = time.monotonic()
        self._generation += 1
        generation = self._generation
        request_trace_id = uuid.uuid4().hex[:12]
        _emit_auth_event("AUTH_SESSION_JWKS_STARTED", stage="jwks")
        if self._transport is None:
            self._transport = _default_transport()
        self._last_refresh_attempt = self._clock()
        _emit_auth_event("JWKS_HTTP_ATTEMPT_STARTED", attempt=1, proxy_used=False, retry=False)
        result: queue.Queue = queue.Queue(maxsize=1)
        def request_once() -> None:
            worker_started = time.monotonic()
            _emit_auth_event("JWKS_PHYSICAL_THREAD_ENTERED", generation=generation, request_trace_id=request_trace_id, thread_ident=threading.get_ident(), native_thread_id=threading.get_native_id())
            _emit_auth_event("JWKS_TRANSPORT_CALL_ENTERED", generation=generation, request_trace_id=request_trace_id, thread_ident=threading.get_ident())
            try:
                result.put((True, self._transport.request("GET", self._jwks_url, headers={"Accept": "application/json"})))
            except Exception as exc:
                result.put((False, exc))
            _emit_auth_event("JWKS_TRANSPORT_CALL_RETURNED", generation=generation, request_trace_id=request_trace_id, thread_ident=threading.get_ident(), wall_elapsed_ms=int((time.monotonic() - worker_started) * 1000))
            _emit_auth_event("JWKS_PHYSICAL_RESULT_READY", generation=generation, request_trace_id=request_trace_id, thread_ident=threading.get_ident())
            _emit_auth_event("JWKS_PHYSICAL_THREAD_EXITED", generation=generation, request_trace_id=request_trace_id, thread_ident=threading.get_ident(), wall_elapsed_ms=int((time.monotonic() - worker_started) * 1000))
        _emit_auth_event("JWKS_PHYSICAL_THREAD_CREATED", generation=generation, request_trace_id=request_trace_id)
        request_thread = threading.Thread(target=request_once, name="yomu-jwks-fetch", daemon=True)
        request_thread.start()
        request_thread.join(timeout=JWKS_TOTAL_DEADLINE_SECONDS)
        if request_thread.is_alive():
            frames = sys._current_frames()
            frame = frames.get(request_thread.ident)
            stack = []
            while frame is not None:
                stack.append(f"{Path(frame.f_code.co_filename).name}:{frame.f_code.co_name}:{frame.f_lineno}")
                frame = frame.f_back
            _emit_auth_event("JWKS_WORKER_STACK_AT_DEADLINE", generation=generation, request_trace_id=request_trace_id, frames=list(reversed(stack))[:32])
            _emit_auth_event("JWKS_HTTP_ATTEMPT_FINISHED", attempt=1, result="deadline_exceeded", exception_type="TimeoutError", elapsed_ms=int((time.monotonic() - started) * 1000))
            def release_when_done() -> None:
                request_thread.join()
                with self._condition:
                    self._physical_fetch_in_flight = False
                    self._refreshing = False
                    self._condition.notify_all()
            threading.Thread(target=release_when_done, name="yomu-jwks-release", daemon=True).start()
            raise AuthConfigurationError("jwks fetch deadline exceeded")
        ok, response_or_exc = result.get_nowait()
        if not ok:
            _emit_auth_event("JWKS_HTTP_ATTEMPT_FINISHED", attempt=1, result="transport_error", exception_type=type(response_or_exc).__name__, elapsed_ms=int((time.monotonic() - started) * 1000))
            with self._condition:
                self._physical_fetch_in_flight = False
                self._refreshing = False
                self._condition.notify_all()
            raise AuthConfigurationError("jwks fetch failed") from response_or_exc
        response = response_or_exc
        _emit_auth_event("JWKS_HTTP_ATTEMPT_FINISHED", attempt=1, result=f"http_{response.status}", elapsed_ms=int((time.monotonic() - started) * 1000))
        if response.status != 200:
            _emit_auth_event("AUTH_SESSION_JWKS_FINISHED", status=f"http_{response.status}", elapsed_ms=int((time.monotonic() - started) * 1000))
            with self._condition:
                self._physical_fetch_in_flight = False
                self._refreshing = False
                self._condition.notify_all()
            raise AuthConfigurationError(f"jwks fetch failed with status {response.status}")
        body = response.content or b""
        if len(body) > JWKS_MAX_BYTES:
            with self._condition:
                self._physical_fetch_in_flight = False
                self._refreshing = False
                self._condition.notify_all()
            raise AuthConfigurationError("jwks document too large")
        try:
            document = json.loads(body.decode("utf-8"))
            raw_keys = document["keys"]
            if not isinstance(raw_keys, list):
                raise TypeError
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            _emit_auth_event("AUTH_SESSION_JWKS_FINISHED", status="invalid_document", elapsed_ms=int((time.monotonic() - started) * 1000))
            with self._condition:
                self._physical_fetch_in_flight = False
                self._refreshing = False
                self._condition.notify_all()
            raise AuthConfigurationError("jwks document invalid") from exc
        keys: dict[str, PyJWK] = {}
        for raw in raw_keys:
            if not isinstance(raw, dict) or not raw.get("kid"):
                continue
            try:
                key = PyJWK.from_dict(raw)
            except InvalidTokenError:
                continue  # unsupported key types are simply not selectable
            keys[str(raw["kid"])] = key
        if not keys:
            _emit_auth_event("AUTH_SESSION_JWKS_FINISHED", status="no_usable_keys", elapsed_ms=int((time.monotonic() - started) * 1000))
            with self._condition:
                self._physical_fetch_in_flight = False
                self._refreshing = False
                self._condition.notify_all()
            raise AuthConfigurationError("jwks document has no usable keys")
        self._keys = keys
        self._fetched_at = self._clock()
        with self._condition:
            self._physical_fetch_in_flight = False
            self._refreshing = False
            self._condition.notify_all()
        _emit_auth_event("AUTH_SESSION_JWKS_FINISHED", status="ok", key_count=len(keys), elapsed_ms=int((time.monotonic() - started) * 1000))

    def get_key(self, kid: str) -> PyJWK | None:
        wait_started = time.monotonic()
        _emit_auth_event("JWKS_LOCK_WAIT_STARTED", thread_id=threading.get_ident())
        while True:
            with self._condition:
                now = self._clock()
                stale = self._fetched_at is None or (now - self._fetched_at) > self._ttl
                key = self._keys.get(kid)
                if key is not None and not stale:
                    _emit_auth_event("JWKS_LOCK_ACQUIRED", thread_id=threading.get_ident(), lock_wait_ms=int((time.monotonic() - wait_started) * 1000), cache_hit=True, refresh_owner=False)
                    return key
                if key is None and not stale and self._last_refresh_attempt is not None and (now - self._last_refresh_attempt) < JWKS_MIN_REFRESH_INTERVAL_SECONDS:
                    _emit_auth_event("JWKS_LOCK_ACQUIRED", thread_id=threading.get_ident(), lock_wait_ms=int((time.monotonic() - wait_started) * 1000), cache_hit=True, refresh_owner=False)
                    return None
                # A failed/expired initial fetch leaves no cache entry, but it
                # still establishes a refresh-attempt timestamp.  Do not let
                # waiters become sequential refresh owners immediately after
                # the physical request finishes; the same cooldown applies to
                # the no-cache state and prevents a retry stampede.
                if (self._fetched_at is None and self._last_refresh_attempt is not None
                        and (now - self._last_refresh_attempt) < JWKS_MIN_REFRESH_INTERVAL_SECONDS
                        and not self._refreshing):
                    _emit_auth_event("JWKS_LOCK_ACQUIRED", thread_id=threading.get_ident(), lock_wait_ms=int((time.monotonic() - wait_started) * 1000), cache_hit=False, refresh_owner=False, status="cooldown")
                    return None
                if self._refreshing:
                    remaining = JWKS_WAIT_TIMEOUT_SECONDS - (time.monotonic() - wait_started)
                    if remaining <= 0 or not self._condition.wait(timeout=remaining):
                        _emit_auth_event("JWKS_LOCK_ACQUIRED", thread_id=threading.get_ident(), lock_wait_ms=int((time.monotonic() - wait_started) * 1000), cache_hit=False, refresh_owner=False, status="timeout")
                        raise AuthConfigurationError("jwks refresh timeout")
                    continue
                # Claim the refresh, then release the lock before network I/O.
                self._refreshing = True
                self._physical_fetch_in_flight = True
                _emit_auth_event("JWKS_LOCK_ACQUIRED", thread_id=threading.get_ident(), lock_wait_ms=int((time.monotonic() - wait_started) * 1000), cache_hit=False, refresh_owner=True)
                break
        try:
            self._fetch()
        except Exception:
            raise
        finally:
            with self._condition:
                if not self._physical_fetch_in_flight:
                    self._refreshing = False
                    self._condition.notify_all()
        with self._condition:
            key = self._keys.get(kid)
            if key is not None:
                return key
            # Unknown kid: permit one rate-limited refresh for key rotation.
            now = self._clock()
            recently = (self._last_refresh_attempt is not None and
                        (now - self._last_refresh_attempt) < JWKS_MIN_REFRESH_INTERVAL_SECONDS)
            if recently:
                return None
            self._refreshing = True
            self._physical_fetch_in_flight = True
        try:
            self._fetch()
        finally:
            with self._condition:
                if not self._physical_fetch_in_flight:
                    self._refreshing = False
                    self._condition.notify_all()
        with self._condition:
            return self._keys.get(kid)


class SupabaseAuthProvider:
    """Authenticate requests exclusively from a verified Supabase access token."""

    auth_source = "supabase"
    supports_external_bind = True

    def __init__(self, config: SupabaseAuthConfig, *, transport=None,
                 clock=time.time) -> None:
        self.config = config
        self._clock = clock
        self._jwks = JwksCache(config.jwks_url, transport=transport)

    @property
    def configured(self) -> bool:
        return bool(self.config.url and self.config.jwks_url
                    and self.config.issuer and self.config.audience)

    def public_config(self) -> dict[str, str]:
        """Only fields designed for the browser; never the secret key or JWKS internals."""
        hostname = urlparse(self.config.url).hostname or ""
        project_ref = hostname.split(".", 1)[0] if hostname else ""
        masked_ref = f"{project_ref[:6]}…{project_ref[-4:]}" if len(project_ref) > 10 else project_ref
        return {
            "provider": "supabase",
            "supabase_url": self.config.url,
            "publishable_key": self.config.publishable_key,
            "configured": self.configured,
            "hostname": hostname,
            "project_ref": masked_ref,
            "auth_method": "email_password",
        }

    # ---- request boundary ----------------------------------------------------
    @staticmethod
    def _extract_bearer(request) -> str | None:
        headers = request.headers
        getlist = getattr(headers, "getlist", None)
        values = getlist("authorization") if getlist else (
            [headers["authorization"]] if "authorization" in headers else []
        )
        if not values:
            return None
        if len(values) > 1:
            raise AuthenticationRequired("multiple_authorization_headers")
        raw = str(values[0] or "")
        if len(raw) > MAX_TOKEN_LENGTH + 16:
            raise AuthenticationRequired("authorization_header_too_large")
        scheme, _, token = raw.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise AuthenticationRequired("invalid_authorization_scheme")
        if len(token) > MAX_TOKEN_LENGTH or not _JWS_COMPACT.fullmatch(token):
            raise AuthenticationRequired("invalid_token_format")
        return token

    def _verify(self, token: str) -> dict:
        verify_started = time.monotonic()
        _LOGGER.info("AUTH_SESSION_VERIFY_STARTED")
        _emit_auth_event("AUTH_SESSION_VERIFY_STARTED", stage="jwt")
        try:
            header = pyjwt.get_unverified_header(token)
        except InvalidTokenError as exc:
            _LOGGER.info("JWT_VALIDATION_FAILED reason=%s", _jwt_validation_reason(exc))
            raise AuthenticationRequired("invalid_token") from exc
        algorithm = header.get("alg")
        kid = header.get("kid")
        if algorithm not in ALLOWED_ALGORITHMS:
            _LOGGER.info("JWT_VALIDATION_FAILED reason=invalid_algorithm")
            raise AuthenticationRequired("algorithm_not_allowed")
        if not kid or not isinstance(kid, str):
            _LOGGER.info("JWT_VALIDATION_FAILED reason=malformed")
            raise AuthenticationRequired("kid_missing")
        try:
            key = self._jwks.get_key(kid)
        except AuthConfigurationError as exc:
            # JWKS unavailable/invalid: fail closed as unauthenticated, never accept.
            _LOGGER.info("JWT_VALIDATION_FAILED reason=other")
            raise AuthenticationRequired("jwks_unavailable") from exc
        if key is None:
            _LOGGER.info("JWT_VALIDATION_FAILED reason=invalid_signature")
            raise AuthenticationRequired("unknown_kid")
        try:
            _emit_auth_event("AUTH_SESSION_JWT_DECODE_STARTED", stage="jwt")
            claims = pyjwt.decode(
                token,
                key=key,
                algorithms=[algorithm],
                audience=self.config.audience,
                issuer=self.config.issuer,
                leeway=JWT_CLOCK_SKEW_SECONDS,
                options={
                    "require": ["exp", "iat", "sub"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_nbf": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except InvalidTokenError as exc:
            # The PyJWT subclass name (InvalidAudienceError, InvalidSignatureError,
            # ExpiredSignatureError, InvalidIssuerError, ...) pinpoints the cause and
            # carries no token content, so it is safe to surface for diagnostics.
            _LOGGER.info("JWT_VALIDATION_FAILED reason=%s", _jwt_validation_reason(exc))
            _emit_auth_event("AUTH_SESSION_JWT_DECODE_FINISHED", status="failed", reason_code=_jwt_validation_reason(exc), elapsed_ms=int((time.monotonic() - verify_started) * 1000))
            raise AuthenticationRequired(f"token_verification_failed_{type(exc).__name__}") from exc
        _emit_auth_event("AUTH_SESSION_JWT_DECODE_FINISHED", status="ok", elapsed_ms=int((time.monotonic() - verify_started) * 1000))
        _LOGGER.info("JWT_VALIDATION_SUCCESS")
        return claims

    def authenticate_request(self, request) -> RequestPrincipal:
        token = self._extract_bearer(request)
        if token is None:
            return RequestPrincipal.anonymous()
        claims = self._verify(token)
        _emit_auth_event("AUTH_SESSION_USER_RESOLUTION_STARTED", stage="claims")
        try:
            user_id = normalize_user_id(str(claims.get("sub") or ""))
        except ValueError as exc:
            raise AuthenticationRequired("invalid_subject") from exc
        session_id = None
        raw_session = str(claims.get("session_id") or "")
        if raw_session:
            try:
                session_id = normalize_user_id(raw_session)
            except ValueError:
                session_id = None
        _emit_auth_event("AUTH_SESSION_USER_RESOLUTION_FINISHED", status="ok")
        # Only the common authenticated role in this stage.  admin/moderator are never
        # granted from user-editable metadata, bodies, queries or client headers.
        return RequestPrincipal(
            user_id=user_id,
            authenticated=True,
            roles=frozenset(),
            auth_source=self.auth_source,
            session_id=session_id,
        )

    def require_authenticated(self, request) -> RequestPrincipal:
        principal = self.authenticate_request(request)
        if not principal.authenticated:
            raise AuthenticationRequired("authentication_required")
        return principal

    def require_role(self, request, role: str) -> RequestPrincipal:
        principal = self.require_authenticated(request)
        if str(role or "").strip().lower() not in principal.roles:
            raise AuthorizationDenied("role_required")
        return principal

    def require_csrf(self, request, principal: RequestPrincipal) -> None:
        # Bearer authentication is immune to classic CSRF: the browser never attaches
        # the Authorization header on its own.  Cookie-based mutations do not exist for
        # this provider, so there is nothing to double-submit.
        return None
