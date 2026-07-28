"""Explicit, loopback-only multi-identity authentication for local UI tests.

This module is inert unless selected through ``AUTH_PROVIDER=local_test`` and all
environment guards are satisfied.  It never talks to an external service.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Mapping

from community_auth import (
    AuthConfigurationError,
    AuthenticationRequired,
    AuthorizationDenied,
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    CsrfRejected,
    IssuedSession,
    RequestPrincipal,
    SESSION_COOKIE_NAME,
    bind_is_loopback,
    normalize_roles,
    normalize_user_id,
)


_ALLOWED_ENVIRONMENTS = frozenset({"test", "development"})
_ALLOWED_ASSIGNABLE_ROLES = frozenset({"user", "moderator"})
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_PASSWORD_SCHEME = "scrypt"
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_MIN_PASSWORD_LENGTH = 12
_MAX_PASSWORD_LENGTH = 256
_MAX_SECRET_LENGTH = 512


def _utc_timestamp() -> float:
    return time.time()


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


def _hash_password(password: str) -> str:
    raw = str(password or "")
    if not (_MIN_PASSWORD_LENGTH <= len(raw) <= _MAX_PASSWORD_LENGTH):
        raise ValueError("invalid_password_length")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        raw.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
        p=_SCRYPT_P, dklen=32,
    )
    return "$".join((
        _PASSWORD_SCHEME, str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P),
        _encode(salt), _encode(digest),
    ))


def _verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = str(encoded).split("$", 5)
        if scheme != _PASSWORD_SCHEME:
            return False
        digest = hashlib.scrypt(
            str(password or "").encode("utf-8"),
            salt=_decode(salt), n=int(n), r=int(r), p=int(p), dklen=32,
        )
        return hmac.compare_digest(digest, _decode(expected))
    except (TypeError, ValueError):
        return False


def _normalize_email(value: str) -> str:
    email = str(value or "").strip().lower()
    if (
        not email
        or len(email) > 254
        or email.count("@") != 1
        or any(char.isspace() for char in email)
    ):
        raise ValueError("invalid_email")
    return email


def _safe_store_path(raw_path: str) -> Path:
    value = str(raw_path or "").strip()
    if not value:
        raise AuthConfigurationError("LOCAL_TEST_AUTH_DB is required")
    path = Path(value).expanduser().resolve()
    forbidden = {"jobs.sqlite3", "community.sqlite3", "better-auth.sqlite3"}
    if path.name.lower() in forbidden or path.suffix.lower() not in {".sqlite", ".sqlite3", ".db"}:
        raise AuthConfigurationError("local test auth store must be an isolated sqlite database")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class LocalTestAuthProvider:
    """Persistent local identities with opaque, hashed server-side sessions."""

    auth_source = "local_test"
    supports_external_bind = False

    def __init__(
        self,
        *,
        db_path: str | Path,
        session_secret: str,
        session_ttl_seconds: int = 15 * 60,
        clock: Callable[[], float] = _utc_timestamp,
    ) -> None:
        secret = str(session_secret or "").encode("utf-8")
        if not (32 <= len(secret) <= _MAX_SECRET_LENGTH):
            raise AuthConfigurationError(
                "LOCAL_TEST_AUTH_SESSION_SECRET must contain 32 to 512 bytes")
        if not (60 <= int(session_ttl_seconds) <= 24 * 60 * 60):
            raise AuthConfigurationError("invalid local test session TTL")
        self.db_path = _safe_store_path(str(db_path))
        self.session_ttl_seconds = int(session_ttl_seconds)
        self._clock = clock
        self._pepper = hashlib.sha256(secret).digest()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path), timeout=5.0, isolation_level=None,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    @property
    def configured(self) -> bool:
        return True

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        clock: Callable[[], float] = _utc_timestamp,
    ) -> "LocalTestAuthProvider":
        values = os.environ if env is None else env
        if str(values.get("ALLOW_LOCAL_TEST_IDENTITIES", "")).strip() != "1":
            raise AuthConfigurationError("local test identities require explicit opt-in")
        environment = str(values.get("APP_ENV", "") or "").strip().lower()
        if environment not in _ALLOWED_ENVIRONMENTS:
            raise AuthConfigurationError("local test auth is restricted to test or development")
        bind_host = str(values.get("TRADUTOR_UI_HOST", "127.0.0.1") or "").strip()
        if not bind_is_loopback(bind_host):
            raise AuthConfigurationError("local test auth requires a loopback bind")
        external_values = (
            values.get("SUPABASE_URL"),
            values.get("SUPABASE_JWKS_URL"),
            values.get("BETTER_AUTH_INTERNAL_URL"),
        )
        if any(str(value or "").strip() for value in external_values):
            raise AuthConfigurationError(
                "external authentication configuration must be absent in local test mode")
        try:
            ttl = int(values.get("LOCAL_TEST_AUTH_SESSION_TTL_SECONDS", "900"))
        except ValueError as exc:
            raise AuthConfigurationError("invalid local test session TTL") from exc
        return cls(
            db_path=values.get("LOCAL_TEST_AUTH_DB", ""),
            session_secret=values.get("LOCAL_TEST_AUTH_SESSION_SECRET", ""),
            session_ttl_seconds=ttl,
            clock=clock,
        )

    def public_config(self) -> dict[str, object]:
        return {
            "provider": self.auth_source,
            "local_test_environment": True,
            "signup_enabled": False,
        }

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS local_test_users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active','disabled')),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS local_test_sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES local_test_users(id),
                session_token_hash BLOB NOT NULL UNIQUE,
                csrf_secret_hash BLOB NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                revoked_at REAL,
                last_seen_at REAL NOT NULL,
                user_agent_hash BLOB
            );
            CREATE TABLE IF NOT EXISTS local_test_role_assignments (
                user_id TEXT NOT NULL REFERENCES local_test_users(id),
                role TEXT NOT NULL,
                assigned_by TEXT NOT NULL,
                assigned_at REAL NOT NULL,
                revoked_at REAL,
                PRIMARY KEY(user_id, role)
            );
            CREATE TABLE IF NOT EXISTS local_test_auth_audit_events (
                id TEXT PRIMARY KEY,
                actor_id TEXT,
                subject_id TEXT,
                event_type TEXT NOT NULL,
                outcome TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_local_test_sessions_token
                ON local_test_sessions(session_token_hash);
        """)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _digest(self, value: str) -> bytes:
        return hmac.new(self._pepper, value.encode("utf-8"), hashlib.sha256).digest()

    def _audit(
        self, event_type: str, *, actor_id: str = "", subject_id: str = "",
        outcome: str = "success",
    ) -> None:
        self._conn.execute(
            "INSERT INTO local_test_auth_audit_events "
            "(id,actor_id,subject_id,event_type,outcome,created_at) VALUES(?,?,?,?,?,?)",
            (uuid.uuid4().hex, actor_id or None, subject_id or None,
             event_type, outcome, self._clock()),
        )

    def _roles_for(self, user_id: str) -> frozenset[str]:
        rows = self._conn.execute(
            "SELECT role FROM local_test_role_assignments "
            "WHERE user_id=? AND revoked_at IS NULL ORDER BY role",
            (user_id,),
        ).fetchall()
        return normalize_roles(row["role"] for row in rows)

    def bootstrap_test_identity(
        self, *, email: str, display_name: str, password: str,
    ) -> dict[str, object]:
        normalized_email = _normalize_email(email)
        name = str(display_name or "").strip()
        if not name or len(name) > 120:
            raise ValueError("invalid_display_name")
        with self._lock:
            existing = self._conn.execute(
                "SELECT id,email,display_name,status FROM local_test_users WHERE email=?",
                (normalized_email,),
            ).fetchone()
            if existing:
                return {**dict(existing), "created": False}
            now = self._clock()
            user_id = uuid.uuid4().hex
            self._conn.execute(
                "INSERT INTO local_test_users "
                "(id,email,display_name,password_hash,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (user_id, normalized_email, name, _hash_password(password),
                 "active", now, now),
            )
            self._audit("identity_created", actor_id="local_cli", subject_id=user_id)
            return {
                "id": user_id, "email": normalized_email, "display_name": name,
                "status": "active", "created": True,
            }

    def list_test_identities(self) -> list[dict[str, object]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,email,display_name,status,created_at,updated_at "
                "FROM local_test_users ORDER BY created_at,id"
            ).fetchall()
            return [{**dict(row), "roles": sorted(self._roles_for(row["id"]))} for row in rows]

    def public_identity(self, user_id: str) -> dict[str, object]:
        """Return non-sensitive display metadata for the authenticated shell."""
        normalized_user = normalize_user_id(user_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT id,display_name,status FROM local_test_users WHERE id=?",
                (normalized_user,),
            ).fetchone()
            if row is None or row["status"] != "active":
                return {}
            return {
                "id": row["id"],
                "display_name": row["display_name"],
                "roles": sorted(self._roles_for(row["id"])),
            }

    def assign_test_role(
        self, user_id: str, role: str, *, assigned_by: str,
    ) -> bool:
        normalized_user = normalize_user_id(user_id)
        normalized_role = str(role or "").strip().lower()
        if normalized_role not in _ALLOWED_ASSIGNABLE_ROLES:
            raise ValueError("role_not_assignable")
        if str(assigned_by or "") != "local_cli":
            raise ValueError("role_assignment_requires_local_cli")
        with self._lock:
            if not self._conn.execute(
                "SELECT 1 FROM local_test_users WHERE id=?", (normalized_user,)
            ).fetchone():
                raise ValueError("identity_not_found")
            now = self._clock()
            self._conn.execute(
                "INSERT INTO local_test_role_assignments "
                "(user_id,role,assigned_by,assigned_at,revoked_at) VALUES(?,?,?,?,NULL) "
                "ON CONFLICT(user_id,role) DO UPDATE SET "
                "assigned_by=excluded.assigned_by,assigned_at=excluded.assigned_at,revoked_at=NULL",
                (normalized_user, normalized_role, assigned_by, now),
            )
            self._audit(
                "role_assigned", actor_id=assigned_by, subject_id=normalized_user)
            return True

    def disable_test_identity(self, user_id: str, *, disabled_by: str) -> None:
        if str(disabled_by or "") != "local_cli":
            raise ValueError("identity_disable_requires_local_cli")
        normalized_user = normalize_user_id(user_id)
        with self._lock:
            now = self._clock()
            self._conn.execute(
                "UPDATE local_test_users SET status='disabled',updated_at=? WHERE id=?",
                (now, normalized_user),
            )
            self._conn.execute(
                "UPDATE local_test_sessions SET revoked_at=? "
                "WHERE user_id=? AND revoked_at IS NULL",
                (now, normalized_user),
            )
            self._audit(
                "identity_disabled", actor_id=disabled_by, subject_id=normalized_user)

    def _new_token(self) -> str:
        return secrets.token_urlsafe(32)

    def authenticate_credentials(
        self, *, email: str, password: str, client_host: str,
        user_agent: str = "",
    ) -> IssuedSession:
        if not bind_is_loopback(client_host):
            raise AuthorizationDenied("local_test_login_requires_loopback")
        normalized_email = _normalize_email(email)
        with self._lock:
            row = self._conn.execute(
                "SELECT id,password_hash,status FROM local_test_users WHERE email=?",
                (normalized_email,),
            ).fetchone()
            if (
                row is None
                or row["status"] != "active"
                or not _verify_password(password, row["password_hash"])
            ):
                self._audit("login", outcome="denied")
                raise AuthenticationRequired("invalid_credentials")
            now = self._clock()
            session_token = self._new_token()
            csrf_token = self._new_token()
            session_id = uuid.uuid4().hex
            expires_at = now + self.session_ttl_seconds
            agent_hash = self._digest(user_agent) if user_agent else None
            self._conn.execute(
                "INSERT INTO local_test_sessions "
                "(id,user_id,session_token_hash,csrf_secret_hash,created_at,expires_at,"
                "revoked_at,last_seen_at,user_agent_hash) VALUES(?,?,?,?,?,?,NULL,?,?)",
                (session_id, row["id"], self._digest(session_token),
                 self._digest(csrf_token), now, expires_at, now, agent_hash),
            )
            roles = self._roles_for(row["id"])
            principal = RequestPrincipal(
                user_id=row["id"], authenticated=True, roles=roles,
                auth_source=self.auth_source, session_id=session_id,
            )
            self._audit("login", actor_id=row["id"], subject_id=row["id"])
            return IssuedSession(
                session_token, csrf_token, principal,
                expires_at, self.session_ttl_seconds,
            )

    def _session_row(self, token: str):
        if not token or len(token) > _MAX_SECRET_LENGTH:
            return None
        return self._conn.execute(
            "SELECT s.*,u.status FROM local_test_sessions s "
            "JOIN local_test_users u ON u.id=s.user_id "
            "WHERE s.session_token_hash=?",
            (self._digest(token),),
        ).fetchone()

    def authenticate_request(self, request) -> RequestPrincipal:
        token = str(request.cookies.get(SESSION_COOKIE_NAME, "") or "")
        with self._lock:
            row = self._session_row(token)
            now = self._clock()
            if (
                row is None or row["revoked_at"] is not None
                or row["expires_at"] <= now or row["status"] != "active"
            ):
                return RequestPrincipal.anonymous()
            self._conn.execute(
                "UPDATE local_test_sessions SET last_seen_at=? WHERE id=?",
                (now, row["id"]),
            )
            return RequestPrincipal(
                user_id=row["user_id"], authenticated=True,
                roles=self._roles_for(row["user_id"]),
                auth_source=self.auth_source, session_id=row["id"],
            )

    def get_current_identity(self, request) -> RequestPrincipal:
        return self.authenticate_request(request)

    def get_session(self, request) -> RequestPrincipal:
        return self.authenticate_request(request)

    def refresh_session(self, request) -> RequestPrincipal:
        principal = self.require_authenticated(request)
        with self._lock:
            self._conn.execute(
                "UPDATE local_test_sessions SET expires_at=?,last_seen_at=? WHERE id=?",
                (self._clock() + self.session_ttl_seconds, self._clock(),
                 principal.session_id),
            )
        return principal

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
        if not principal.authenticated:
            raise AuthenticationRequired("authentication_required")
        session_token = str(request.cookies.get(SESSION_COOKIE_NAME, "") or "")
        csrf_cookie = str(request.cookies.get(CSRF_COOKIE_NAME, "") or "")
        csrf_header = str(request.headers.get(CSRF_HEADER_NAME, "") or "")
        with self._lock:
            row = self._session_row(session_token)
            if (
                row is None or row["id"] != principal.session_id
                or row["user_id"] != principal.user_id
                or row["revoked_at"] is not None
                or row["expires_at"] <= self._clock()
                or not csrf_cookie or not csrf_header
                or not hmac.compare_digest(self._digest(csrf_cookie), row["csrf_secret_hash"])
                or not hmac.compare_digest(self._digest(csrf_header), row["csrf_secret_hash"])
            ):
                raise CsrfRejected("csrf_invalid")

    def revoke_session(self, session_id: str | None) -> None:
        if not session_id:
            return
        with self._lock:
            now = self._clock()
            self._conn.execute(
                "UPDATE local_test_sessions SET revoked_at=? "
                "WHERE id=? AND revoked_at IS NULL",
                (now, session_id),
            )
            self._audit("logout", subject_id=str(session_id))


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m local_test_auth")
    sub = parser.add_subparsers(dest="command", required=True)
    bootstrap = sub.add_parser("bootstrap")
    bootstrap.add_argument("--credentials-output", required=True)
    assign = sub.add_parser("assign-role")
    assign.add_argument("--user", required=True)
    assign.add_argument("--role", choices=sorted(_ALLOWED_ASSIGNABLE_ROLES), required=True)
    reset = sub.add_parser("reset")
    reset.add_argument("--confirm-local-test-reset", action="store_true")
    return parser


