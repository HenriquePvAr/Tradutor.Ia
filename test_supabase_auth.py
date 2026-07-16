"""Supabase JWT verification and provider selection — fully offline.

Temporary EC keys are generated in-process; a fake JWKS transport serves the public key.
No network, no real Supabase, no import-time I/O.
"""

import json
import time
import unittest
from unittest.mock import patch

import jwt as pyjwt
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

from community_auth import (
    AuthConfigurationError,
    AuthenticationRequired,
    RequestPrincipal,
    build_auth_provider,
    LocalSessionAuthProvider,
)
import supabase_auth
from supabase_auth import SupabaseAuthConfig, SupabaseAuthProvider, JwksCache

ISSUER = "https://proj.supabase.co/auth/v1"
JWKS_URL = "https://proj.supabase.co/auth/v1/.well-known/jwks.json"
AUDIENCE = "authenticated"
CONFIG = SupabaseAuthConfig(url="https://proj.supabase.co", jwks_url=JWKS_URL,
                            issuer=ISSUER, audience=AUDIENCE, publishable_key="sb_publishable_x")


def _make_key(kid: str):
    private = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(ECAlgorithm.to_jwk(private.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "ES256"})
    return private, jwk


class _Resp:
    def __init__(self, status, body):
        self.status = status
        self.content = body if isinstance(body, bytes) else json.dumps(body).encode()

    def json(self):
        return json.loads(self.content.decode())


class _FakeJwksTransport:
    """Serves a JWKS document and counts fetches; no network."""

    def __init__(self, keys, *, status=200, raw=None):
        self._doc = {"keys": keys}
        self.status = status
        self.raw = raw
        self.calls = 0

    def request(self, method, url, *, headers=None, data=None, stream=False):
        self.calls += 1
        assert url == JWKS_URL
        if self.raw is not None:
            return _Resp(self.status, self.raw)
        return _Resp(self.status, self._doc)


class _Req:
    """Minimal request stand-in with a multi-value-aware headers mapping."""

    def __init__(self, authorization=None, headers=None):
        store = {}
        if authorization is not None:
            store["authorization"] = authorization if isinstance(authorization, list) else [authorization]
        for k, v in (headers or {}).items():
            store[k.lower()] = v if isinstance(v, list) else [v]
        self.headers = _Headers(store)


class _Headers:
    def __init__(self, store):
        self._store = store

    def __contains__(self, key):
        return key.lower() in self._store

    def __getitem__(self, key):
        return self._store[key.lower()][0]

    def getlist(self, key):
        return list(self._store.get(key.lower(), []))


def _token(private, kid, *, sub="user-123", iss=ISSUER, aud=AUDIENCE,
           alg="ES256", exp_delta=3600, nbf_delta=None, extra=None, drop_sub=False):
    now = int(time.time())
    claims = {"sub": sub, "iss": iss, "aud": aud, "exp": now + exp_delta, "iat": now}
    if nbf_delta is not None:
        claims["nbf"] = now + nbf_delta
    if drop_sub:
        claims.pop("sub")
    if extra:
        claims.update(extra)
    headers = {"kid": kid}
    return pyjwt.encode(claims, private, algorithm=alg, headers=headers)


def _provider(transport):
    return SupabaseAuthProvider(CONFIG, transport=transport)


