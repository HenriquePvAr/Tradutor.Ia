"""Fail-closed Scan Beta licensing primitives.

This module deliberately separates Supabase authentication from Beta authorization.
It never reads service-role credentials, never stores token values, and is written so
the desktop/UI can depend on a small authorization decision while the later Supabase
integration can provide the authoritative server response.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol


class LicenseState(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"
    DEVICE_LIMIT_REACHED = "device_limit_reached"
    DEVICE_REVOKED = "device_revoked"
    NOT_ENTITLED = "not_entitled"
    LICENSE_UNAVAILABLE = "license_unavailable"
    MALFORMED_LICENSE = "malformed_license"
    NOT_STARTED = "not_started"
    AUTH_REQUIRED = "auth_required"
    AUTH_INVALID = "auth_invalid"


DENY_STATES = frozenset(state for state in LicenseState if state is not LicenseState.ACTIVE)


@dataclass(frozen=True, slots=True)
class BetaAccessDecision:
    """Structured authorization result safe to persist in job metadata."""

    allowed: bool
    state: LicenseState
    reason_code: str
    user_id: str = ""
    device_fingerprint_hash: str = ""
    entitlement_id: str = ""
    expires_at: str = ""
    checked_at: str = ""
    source: str = "unknown"
    retryable: bool = False

    def __post_init__(self) -> None:
        if self.allowed and self.state is not LicenseState.ACTIVE:
            raise ValueError("allowed_decision_must_be_active")
        if not self.allowed and self.state not in DENY_STATES:
            raise ValueError("denied_decision_must_have_deny_state")

    @classmethod
    def deny(
        cls,
        state: LicenseState,
        reason_code: str,
        *,
        user_id: str = "",
        device_fingerprint_hash: str = "",
        source: str = "license_authority",
        retryable: bool = False,
        checked_at: str | None = None,
    ) -> "BetaAccessDecision":
        return cls(
            allowed=False,
            state=state,
            reason_code=reason_code,
            user_id=user_id,
            device_fingerprint_hash=device_fingerprint_hash,
            checked_at=checked_at or utc_now_iso(),
            source=source,
            retryable=retryable,
        )

    @classmethod
    def allow(
        cls,
        *,
        user_id: str,
        device_fingerprint_hash: str,
        entitlement_id: str,
        expires_at: str,
        source: str = "license_authority",
        checked_at: str | None = None,
    ) -> "BetaAccessDecision":
        return cls(
            allowed=True,
            state=LicenseState.ACTIVE,
            reason_code="active",
            user_id=user_id,
            device_fingerprint_hash=device_fingerprint_hash,
            entitlement_id=entitlement_id,
            expires_at=expires_at,
            checked_at=checked_at or utc_now_iso(),
            source=source,
            retryable=False,
        )

    def to_safe_job_metadata(self) -> dict[str, Any]:
        return {
            "required": True,
            "allowed": bool(self.allowed),
            "state": self.state.value,
            "reason_code": self.reason_code,
            "user_id": self.user_id,
            "device_fingerprint_hash": self.device_fingerprint_hash,
            "entitlement_id": self.entitlement_id,
            "expires_at": self.expires_at,
            "checked_at": self.checked_at,
            "source": self.source,
            "retryable": bool(self.retryable),
        }


class BetaLicenseAuthorizer(Protocol):
    def authorize(
        self, *, principal: Any = None, operation: str = "start_translation"
    ) -> BetaAccessDecision:
        ...


class LocalDevelopmentBetaAuthorizer:
    """Explicit non-production bypass used to keep hermetic development runnable."""

    def authorize(
        self, *, principal: Any = None, operation: str = "start_translation"
    ) -> BetaAccessDecision:
        user_id = str(getattr(principal, "user_id", "") or "local-dev")
        return BetaAccessDecision.allow(
            user_id=user_id,
            device_fingerprint_hash="local-development-device",
            entitlement_id="local-development",
            expires_at="9999-12-31T23:59:59Z",
            source="local_development",
        )


class StaticBetaAuthorizer:
    """Tiny fake adapter for hermetic tests and offline state-machine proofs."""

    def __init__(self, decision: BetaAccessDecision):
        self.decision = decision
        self.calls: list[str] = []

    def authorize(
        self, *, principal: Any = None, operation: str = "start_translation"
    ) -> BetaAccessDecision:
        self.calls.append(operation)
        return self.decision


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def stable_install_fingerprint_hash(install_id: str, bounded_machine_hint: str = "") -> str:
    """Return a minimized irreversible device identifier."""

    install = str(install_id or "").strip()
    if not install:
        raise ValueError("install_id_required")
    hint = str(bounded_machine_hint or "").strip()
    payload = json.dumps(
        {"install_id": install, "machine_hint": hint},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SQLiteBetaLicenseStore:
    """Local hermetic authority used to prove Beta licensing semantics."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path), timeout=5.0, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS beta_tester_entitlements (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL,
                starts_at TEXT,
                expires_at TEXT,
                revoked_at TEXT,
                revocation_reason TEXT,
                max_devices INTEGER NOT NULL DEFAULT 1,
                beta_channel TEXT NOT NULL DEFAULT 'scan-beta',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, beta_channel)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS beta_tester_devices (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                device_fingerprint_hash TEXT NOT NULL,
                device_label TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                revoked_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, device_fingerprint_hash)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS beta_tester_license_events (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                device_fingerprint_hash TEXT,
                reason TEXT,
                created_at TEXT NOT NULL
            )
            """
        )

    def upsert_entitlement(
        self,
        *,
        user_id: str,
        status: str = "active",
        starts_at: str = "",
        expires_at: str = "",
        revoked_at: str = "",
        revocation_reason: str = "",
        max_devices: int = 1,
        beta_channel: str = "scan-beta",
        entitlement_id: str | None = None,
    ) -> str:
        now = utc_now_iso()
        entitlement_id = entitlement_id or uuid.uuid4().hex
        self._conn.execute(
            """
            INSERT INTO beta_tester_entitlements (
                id, user_id, status, starts_at, expires_at, revoked_at,
                revocation_reason, max_devices, beta_channel, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id, beta_channel) DO UPDATE SET
                status=excluded.status,
                starts_at=excluded.starts_at,
                expires_at=excluded.expires_at,
                revoked_at=excluded.revoked_at,
                revocation_reason=excluded.revocation_reason,
                max_devices=excluded.max_devices,
                updated_at=excluded.updated_at
            """,
            (
                entitlement_id,
                user_id,
                status,
                starts_at,
                expires_at,
                revoked_at,
                revocation_reason,
                int(max_devices),
                beta_channel,
                now,
                now,
            ),
        )
        return entitlement_id

    def revoke_device(
        self, *, user_id: str, device_fingerprint_hash: str, at: str | None = None
    ) -> None:
        self._conn.execute(
            """
            UPDATE beta_tester_devices
            SET revoked_at=?
            WHERE user_id=? AND device_fingerprint_hash=?
            """,
            (at or utc_now_iso(), user_id, device_fingerprint_hash),
        )

    def authorize(
        self,
        *,
        user_id: str,
        device_fingerprint_hash: str,
        now: str,
        beta_channel: str = "scan-beta",
    ) -> BetaAccessDecision:
        if not user_id:
            return BetaAccessDecision.deny(LicenseState.AUTH_REQUIRED, "auth_required")
        if not device_fingerprint_hash:
            return BetaAccessDecision.deny(
                LicenseState.MALFORMED_LICENSE, "device_fingerprint_required", user_id=user_id
            )
        checked_at = now
        current_time = parse_utc(now)
        if current_time is None:
            return BetaAccessDecision.deny(
                LicenseState.MALFORMED_LICENSE,
                "malformed_authority_time",
                user_id=user_id,
                device_fingerprint_hash=device_fingerprint_hash,
            )
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    """
                    SELECT * FROM beta_tester_entitlements
                    WHERE user_id=? AND beta_channel=?
                    """,
                    (user_id, beta_channel),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return BetaAccessDecision.deny(
                        LicenseState.NOT_ENTITLED,
                        "not_entitled",
                        user_id=user_id,
                        device_fingerprint_hash=device_fingerprint_hash,
                        checked_at=checked_at,
                    )
                decision = self._entitlement_state(row, current_time, checked_at,
                                                   device_fingerprint_hash)
                if decision is not None:
                    self._conn.execute("COMMIT")
                    return decision
                device = self._conn.execute(
                    """
                    SELECT * FROM beta_tester_devices
                    WHERE user_id=? AND device_fingerprint_hash=?
                    """,
                    (user_id, device_fingerprint_hash),
                ).fetchone()
                if device is not None:
                    if device["revoked_at"]:
                        self._conn.execute("COMMIT")
                        return BetaAccessDecision.deny(
                            LicenseState.DEVICE_REVOKED,
                            "device_revoked",
                            user_id=user_id,
                            device_fingerprint_hash=device_fingerprint_hash,
                            checked_at=checked_at,
                        )
                    self._conn.execute(
                        """
                        UPDATE beta_tester_devices
                        SET last_seen_at=?
                        WHERE user_id=? AND device_fingerprint_hash=?
                        """,
                        (checked_at, user_id, device_fingerprint_hash),
                    )
                    self._conn.execute("COMMIT")
                    return BetaAccessDecision.allow(
                        user_id=user_id,
                        device_fingerprint_hash=device_fingerprint_hash,
                        entitlement_id=row["id"],
                        expires_at=row["expires_at"] or "",
                        checked_at=checked_at,
                    )
                active_count = int(self._conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM beta_tester_devices
                    WHERE user_id=? AND revoked_at IS NULL
                    """,
                    (user_id,),
                ).fetchone()["n"])
                if active_count >= int(row["max_devices"]):
                    self._conn.execute("COMMIT")
                    return BetaAccessDecision.deny(
                        LicenseState.DEVICE_LIMIT_REACHED,
                        "device_limit_reached",
                        user_id=user_id,
                        device_fingerprint_hash=device_fingerprint_hash,
                        checked_at=checked_at,
                    )
                self._conn.execute(
                    """
                    INSERT INTO beta_tester_devices (
                        id, user_id, device_fingerprint_hash, device_label,
                        first_seen_at, last_seen_at, created_at
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        uuid.uuid4().hex,
                        user_id,
                        device_fingerprint_hash,
                        "",
                        checked_at,
                        checked_at,
                        checked_at,
                    ),
                )
                self._conn.execute(
                    """
                    INSERT INTO beta_tester_license_events (
                        id, user_id, event_type, device_fingerprint_hash, reason, created_at
                    ) VALUES (?,?,?,?,?,?)
                    """,
                    (uuid.uuid4().hex, user_id, "device_register",
                     device_fingerprint_hash, "allowed", checked_at),
                )
                self._conn.execute("COMMIT")
                return BetaAccessDecision.allow(
                    user_id=user_id,
                    device_fingerprint_hash=device_fingerprint_hash,
                    entitlement_id=row["id"],
                    expires_at=row["expires_at"] or "",
                    checked_at=checked_at,
                )
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _entitlement_state(
        row: sqlite3.Row,
        current_time: datetime,
        checked_at: str,
        device_fingerprint_hash: str,
    ) -> BetaAccessDecision | None:
        user_id = row["user_id"]
        status = str(row["status"] or "").casefold()
        if status == "revoked" or row["revoked_at"]:
            return BetaAccessDecision.deny(
                LicenseState.REVOKED, "license_revoked",
                user_id=user_id, device_fingerprint_hash=device_fingerprint_hash,
                checked_at=checked_at,
            )
        if status != "active":
            return BetaAccessDecision.deny(
                LicenseState.NOT_ENTITLED, "license_not_active",
                user_id=user_id, device_fingerprint_hash=device_fingerprint_hash,
                checked_at=checked_at,
            )
        starts_at = parse_utc(row["starts_at"] or "")
        if starts_at is not None and starts_at > current_time:
            return BetaAccessDecision.deny(
                LicenseState.NOT_STARTED, "license_not_started",
                user_id=user_id, device_fingerprint_hash=device_fingerprint_hash,
                checked_at=checked_at,
            )
        expires_at = parse_utc(row["expires_at"] or "")
        if expires_at is None:
            return BetaAccessDecision.deny(
                LicenseState.MALFORMED_LICENSE, "missing_or_malformed_expiry",
                user_id=user_id, device_fingerprint_hash=device_fingerprint_hash,
                checked_at=checked_at,
            )
        if current_time >= expires_at:
            return BetaAccessDecision.deny(
                LicenseState.EXPIRED, "license_expired",
                user_id=user_id, device_fingerprint_hash=device_fingerprint_hash,
                checked_at=checked_at,
            )
        if int(row["max_devices"] or 0) < 1:
            return BetaAccessDecision.deny(
                LicenseState.MALFORMED_LICENSE, "invalid_device_limit",
                user_id=user_id, device_fingerprint_hash=device_fingerprint_hash,
                checked_at=checked_at,
            )
        return None


def fail_closed_from_authority_error(
    *,
    user_id: str = "",
    device_fingerprint_hash: str = "",
    reason_code: str = "license_service_unavailable",
) -> BetaAccessDecision:
    return BetaAccessDecision.deny(
        LicenseState.LICENSE_UNAVAILABLE,
        reason_code,
        user_id=user_id,
        device_fingerprint_hash=device_fingerprint_hash,
        retryable=True,
    )


def validate_authorization_response(value: Any) -> BetaAccessDecision:
    """Validate a server/fake response and fail closed on malformed content."""

    if isinstance(value, BetaAccessDecision):
        return value
    if not isinstance(value, dict):
        return BetaAccessDecision.deny(LicenseState.MALFORMED_LICENSE, "malformed_license_response")
    try:
        state = LicenseState(str(value.get("state") or ""))
        allowed = bool(value.get("allowed"))
        reason = str(value.get("reason_code") or state.value)
    except ValueError:
        return BetaAccessDecision.deny(LicenseState.MALFORMED_LICENSE, "malformed_license_state")
    try:
        return BetaAccessDecision(
            allowed=allowed,
            state=state,
            reason_code=reason,
            user_id=str(value.get("user_id") or ""),
            device_fingerprint_hash=str(value.get("device_fingerprint_hash") or ""),
            entitlement_id=str(value.get("entitlement_id") or ""),
            expires_at=str(value.get("expires_at") or ""),
            checked_at=str(value.get("checked_at") or utc_now_iso()),
            source=str(value.get("source") or "license_authority"),
            retryable=bool(value.get("retryable")),
        )
    except ValueError:
        return BetaAccessDecision.deny(LicenseState.MALFORMED_LICENSE, "incoherent_license_response")
