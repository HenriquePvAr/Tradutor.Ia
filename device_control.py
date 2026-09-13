"""Ed25519 device challenge/replay primitives for server-side tests."""
from __future__ import annotations
import hashlib, secrets, sqlite3, time
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

class DeviceChallenges:
    def __init__(self, db: sqlite3.Connection, ttl_seconds: int = 60):
        self.db, self.ttl = db, ttl_seconds
        self.db.execute("CREATE TABLE IF NOT EXISTS device_challenges(nonce_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,device_id TEXT NOT NULL,expires_at REAL NOT NULL,used INTEGER NOT NULL DEFAULT 0)")
        self.db.commit()
    def issue(self, *, user_id: str, device_id: str) -> str:
        nonce = secrets.token_urlsafe(32)
        self.db.execute("INSERT INTO device_challenges VALUES(?,?,?,?,0)", (hashlib.sha256(nonce.encode()).hexdigest(), user_id, device_id, time.time()+self.ttl)); self.db.commit()
        return nonce
    def verify(self, *, nonce: str, user_id: str, device_id: str, public_key: bytes, signature: bytes) -> bool:
        digest = hashlib.sha256(nonce.encode()).hexdigest(); row = self.db.execute("SELECT * FROM device_challenges WHERE nonce_hash=?", (digest,)).fetchone()
        if not row or row[1] != user_id or row[2] != device_id or row[4] or row[3] < time.time(): return False
        try: Ed25519PublicKey.from_public_bytes(public_key).verify(signature, nonce.encode())
        except (InvalidSignature, ValueError): return False
        self.db.execute("UPDATE device_challenges SET used=1 WHERE nonce_hash=?", (digest,)); self.db.commit(); return True
