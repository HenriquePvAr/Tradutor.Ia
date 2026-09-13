"""Per-installation Ed25519 identity protected by Windows DPAPI.

The private key is generated once, kept under the user's local application data,
and never serialized in plaintext.  Non-Windows hosts fail closed instead of
silently weakening the protection contract.
"""
from __future__ import annotations

import base64
import hashlib
import os
import struct
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization


MAGIC = b"YSID1"


def _root() -> Path:
    base = os.environ.get("LOCALAPPDATA", "").strip()
    if not base:
        raise RuntimeError("local_appdata_unavailable")
    path = Path(base) / "YomuSekai"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("dpapi_unavailable")
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    raw = ctypes.create_string_buffer(data)
    inp = Blob(len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_char)))
    out = Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(inp), "YomuSekai install identity", None, None, None, 0, ctypes.byref(out)):
        raise RuntimeError("dpapi_protect_failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("dpapi_unavailable")
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    raw = ctypes.create_string_buffer(data)
    inp = Blob(len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_char)))
    out = Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise RuntimeError("dpapi_unprotect_failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


class InstallIdentity:
    def __init__(self, path: Path | None = None):
        self.path = path or (_root() / "device-identity.bin")
        self._private: Ed25519PrivateKey | None = None

    def _load(self) -> Ed25519PrivateKey | None:
        if not self.path.exists():
            return None
        envelope = self.path.read_bytes()
        if not envelope.startswith(MAGIC) or len(envelope) < len(MAGIC) + 4:
            raise RuntimeError("identity_envelope_corrupt")
        size = struct.unpack("!I", envelope[len(MAGIC):len(MAGIC) + 4])[0]
        protected = envelope[len(MAGIC) + 4:]
        if size != len(protected) or not protected:
            raise RuntimeError("identity_envelope_corrupt")
        raw = _unprotect(protected)
        key = serialization.load_der_private_key(raw, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise RuntimeError("identity_key_invalid")
        return key

    def ensure(self) -> Ed25519PrivateKey:
        if self._private is not None:
            return self._private
        key = self._load()
        if key is None:
            key = Ed25519PrivateKey.generate()
            raw = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
            protected = _protect(raw)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_bytes(MAGIC + struct.pack("!I", len(protected)) + protected)
            os.replace(tmp, self.path)
        self._private = key
        return key

    def public_key(self) -> str:
        raw = self.ensure().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return _b64(raw)

    def device_id(self) -> str:
        raw = base64.urlsafe_b64decode(self.public_key() + "==")
        return "ys-" + hashlib.sha256(raw).hexdigest()[:32]

    def sign(self, message: str) -> str:
        return _b64(self.ensure().sign(str(message).encode("utf-8")))
