"""Offline-testable control-plane primitives; remote adapters remain disabled by default."""
from __future__ import annotations
import hashlib, secrets, sqlite3, time
from dataclasses import dataclass

DEFAULT_FLAGS = {"translation_enabled": True, "billing_enabled": False,
                 "rewarded_ads_enabled": False, "community_enabled": True,
                 "auto_update_enabled": True, "maintenance_mode": False,
                 "new_signups_enabled": True}

class ControlPlane:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS beta_flags(name TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS beta_invites(id TEXT PRIMARY KEY, code_hash TEXT UNIQUE NOT NULL,
          created_by TEXT NOT NULL, max_uses INTEGER NOT NULL, uses INTEGER NOT NULL DEFAULT 0,
          expires_at REAL, default_license_days INTEGER NOT NULL DEFAULT 30, status TEXT NOT NULL DEFAULT 'active');
        CREATE TABLE IF NOT EXISTS beta_grants(request_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
          plan TEXT NOT NULL, days INTEGER NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS sanitized_error_reports(id TEXT PRIMARY KEY, user_id TEXT, code TEXT NOT NULL,
          stage TEXT, duration_ms INTEGER, page_count INTEGER, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS legal_acceptances(user_id TEXT NOT NULL, document_type TEXT NOT NULL,
          version TEXT NOT NULL, accepted_at REAL NOT NULL, PRIMARY KEY(user_id, document_type, version));
        """)
        for name, value in DEFAULT_FLAGS.items():
            self.db.execute("INSERT OR IGNORE INTO beta_flags VALUES(?,?)", (name, "1" if value else "0"))
        self.db.commit()

    def flag(self, name: str) -> bool:
        row = self.db.execute("SELECT value FROM beta_flags WHERE name=?", (name,)).fetchone()
        return bool(row and row[0] == "1")

    def set_flag(self, name: str, enabled: bool) -> None:
        self.db.execute("INSERT INTO beta_flags(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, "1" if enabled else "0"))
        self.db.commit()

    def create_invite(self, *, created_by: str, max_uses: int = 1, days: int = 30, expires_at: float | None = None) -> str:
        if not created_by or max_uses < 1 or days not in {15, 30, 60, 90}:
            raise ValueError("invalid_invite")
        code = secrets.token_urlsafe(18)
        self.db.execute("INSERT INTO beta_invites VALUES(?,?,?,?,?,?,?,?)", (secrets.token_hex(8), hashlib.sha256(code.encode()).hexdigest(), created_by, max_uses, 0, expires_at, days, "active"))
        self.db.commit()
        return code

    def redeem_invite(self, *, user_id: str, code: str, request_id: str) -> int:
        row = self.db.execute("SELECT * FROM beta_invites WHERE code_hash=?", (hashlib.sha256(code.encode()).hexdigest(),)).fetchone()
        if not row or row["status"] != "active" or row["uses"] >= row["max_uses"] or (row["expires_at"] and row["expires_at"] < time.time()):
            raise ValueError("invite_invalid_or_expired")
        self.db.execute("UPDATE beta_invites SET uses=uses+1 WHERE id=? AND uses<max_uses", (row["id"],))
        self.db.execute("INSERT OR IGNORE INTO beta_grants VALUES(?,?,?,?,?)", (request_id, user_id, "beta", row["default_license_days"], time.time()))
        self.db.commit()
        return int(row["default_license_days"])

    def record_error(self, *, report_id: str, user_id: str | None, code: str, stage: str = "", duration_ms: int = 0, page_count: int = 0) -> None:
        self.db.execute("INSERT OR IGNORE INTO sanitized_error_reports VALUES(?,?,?,?,?,?,?)", (report_id, user_id, code, stage, int(duration_ms), int(page_count), time.time()))
        self.db.commit()

    def accept_legal(self, *, user_id: str, document_type: str, version: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO legal_acceptances VALUES(?,?,?,?)", (user_id, document_type, version, time.time())); self.db.commit()
