"""Supabase Auth provider: cryptographic JWT verification against the project JWKS.

This provider only turns a *verified* access token into a ``RequestPrincipal``.  All
authorization decisions stay in ``community_authorization``.  The Supabase secret key is
never read here — token verification needs only the public JWKS document.

Bearer tokens are not ambient credentials: browsers never attach them automatically the
way they attach cookies, so CSRF double-submit does not apply to this provider.
"""

from __future__ import annotations

import json
import re
import threading
import time
from urllib.parse import urlparse
from dataclasses import dataclass
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
CLOCK_LEEWAY_SECONDS = 10
JWKS_MAX_BYTES = 64 * 1024
JWKS_CACHE_TTL_SECONDS = 300.0
JWKS_MIN_REFRESH_INTERVAL_SECONDS = 30.0
_JWS_COMPACT = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


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

    return RequestsHttpTransport(connect_timeout=10.0, read_timeout=10.0)


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
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at: float | None = None
        self._last_refresh_attempt: float | None = None

    def _fetch(self) -> None:
        if self._transport is None:
            self._transport = _default_transport()
        self._last_refresh_attempt = self._clock()
        try:
            response = self._transport.request("GET", self._jwks_url, headers={
                "Accept": "application/json",
            })
        except Exception as exc:  # transport-level failure: fail closed, never accept
            raise AuthConfigurationError("jwks fetch failed") from exc
        if response.status != 200:
            raise AuthConfigurationError(f"jwks fetch failed with status {response.status}")
        body = response.content or b""
        if len(body) > JWKS_MAX_BYTES:
            raise AuthConfigurationError("jwks document too large")
        try:
            document = json.loads(body.decode("utf-8"))
            raw_keys = document["keys"]
            if not isinstance(raw_keys, list):
                raise TypeError
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
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
            raise AuthConfigurationError("jwks document has no usable keys")
        self._keys = keys
        self._fetched_at = self._clock()

    def get_key(self, kid: str) -> PyJWK | None:
        with self._lock:
            now = self._clock()
            stale = self._fetched_at is None or (now - self._fetched_at) > self._ttl
            if stale:
                self._fetch()
            key = self._keys.get(kid)
            if key is not None:
                return key
            # Unknown kid: allow one rate-limited refresh for key rotation, never a
            # refresh loop driven by attacker-supplied kids.
            recently = (
                self._last_refresh_attempt is not None
                and (now - self._last_refresh_attempt) < JWKS_MIN_REFRESH_INTERVAL_SECONDS
            )
            if not recently:
                self._fetch()
                key = self._keys.get(kid)
            return key


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
        try:
            header = pyjwt.get_unverified_header(token)
        except InvalidTokenError as exc:
            raise AuthenticationRequired("invalid_token") from exc
        algorithm = header.get("alg")
        kid = header.get("kid")
        if algorithm not in ALLOWED_ALGORITHMS:
            raise AuthenticationRequired("algorithm_not_allowed")
        if not kid or not isinstance(kid, str):
            raise AuthenticationRequired("kid_missing")
        try:
            key = self._jwks.get_key(kid)
        except AuthConfigurationError as exc:
            # JWKS unavailable/invalid: fail closed as unauthenticated, never accept.
            raise AuthenticationRequired("jwks_unavailable") from exc
        if key is None:
            raise AuthenticationRequired("unknown_kid")
        try:
            claims = pyjwt.decode(
                token,
                key=key,
                algorithms=[algorithm],
                audience=self.config.audience,
                issuer=self.config.issuer,
                leeway=CLOCK_LEEWAY_SECONDS,
                options={"require": ["exp", "sub"]},
            )
        except InvalidTokenError as exc:
            # The PyJWT subclass name (InvalidAudienceError, InvalidSignatureError,
            # ExpiredSignatureError, InvalidIssuerError, ...) pinpoints the cause and
            # carries no token content, so it is safe to surface for diagnostics.
            raise AuthenticationRequired(f"token_verification_failed_{type(exc).__name__}") from exc
        return claims

    def authenticate_request(self, request) -> RequestPrincipal:
        token = self._extract_bearer(request)
        if token is None:
            return RequestPrincipal.anonymous()
        claims = self._verify(token)
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
