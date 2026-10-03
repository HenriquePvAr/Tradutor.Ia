import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from commercial_device import CommercialDeviceAuthorizer, CommercialDeviceAuthorizationError, CommercialDeviceContext
from community_auth import RequestPrincipal


class _Identity:
    def device_id(self): return "ys-" + "a" * 32
    def public_key(self): return "cHVibGljLWtleQ"
    def sign(self, nonce): return "c2ln"


class _Transport:
    def __init__(self, responses): self.responses = list(responses); self.calls = []
    def request(self, method, url, *, headers, data):
        self.calls.append((method, url, json.loads(data.decode("utf-8"))))
        return self.responses.pop(0)


def _response(status, payload):
    return SimpleNamespace(status=status, content=json.dumps(payload).encode("utf-8"))


class CommercialDeviceAuthorityTests(unittest.TestCase):
    def principal(self): return RequestPrincipal(user_id="user-1", authenticated=True, auth_source="supabase")

    def test_challenge_signature_verify_returns_commercial_context(self):
        transport = _Transport([
            _response(200, {"device_id": "11111111-1111-4111-8111-111111111111", "license_id": "lic-1"}),
            _response(200, {"challenge_id": "ch-1", "nonce": "nonce-1"}),
            _response(200, {"verified": True, "device_id": "11111111-1111-4111-8111-111111111111", "license_id": "lic-1"}),
        ])
        with TemporaryDirectory() as d:
            ctx = CommercialDeviceAuthorizer(Path(d), supabase_url="https://example.test", publishable_key="pk", identity=_Identity(), transport=transport).authorize(principal=self.principal(), access_token="jwt")
        self.assertIsInstance(ctx, CommercialDeviceContext)
        self.assertEqual(ctx.device_uuid, "11111111-1111-4111-8111-111111111111")
        self.assertEqual([c[1].split("/functions/v1/")[-1] for c in transport.calls], ["device-register", "device-challenge", "device-verify"])
        self.assertEqual(transport.calls[-1][2]["signature"], "c2ln")

    def test_revoked_commercial_device_fails_closed(self):
        transport = _Transport([
            _response(200, {"device_id": "11111111-1111-4111-8111-111111111111"}),
            _response(403, {"code": "device_revoked"}),
        ])
        with TemporaryDirectory() as d:
            with self.assertRaises(CommercialDeviceAuthorizationError) as cm:
                CommercialDeviceAuthorizer(Path(d), supabase_url="https://example.test", publishable_key="pk", identity=_Identity(), transport=transport).authorize(principal=self.principal(), access_token="jwt")
        self.assertEqual(cm.exception.code, "device_revoked")

    def test_cached_uuid_avoids_register_on_next_job(self):
        with TemporaryDirectory() as d:
            first = _Transport([_response(200, {"device_id": "11111111-1111-4111-8111-111111111111"}), _response(200, {"challenge_id": "c", "nonce": "n"}), _response(200, {"verified": True, "device_id": "11111111-1111-4111-8111-111111111111"})])
            authorizer = CommercialDeviceAuthorizer(Path(d), supabase_url="https://example.test", publishable_key="pk", identity=_Identity(), transport=first)
            authorizer.authorize(principal=self.principal(), access_token="jwt")
            second = _Transport([_response(200, {"challenge_id": "c2", "nonce": "n2"}), _response(200, {"verified": True, "device_id": "11111111-1111-4111-8111-111111111111"})])
            CommercialDeviceAuthorizer(Path(d), supabase_url="https://example.test", publishable_key="pk", identity=_Identity(), transport=second).authorize(principal=self.principal(), access_token="jwt")
        self.assertEqual(second.calls[0][1].split("/functions/v1/")[-1], "device-challenge")

