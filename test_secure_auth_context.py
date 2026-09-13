import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from secure_auth_context import AuthContextError, AuthEnvelopeStore


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI required")


def token(exp: int) -> str:
    head = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp, "sub": "user-a"}).encode()).decode().rstrip("=")
    return f"{head}.{body}.signature-for-tests"


def test_dpapi_binding_ttl_tamper_and_cleanup(tmp_path: Path):
    store = AuthEnvelopeStore(tmp_path, ttl_seconds=1800)
    secret = "YOMU_AUTH_SECRET_MUST_NOT_LEAK_123456"
    path = store.seal("job-a", token(int(time.time()) + 3600), user_id="user-a")
    assert secret.encode() not in path.read_bytes()
    ctx = store.acquire("job-a")
    assert ctx.safe_metadata()["present"] is True
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


def test_real_process_boundary(tmp_path: Path):
    store = AuthEnvelopeStore(tmp_path, ttl_seconds=1800)
    path = store.seal("job-process", token(int(time.time()) + 3600))
    code = (
        "from secure_auth_context import AuthEnvelopeStore; "
        f"s=AuthEnvelopeStore(r'{tmp_path}'); c=s.acquire('job-process'); "
        "print(c.safe_metadata()['present'])"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "True"
    assert str(path) not in result.stdout