def _write_private_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _cli_bootstrap(provider: LocalTestAuthProvider, output: Path) -> None:
    if output.exists():
        try:
            existing = json.loads(output.read_text(encoding="utf-8"))
            credentials = existing.get("identities", [])
            known_ids = {item["id"] for item in provider.list_test_identities()}
            if (
                isinstance(credentials, list)
                and len(credentials) == 3
                and all(
                    isinstance(item, dict)
                    and item.get("id") in known_ids
                    and item.get("role") in _ALLOWED_ASSIGNABLE_ROLES
                    for item in credentials
                )
            ):
                return
        except (OSError, ValueError, KeyError, TypeError):
            pass
        raise AuthConfigurationError(
            "existing credentials output is incompatible; reset the isolated store explicitly")
    specs = (
        ("member-one", "Local Member One", "user"),
        ("member-two", "Local Member Two", "user"),
        ("community-reviewer", "Local Community Reviewer", "moderator"),
    )
    credentials = []
    for local_name, display_name, role in specs:
        email = f"{local_name}@local.invalid"
        password = secrets.token_urlsafe(24)
        identity = provider.bootstrap_test_identity(
            email=email, display_name=display_name, password=password)
        if not identity["created"]:
            raise AuthConfigurationError(
                "local test identity exists without recoverable credentials; reset explicitly")
        provider.assign_test_role(identity["id"], role, assigned_by="local_cli")
        credentials.append({
            "id": identity["id"], "email": email, "password": password, "role": role,
        })
    _write_private_json(output, {"identities": credentials})


def main(argv: list[str] | None = None) -> int:
    args = _build_cli_parser().parse_args(argv)
    provider = LocalTestAuthProvider.from_env()
    try:
        if args.command == "bootstrap":
            _cli_bootstrap(provider, Path(args.credentials_output).resolve())
        elif args.command == "assign-role":
            provider.assign_test_role(args.user, args.role, assigned_by="local_cli")
        elif args.command == "reset":
            if not args.confirm_local_test_reset:
                raise SystemExit("explicit reset confirmation is required")
            provider.close()
            for suffix in ("", "-wal", "-shm"):
                Path(f"{provider.db_path}{suffix}").unlink(missing_ok=True)
            return 0
    finally:
        try:
            provider.close()
        except sqlite3.Error:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
