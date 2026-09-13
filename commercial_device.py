"""Server-side commercial device authorization for local job creation.

The browser is never trusted for the commercial UUID.  The local process derives
the public device id from its protected InstallIdentity, then proves possession
through the commercial challenge/signature/verify protocol.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from desktop_identity import InstallIdentity


_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")


@dataclass(frozen=True, slots=True)
class CommercialDeviceContext:
    device_uuid: str
    device_id: str
    license_id: str = ""
    user_id: str = ""
    status: str = "active"
    verified: bool = True


class CommercialDeviceAuthorizationError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class CommercialDeviceAuthorizer:
    def __init__(self, runtime_root: Path, *, supabase_url: str, publishable_key: str,
                 identity: InstallIdentity | None = None, transport=None):
        self.runtime_root = Path(runtime_root)
        self.supabase_url = str(supabase_url).rstrip("/")
        self.publishable_key = str(publishable_key)
        self.identity = identity or InstallIdentity()
        self.transport = transport
        self.mapping_path = self.runtime_root / "commercial_device.json"

    def _http(self, name: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
        if not token:
            raise CommercialDeviceAuthorizationError("auth_required")
        headers = {"apikey": self.publishable_key, "Authorization": f"Bearer {token}",
                   "Accept": "application/json", "Content-Type": "application/json"}
        url = f"{self.supabase_url}/functions/v1/{name}"
        try:
            if self.transport is not None:
                response = self.transport.request("POST", url, headers=headers,
                                                   data=json.dumps(payload).encode("utf-8"))
                status = int(getattr(response, "status", 0) or 0)
                raw = getattr(response, "content", b"") or b""
                body = json.loads(raw.decode("utf-8")) if raw else {}
            else:
                from google_drive_transport import RequestsHttpTransport
                response = RequestsHttpTransport(connect_timeout=5.0, read_timeout=10.0).request(
                    "POST", url, headers=headers, data=json.dumps(payload).encode("utf-8"))
                status = int(getattr(response, "status", 0) or 0)
                raw = getattr(response, "content", b"") or b""
                body = json.loads(raw.decode("utf-8")) if raw else {}
        except CommercialDeviceAuthorizationError:
            raise
        except Exception as exc:
            raise CommercialDeviceAuthorizationError("control_plane_temporarily_unavailable", retryable=True) from exc
        if status >= 500 or status == 429:
            raise CommercialDeviceAuthorizationError("control_plane_temporarily_unavailable", retryable=True)
        if status >= 400 or not isinstance(body, dict):
            code = str(body.get("code") or "device_rejected") if isinstance(body, dict) else "device_rejected"
            raise CommercialDeviceAuthorizationError(code)
        if body.get("code"):
            raise CommercialDeviceAuthorizationError(str(body["code"]))
        return body

    def _load_mapping(self, device_id: str) -> str:
        try:
            data = json.loads(self.mapping_path.read_text(encoding="utf-8"))
            value = str(data.get("device_uuid") or "")
            return value if data.get("device_id") == device_id and _UUID.fullmatch(value) else ""
        except (OSError, ValueError, TypeError):
            return ""

    def _save_mapping(self, device_id: str, device_uuid: str) -> None:
        if not _UUID.fullmatch(device_uuid):
            raise CommercialDeviceAuthorizationError("malformed_device_response")
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        tmp = self.mapping_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"device_id": device_id, "device_uuid": device_uuid}, separators=(",", ":")), encoding="utf-8")
        tmp.replace(self.mapping_path)

    def authorize(self, *, principal=None, operation: str = "start_translation", access_token: str = "") -> CommercialDeviceContext:
        if not getattr(principal, "authenticated", False):
            raise CommercialDeviceAuthorizationError("auth_required")
        device_id = self.identity.device_id()
        public_key = self.identity.public_key()
        device_uuid = self._load_mapping(device_id)
        if not device_uuid:
            registered = self._http("device-register", {"device_id": device_id, "public_key": public_key}, access_token)
            device_uuid = str(registered.get("device_id") or registered.get("device_uuid") or "")
            self._save_mapping(device_id, device_uuid)
        try:
            challenge = self._http("device-challenge", {"device_uuid": device_uuid}, access_token)
        except CommercialDeviceAuthorizationError as exc:
            if exc.code != "device_not_found":
                raise
            registered = self._http("device-register", {"device_id": device_id, "public_key": public_key}, access_token)
            device_uuid = str(registered.get("device_id") or registered.get("device_uuid") or "")
            self._save_mapping(device_id, device_uuid)
            challenge = self._http("device-challenge", {"device_uuid": device_uuid}, access_token)
        challenge_id = str(challenge.get("challenge_id") or "")
        nonce = str(challenge.get("nonce") or "")
        if not challenge_id or not nonce:
            raise CommercialDeviceAuthorizationError("malformed_challenge")
        verified = self._http("device-verify", {"challenge_id": challenge_id, "nonce": nonce, "signature": self.identity.sign(nonce)}, access_token)
        returned_uuid = str(verified.get("device_id") or verified.get("device_uuid") or device_uuid)
        if returned_uuid != device_uuid or not _UUID.fullmatch(returned_uuid) or verified.get("verified") is not True:
            raise CommercialDeviceAuthorizationError("device_context_mismatch")
        return CommercialDeviceContext(device_uuid=returned_uuid, device_id=device_id,
                                       license_id=str(verified.get("license_id") or ""),
                                       user_id=str(getattr(principal, "user_id", "") or ""))


class UnavailableCommercialDeviceAuthorizer:
    def authorize(self, **_: Any):
        raise CommercialDeviceAuthorizationError("control_plane_temporarily_unavailable", retryable=True)