class SupabaseTokenTests(unittest.TestCase):
    def setUp(self):
        self.private, self.jwk = _make_key("kid-1")
        self.transport = _FakeJwksTransport([self.jwk])
        self.provider = _provider(self.transport)

    def _auth(self, token):
        return self.provider.authenticate_request(_Req(f"Bearer {token}"))

    def test_valid_token_builds_principal(self):
        p = self._auth(_token(self.private, "kid-1", sub="abc-123"))
        self.assertIsInstance(p, RequestPrincipal)
        self.assertTrue(p.authenticated)
        self.assertEqual(p.user_id, "abc-123")
        self.assertEqual(p.auth_source, "supabase")
        self.assertEqual(p.roles, frozenset())

    def test_invalid_signature_rejected(self):
        other, _ = _make_key("kid-1")  # different private key, same advertised kid
        with self.assertRaises(AuthenticationRequired):
            self._auth(_token(other, "kid-1"))

    def test_wrong_algorithm_rejected(self):
        # HS256 is not in the allow-list even if a token claims it.
        forged = pyjwt.encode({"sub": "x", "iss": ISSUER, "aud": AUDIENCE,
                               "exp": int(time.time()) + 60}, "secret" * 8,
                              algorithm="HS256", headers={"kid": "kid-1"})
        with self.assertRaises(AuthenticationRequired):
            self._auth(forged)

    def test_kid_missing_rejected(self):
        tok = pyjwt.encode({"sub": "x", "iss": ISSUER, "aud": AUDIENCE,
                            "exp": int(time.time()) + 60}, self.private, algorithm="ES256")
        with self.assertRaises(AuthenticationRequired):
            self._auth(tok)

    def test_unknown_kid_rejected(self):
        with self.assertRaises(AuthenticationRequired):
            self._auth(_token(self.private, "kid-unknown"))

    def test_key_rotation_refreshes_cache_once(self):
        # A new kid appears; the cache refetches once and then verifies.
        new_private, new_jwk = _make_key("kid-2")
        self.transport._doc = {"keys": [self.jwk, new_jwk]}
        calls_before = self.transport.calls
        p = self._auth(_token(new_private, "kid-2"))
        self.assertTrue(p.authenticated)
        self.assertGreater(self.transport.calls, calls_before)

    def test_unknown_kid_does_not_refresh_loop(self):
        # Repeated unknown kids must be rate limited, not a fetch per attempt.
        self.provider = _provider(self.transport)
        for _ in range(5):
            with self.assertRaises(AuthenticationRequired):
                self._auth(_token(self.private, "ghost"))
        # First get_key triggers initial fetch + one rotation fetch; subsequent are throttled.
        self.assertLessEqual(self.transport.calls, 2)

    def test_wrong_issuer_rejected(self):
        with self.assertRaises(AuthenticationRequired):
            self._auth(_token(self.private, "kid-1", iss="https://evil.example/auth/v1"))

    def test_wrong_audience_rejected(self):
        with self.assertRaises(AuthenticationRequired):
            self._auth(_token(self.private, "kid-1", aud="anon"))

    def test_expired_token_rejected(self):
        with self.assertRaises(AuthenticationRequired):
            self._auth(_token(self.private, "kid-1", exp_delta=-3600))

    def test_future_nbf_rejected(self):
        with self.assertRaises(AuthenticationRequired):
            self._auth(_token(self.private, "kid-1", nbf_delta=3600))

    def test_missing_sub_rejected(self):
        with self.assertRaises(AuthenticationRequired):
            self._auth(_token(self.private, "kid-1", drop_sub=True))

    def test_invalid_sub_rejected(self):
        with self.assertRaises(AuthenticationRequired):
            self._auth(_token(self.private, "kid-1", sub="has space!!"))

    def test_missing_authorization_is_anonymous(self):
        p = self.provider.authenticate_request(_Req())
        self.assertFalse(p.authenticated)
        self.assertEqual(p.auth_source, "anonymous")

    def test_basic_scheme_rejected(self):
        with self.assertRaises(AuthenticationRequired):
            self.provider.authenticate_request(_Req("Basic dXNlcjpwYXNz"))

    def test_empty_bearer_rejected(self):
        with self.assertRaises(AuthenticationRequired):
            self.provider.authenticate_request(_Req("Bearer "))

    def test_duplicate_authorization_headers_rejected(self):
        tok = _token(self.private, "kid-1")
        with self.assertRaises(AuthenticationRequired):
            self.provider.authenticate_request(_Req([f"Bearer {tok}", f"Bearer {tok}"]))

    def test_truncated_token_rejected(self):
        tok = _token(self.private, "kid-1")
        with self.assertRaises(AuthenticationRequired):
            self._auth(tok[: len(tok) // 2])

    def test_oversized_token_rejected(self):
        with self.assertRaises(AuthenticationRequired):
            self._auth("a.b." + "x" * 5000)

    def test_require_authenticated_raises_on_anonymous(self):
        with self.assertRaises(AuthenticationRequired):
            self.provider.require_authenticated(_Req())

    def test_forged_role_headers_are_ignored(self):
        req = _Req(f"Bearer {_token(self.private, 'kid-1', sub='u1')}",
                   headers={"X-Role": "admin", "X-Admin": "true", "X-User-Id": "root"})
        p = self.provider.authenticate_request(req)
        self.assertEqual(p.user_id, "u1")
        self.assertEqual(p.roles, frozenset())

    def test_metadata_never_grants_admin(self):
        tok = _token(self.private, "kid-1",
                     extra={"user_metadata": {"role": "admin"},
                            "raw_user_meta_data": {"is_admin": True},
                            "role": "service_role"})
        p = self._auth(tok)
        self.assertEqual(p.roles, frozenset())
        self.assertFalse(p.has_role("admin"))

    def test_session_id_claim_populates_principal(self):
        tok = _token(self.private, "kid-1", extra={"session_id": "sess-42"})
        p = self._auth(tok)
        self.assertEqual(p.session_id, "sess-42")

    def test_require_csrf_is_noop_for_bearer(self):
        # Bearer is not ambient; there is nothing to double-submit.
        self.provider.require_csrf(_Req(), RequestPrincipal.anonymous())


class JwksCacheTests(unittest.TestCase):
    def setUp(self):
        self.private, self.jwk = _make_key("kid-1")

    def test_jwks_timeout_fails_closed(self):
        class _Boom:
            def request(self, *a, **k):
                from community_storage import StorageError
                raise StorageError("http_transport_error: Timeout", transient=True)

        provider = SupabaseAuthProvider(CONFIG, transport=_Boom())
        with self.assertRaises(AuthenticationRequired):
            provider.authenticate_request(_Req(f"Bearer {_token(self.private, 'kid-1')}"))

    def test_invalid_jwks_document_fails_closed(self):
        transport = _FakeJwksTransport([], raw=b"{not json")
        provider = SupabaseAuthProvider(CONFIG, transport=transport)
        with self.assertRaises(AuthenticationRequired):
            provider.authenticate_request(_Req(f"Bearer {_token(self.private, 'kid-1')}"))

    def test_oversized_jwks_fails_closed(self):
        transport = _FakeJwksTransport([], raw=b"x" * (supabase_auth.JWKS_MAX_BYTES + 1))
        cache = JwksCache(JWKS_URL, transport=transport)
        with self.assertRaises(AuthConfigurationError):
            cache.get_key("kid-1")

    def test_cache_avoids_refetch_within_ttl(self):
        transport = _FakeJwksTransport([self.jwk])
        cache = JwksCache(JWKS_URL, transport=transport)
        cache.get_key("kid-1")
        cache.get_key("kid-1")
        self.assertEqual(transport.calls, 1)

    def test_no_network_on_import(self):
        # Building the provider must not fetch JWKS; only a request triggers it.
        transport = _FakeJwksTransport([self.jwk])
        SupabaseAuthProvider(CONFIG, transport=transport)
        self.assertEqual(transport.calls, 0)


class ProviderSelectionTests(unittest.TestCase):
    def test_local_provider_preserved(self):
        provider = build_auth_provider({
            "COMMUNITY_AUTH_PROVIDER": "local",
            "COMMUNITY_LOCAL_BOOTSTRAP_SECRET": "x" * 40,
            "COMMUNITY_LOCAL_BOOTSTRAP_USER_ID": "op",
        })
        self.assertIsInstance(provider, LocalSessionAuthProvider)

    def test_supabase_selected_when_configured(self):
        provider = build_auth_provider({
            "COMMUNITY_AUTH_PROVIDER": "supabase",
            "SUPABASE_URL": "https://proj.supabase.co",
            "SUPABASE_EXPECTED_AUDIENCE": "authenticated",
        })
        self.assertIsInstance(provider, SupabaseAuthProvider)
        self.assertEqual(provider.auth_source, "supabase")

    def test_supabase_misconfigured_fails_closed(self):
        with self.assertRaises(AuthConfigurationError):
            build_auth_provider({"COMMUNITY_AUTH_PROVIDER": "supabase"})

    def test_unknown_provider_fails_closed(self):
        with self.assertRaises(AuthConfigurationError):
            build_auth_provider({"COMMUNITY_AUTH_PROVIDER": "auth0"})

    def test_non_https_jwks_rejected(self):
        with self.assertRaises(AuthConfigurationError):
            SupabaseAuthConfig.from_env({
                "COMMUNITY_AUTH_PROVIDER": "supabase",
                "SUPABASE_URL": "https://proj.supabase.co",
                "SUPABASE_JWKS_URL": "http://insecure/jwks",
                "SUPABASE_EXPECTED_AUDIENCE": "authenticated",
            })

    def test_public_config_excludes_secret(self):
        provider = SupabaseAuthProvider(CONFIG, transport=_FakeJwksTransport([]))
        public = provider.public_config()
        blob = json.dumps(public)
        self.assertNotIn("sb_secret", blob)
        self.assertNotIn("secret", blob.lower())
        self.assertEqual(public["publishable_key"], "sb_publishable_x")

    def test_supabase_supports_external_bind_flag(self):
        provider = SupabaseAuthProvider(CONFIG, transport=_FakeJwksTransport([]))
        self.assertTrue(provider.supports_external_bind)


if __name__ == "__main__":
    unittest.main()
