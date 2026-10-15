"""Job-scoped authentication handoff protected by Windows DPAPI.

Only encrypted bytes are persisted.  Access tokens are intentionally never part of
job metadata, command lines, environment variables, logs, or diagnostics.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENVELOPE_VERSION = 1
DEFAULT_TTL_SECONDS = 30 * 60
MIN_TOKEN_LIFETIME_SECONDS = 10 * 60
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{16,8192}$")
MAX_ENVELOPE_BYTES = 16 * 1024
MAX_AUTH_PAYLOAD_BYTES = 12 * 1024
_PAYLOAD_KEYS = {"version", "job_id", "created_at", "envelope_expires_at", "expires_at", "access_token", "user_id"}


class AuthContextError(RuntimeError):
    pass


def _dpapi(data: bytes, *, decrypt: bool, entropy: bytes) -> bytes:
    if os.name != "nt":
        raise AuthContextError("dpapi_unavailable")
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]
    raw = ctypes.create_string_buffer(data)
    ent = ctypes.create_string_buffer(entropy)
    inp = Blob(len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_char)))
    ent_blob = Blob(len(entropy), ctypes.cast(ent, ctypes.POINTER(ctypes.c_char)))
    out = Blob()
    crypt = ctypes.windll.crypt32
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    if decrypt:
        ok = fn(ctypes.byref(inp), None, ctypes.byref(ent_blob), None, None, 0, ctypes.byref(out))
    else:
        ok = fn(ctypes.byref(inp), "YomuSekai job auth", ctypes.byref(ent_blob), None, None, 0, ctypes.byref(out))
    if not ok:
        raise AuthContextError("dpapi_decrypt_failed" if decrypt else "dpapi_protect_failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _job_id(value: str) -> str:
    value = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", value):
        raise AuthContextError("invalid_job_id")
    return value


def _entropy(job_id: str) -> bytes:
    return hashlib.sha256(("YomuSekai.auth.v1:" + job_id).encode()).digest()


def _finite_number(value: Any, reason: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AuthContextError(reason)
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise AuthContextError(reason) from None
    if not math.isfinite(result):
        raise AuthContextError(reason)
    return result


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AuthContextError("auth_context_duplicate_field")
        result[key] = value
    return result


def _jwt_exp(token: str) -> float | None:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part).decode("utf-8"),
                             object_pairs_hook=_unique_json_object)
        if not isinstance(payload, dict) or "exp" not in payload:
            return None
        value = _finite_number(payload.get("exp"), "invalid_token_expiry")
        return value if value > 0 else None
    except (IndexError, ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeError):
        return None


@dataclass(frozen=True, repr=False)
class AuthContext:
    job_id: str
    access_token: str = ""
    expires_at: float = 0.0
    envelope_expires_at: float = 0.0
    user_id: str = ""

    def __repr__(self) -> str:  # never expose credentials in diagnostics/errors
        return (f"AuthContext(job_id={self.job_id!r}, present={bool(self.access_token)}, "
                f"expires_at={self.expires_at:.0f}, token_length={len(self.access_token)})")

    def safe_metadata(self) -> dict[str, Any]:
        return {"job_id": self.job_id, "present": bool(self.access_token),
                "expires_at": self.expires_at, "envelope_expires_at": self.envelope_expires_at,
                "token_length": len(self.access_token), "user_id": self.user_id}


class AuthEnvelopeStore:
    def __init__(self, runtime_root: Path, *, ttl_seconds: int = DEFAULT_TTL_SECONDS, clock=time.time):
        self.root = Path(runtime_root).resolve() / "auth"
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = int(ttl_seconds)
        if self.ttl_seconds < 60:
            raise ValueError("auth_envelope_ttl_too_short")
        self.clock = clock

    def _canonical_target(self, job_id: str, supplied: Path | None = None) -> Path:
        expected = self.path_for(job_id)
        target = expected if supplied is None else Path(supplied).absolute()
        try:
            if target.resolve(strict=False) != expected.resolve(strict=False):
                raise AuthContextError("auth_context_path_mismatch")
            if target.is_symlink():
                raise AuthContextError("auth_context_path_mismatch")
        except (OSError, RuntimeError):
            raise AuthContextError("auth_context_path_mismatch")
        return expected

    def path_for(self, job_id: str) -> Path:
        return self.root / (_job_id(job_id) + ".auth")

    def seal(self, job_id: str, access_token: str, *, expires_at: float | None = None,
             user_id: str = "") -> Path:
        job_id = _job_id(job_id)
        token = str(access_token or "")
        if not _TOKEN_RE.fullmatch(token):
            raise AuthContextError("invalid_access_token")
        now = _finite_number(self.clock(), "invalid_auth_clock")
        jwt_exp = _jwt_exp(token)
        token_exp = _finite_number(expires_at, "invalid_token_expiry") if expires_at is not None else (jwt_exp or 0.0)
        if jwt_exp is not None:
            token_exp = min(token_exp, jwt_exp) if token_exp else jwt_exp
        if token_exp and token_exp <= now:
            raise AuthContextError("auth_expired")
        if token_exp and token_exp - now < MIN_TOKEN_LIFETIME_SECONDS:
            raise AuthContextError("auth_refresh_required")
        envelope_exp = min(now + self.ttl_seconds, token_exp) if token_exp else now + self.ttl_seconds
        normalized_user_id = str(user_id or "")
        if len(normalized_user_id) > 200 or any(ord(ch) < 32 for ch in normalized_user_id):
            raise AuthContextError("invalid_auth_user")
        payload = {"version": ENVELOPE_VERSION, "job_id": job_id, "created_at": now,
                   "envelope_expires_at": envelope_exp, "expires_at": token_exp,
                   "access_token": token, "user_id": normalized_user_id}
        plaintext = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(plaintext) > MAX_AUTH_PAYLOAD_BYTES:
            raise AuthContextError("auth_context_too_large")
        protected = _dpapi(plaintext, decrypt=False, entropy=_entropy(job_id))
        target = self._canonical_target(job_id)
        temp = target.with_suffix(".tmp")
        temp.write_bytes(b"YSA1" + protected)
        os.replace(temp, target)
        return target

    def acquire(self, job_id: str, path: Path | None = None) -> AuthContext:
        job_id = _job_id(job_id)
        target = self._canonical_target(job_id, path)
        try:
            with target.open("rb") as handle:
                raw = handle.read(MAX_ENVELOPE_BYTES + 1)
            if len(raw) > MAX_ENVELOPE_BYTES:
                raise AuthContextError("auth_context_too_large")
            if not raw.startswith(b"YSA1") or len(raw) <= 4:
                raise AuthContextError("auth_context_corrupt")
            plaintext = _dpapi(raw[4:], decrypt=True, entropy=_entropy(job_id))
            if len(plaintext) > MAX_AUTH_PAYLOAD_BYTES:
                raise AuthContextError("auth_context_too_large")
            payload = json.loads(plaintext.decode("utf-8"), object_pairs_hook=_unique_json_object)
        except AuthContextError:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError, UnicodeError):
            raise AuthContextError("auth_context_missing_or_corrupt")
        if (not isinstance(payload, dict) or set(payload) != _PAYLOAD_KEYS
                or type(payload.get("version")) is not int or payload.get("version") != ENVELOPE_VERSION
                or payload.get("job_id") != job_id):
            raise AuthContextError("auth_context_job_mismatch")
        now = _finite_number(self.clock(), "invalid_auth_clock")
        created_at = _finite_number(payload.get("created_at"), "auth_context_corrupt")
        envelope_exp = _finite_number(payload.get("envelope_expires_at"), "auth_context_corrupt")
        if created_at <= 0 or envelope_exp <= now:
            raise AuthContextError("auth_context_expired")
        token = payload.get("access_token")
        user_id = payload.get("user_id")
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            raise AuthContextError("auth_context_corrupt")
        if not isinstance(user_id, str) or len(user_id) > 200 or any(ord(ch) < 32 for ch in user_id):
            raise AuthContextError("auth_context_corrupt")
        token_exp = _finite_number(payload.get("expires_at"), "auth_context_corrupt")
        if token_exp < 0 or (token_exp and token_exp <= now):
            raise AuthContextError("auth_expired")
        jwt_exp = _jwt_exp(token)
        if jwt_exp is not None and (not token_exp or token_exp > jwt_exp or envelope_exp > jwt_exp):
            raise AuthContextError("auth_context_corrupt")
        return AuthContext(job_id, token, token_exp, envelope_exp, user_id)

    def cleanup(self, job_id: str) -> None:
        try:
            self.path_for(job_id).unlink(missing_ok=True)
        except OSError:
            pass

    def cleanup_expired(self) -> int:
        removed = 0
        for path in self.root.glob("*.auth"):
            try:
                job_id = path.stem
                ctx = self.acquire(job_id)
                del ctx
            except AuthContextError as exc:
                if exc.args and exc.args[0] in {"auth_context_expired", "auth_expired", "auth_context_missing_or_corrupt", "auth_context_corrupt", "dpapi_decrypt_failed"}:
                    try:
                        path.unlink(); removed += 1
                    except OSError:
                        pass
        return removed


def acquire_job_auth_context(job_id: str, runtime_root: Path, path: Path | None = None) -> AuthContext:
    """Child-side convenience entrypoint; returns only an in-memory context."""
    return AuthEnvelopeStore(runtime_root).acquire(job_id, path)
