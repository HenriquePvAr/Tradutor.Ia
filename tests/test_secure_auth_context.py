import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from yomu.auth import secure_auth_context as auth_module
from yomu.auth.secure_auth_context import AuthContextError, AuthEnvelopeStore


def token(exp: int | float, *, canary: str = "") -> str:
    head = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    claims = {"exp": exp, "sub": "user-a"}
    if canary:
        claims["private_canary"] = canary
    body = base64.urlsafe_b64encode(json.dumps(claims, allow_nan=True).encode()).decode().rstrip("=")
    return f"{head}.{body}.signature-for-tests"


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI required")
def test_dpapi_binding_ttl_tamper_and_cleanup(tmp_path: Path):
    store = AuthEnvelopeStore(tmp_path, ttl_seconds=1800)
    secret = "YOMU_AUTH_SECRET_MUST_NOT_LEAK_123456"
    access_token = token(int(time.time()) + 3600, canary=secret)
    path = store.seal("job-a", access_token, user_id="user-a")
    assert secret.encode() not in path.read_bytes()
    assert access_token.encode() not in path.read_bytes()
    assert json.dumps({"access_token": access_token}).encode() not in path.read_bytes()
    ctx = store.acquire("job-a")
    assert ctx.safe_metadata()["present"] is True
    assert access_token not in repr(ctx)
    assert access_token not in json.dumps(ctx.safe_metadata())
    assert secret not in repr(ctx)
    with pytest.raises(AuthContextError):
        store.acquire("job-b", path)
    original = path.read_bytes()
    path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    with pytest.raises(AuthContextError):
        store.acquire("job-a")
    path.write_bytes(original[:10])
    with pytest.raises(AuthContextError):
        store.acquire("job-a")
    path.write_bytes(original)
    store.cleanup("job-a")
    with pytest.raises(AuthContextError):
        store.acquire("job-a")


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI required")
def test_expired_envelope_and_jwt_rejected(tmp_path: Path):
    now = int(time.time())
    store = AuthEnvelopeStore(tmp_path, ttl_seconds=1800, clock=lambda: now)
    with pytest.raises(AuthContextError, match="auth_refresh_required"):
        store.seal("job-a", token(now + 30))
    store = AuthEnvelopeStore(tmp_path, ttl_seconds=60, clock=lambda: now)
    path = store.seal("job-a", token(now + 3600))
    expired = AuthEnvelopeStore(tmp_path, ttl_seconds=60, clock=lambda: now + 61)
    with pytest.raises(AuthContextError, match="expired"):
        expired.acquire("job-a", path)


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI required")
def test_real_process_boundary(tmp_path: Path):
    store = AuthEnvelopeStore(tmp_path, ttl_seconds=1800)
    canary = "SUBPROCESS_AUTH_CANARY_MUST_STAY_PRIVATE_98765"
    access_token = token(int(time.time()) + 3600, canary=canary)
    path = store.seal("job-process", access_token)
    code = (
        "from yomu.auth.secure_auth_context import AuthEnvelopeStore; "
        f"s=AuthEnvelopeStore(r'{tmp_path}'); c=s.acquire('job-process'); "
        "print(c.safe_metadata()['present'])"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "True"
    assert str(path) not in result.stdout
    assert canary not in result.stdout + result.stderr
    assert access_token not in result.stdout + result.stderr


def _identity_dpapi(monkeypatch):
    monkeypatch.setattr(auth_module, "_dpapi", lambda data, *, decrypt, entropy: data)


def test_explicit_envelope_path_must_be_canonical_and_bounded(tmp_path: Path, monkeypatch):
    _identity_dpapi(monkeypatch)
    store_a = AuthEnvelopeStore(tmp_path / "a")
    store_b = AuthEnvelopeStore(tmp_path / "b")
    access_token = token(time.time() + 3600)
    path = store_a.seal("job-a", access_token)
    assert store_a.acquire("job-a", path).access_token == access_token
    with pytest.raises(AuthContextError, match="path_mismatch"):
        store_b.acquire("job-a", path)

    too_large = store_a.path_for("job-large")
    too_large.write_bytes(b"YSA1" + b"x" * auth_module.MAX_ENVELOPE_BYTES)
    with pytest.raises(AuthContextError, match="too_large"):
        store_a.acquire("job-large")


@pytest.mark.parametrize("clock", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_clock_and_expiry_are_rejected(tmp_path: Path, monkeypatch, clock):
    _identity_dpapi(monkeypatch)
    with pytest.raises(AuthContextError, match="invalid_auth_clock"):
        AuthEnvelopeStore(tmp_path / "clock", clock=lambda: clock).seal("job-a", token(time.time() + 3600))

    store = AuthEnvelopeStore(tmp_path / "expiry")
    with pytest.raises(AuthContextError, match="invalid_token_expiry"):
        store.seal("job-a", token(float("nan")))
    with pytest.raises(AuthContextError, match="invalid_token_expiry"):
        store.seal("job-b", token(time.time() + 3600), expires_at=float("inf"))
    with pytest.raises(AuthContextError, match="invalid_token_expiry"):
        store.seal("job-c", token(time.time() + 3600), expires_at=10**10000)


def test_envelope_cannot_extend_or_erase_token_claimed_expiry(tmp_path: Path, monkeypatch):
    _identity_dpapi(monkeypatch)
    now = int(time.time())
    store = AuthEnvelopeStore(tmp_path, clock=lambda: now)
    access_token = token(now + 3600)
    path = store.seal("job-a", access_token, expires_at=0)
    assert store.acquire("job-a").expires_at == now + 3600

    payload = json.loads(path.read_bytes()[4:])
    payload["expires_at"] = 0
    path.write_bytes(b"YSA1" + json.dumps(payload).encode())
    with pytest.raises(AuthContextError, match="corrupt"):
        store.acquire("job-a")


def test_decrypted_payload_must_be_exact_bounded_schema(tmp_path: Path, monkeypatch):
    _identity_dpapi(monkeypatch)
    store = AuthEnvelopeStore(tmp_path)
    path = store.path_for("job-a")
    now = time.time()
    payload = {"version": 1, "job_id": "job-a", "created_at": now,
               "envelope_expires_at": now + 1200, "expires_at": 0,
               "access_token": token(now + 3600), "user_id": "user-a", "private": "x"}
    path.write_bytes(b"YSA1" + json.dumps(payload).encode())
    with pytest.raises(AuthContextError, match="job_mismatch"):
        store.acquire("job-a")

    payload.pop("private")
    payload["envelope_expires_at"] = float("nan")
    path.write_text("YSA1" + json.dumps(payload, allow_nan=True), encoding="utf-8")
    with pytest.raises(AuthContextError, match="corrupt"):
        store.acquire("job-a")

    valid_payload = json.dumps({"version": 1, "job_id": "job-a", "created_at": now,
                                "envelope_expires_at": now + 1200, "expires_at": 0,
                                "access_token": token(now + 3600), "user_id": ""})
    duplicate_payload = valid_payload[:-1] + ',"version":1}'
    path.write_text("YSA1" + duplicate_payload, encoding="utf-8")
    with pytest.raises(AuthContextError, match="duplicate_field"):
        store.acquire("job-a")
