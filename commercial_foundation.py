"""Server-authoritative commercial primitives for the controlled beta.

This module contains offline-testable policy and wallet logic only.  It never
decides entitlement from client input and ships no billing/ad credentials.
Remote adapters are intentionally disabled until configured server-side.
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol


DAILY_TARGET = 5
TRANSLATION_COST_YK = 1


@dataclass(frozen=True, slots=True)
class Plan:
    slug: str
    name: str
    ad_free: bool
    rewarded_ads_enabled: bool
    translation_access_mode: str
    daily_yk_target: int = DAILY_TARGET
    included_yk_per_cycle: int = 0
    yk_cost_per_translation: int = TRANSLATION_COST_YK
    max_devices_default: int = 1


PLANS: dict[str, Plan] = {
    "free": Plan("free", "Free", False, True, "yk"),
    "pro_monthly": Plan("pro_monthly", "Pro mensal", True, False, "quota"),
    "pro_annual": Plan("pro_annual", "Pro anual", True, False, "quota"),
}


class BillingProvider(Protocol):
    def create_checkout(self, *, user_id: str, plan_slug: str) -> str: ...
    def create_customer_portal(self, *, user_id: str) -> str: ...


class DisabledBillingProvider:
    """Safe production default: no checkout is possible until configured."""

    enabled = False

    def create_checkout(self, *, user_id: str, plan_slug: str) -> str:
        raise RuntimeError("billing_provider_not_configured")

    def create_customer_portal(self, *, user_id: str) -> str:
        raise RuntimeError("billing_provider_not_configured")


class RewardedAdProvider(Protocol):
    enabled: bool

    def verify_reward(self, receipt: Mapping[str, Any]) -> str: ...


class DisabledRewardedAdProvider:
    enabled = False

    def verify_reward(self, receipt: Mapping[str, Any]) -> str:
        raise RuntimeError("rewarded_ads_provider_not_configured")


@dataclass(frozen=True, slots=True)
class WalletBalances:
    daily: int
    subscription: int
    permanent: int

    @property
    def available(self) -> int:
        return self.daily + self.subscription + self.permanent


class YKWallet:
    """Small SQLite ledger implementing daily top-up and idempotent spending."""

    def __init__(self, connection: sqlite3.Connection):
        self._db = connection
        self._lock = threading.RLock()
        self._db.row_factory = sqlite3.Row
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS yk_ledger (
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL, amount INTEGER NOT NULL,
              bucket TEXT NOT NULL CHECK(bucket IN ('daily','subscription','permanent')),
              event_type TEXT NOT NULL, reference_id TEXT, expires_at TEXT,
              created_at TEXT NOT NULL, metadata_sanitized TEXT
            );
            CREATE TABLE IF NOT EXISTS yk_daily_claims (
              user_id TEXT NOT NULL, claim_day TEXT NOT NULL, amount INTEGER NOT NULL,
              created_at TEXT NOT NULL, PRIMARY KEY(user_id, claim_day)
            );
            CREATE TABLE IF NOT EXISTS yk_reservations (
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL, job_id TEXT UNIQUE NOT NULL,
              amount INTEGER NOT NULL, status TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """
        )

    @staticmethod
    def _day(now: datetime) -> str:
        return now.astimezone(timezone.utc).date().isoformat()

    def _expire_daily(self, user_id: str, now: datetime) -> None:
        day = self._day(now)
        self._db.execute(
            "UPDATE yk_ledger SET amount=0 WHERE user_id=? AND bucket='daily' "
            "AND expires_at IS NOT NULL AND expires_at<=? AND amount>0",
            (user_id, day),
        )

    def balances(self, user_id: str, *, now: datetime | None = None) -> WalletBalances:
        now = now or datetime.now(timezone.utc)
        with self._lock:
            self._expire_daily(user_id, now)
            rows = self._db.execute(
                "SELECT bucket, COALESCE(SUM(amount),0) AS total FROM yk_ledger "
                "WHERE user_id=? GROUP BY bucket", (user_id,)
            ).fetchall()
            totals = {str(row["bucket"]): max(0, int(row["total"])) for row in rows}
            return WalletBalances(totals.get("daily", 0), totals.get("subscription", 0), totals.get("permanent", 0))

    def claim_daily(self, user_id: str, *, now: datetime | None = None, target: int = DAILY_TARGET) -> int:
        now = now or datetime.now(timezone.utc)
        day = self._day(now)
        with self._lock, self._db:
            self._expire_daily(user_id, now)
            if self._db.execute("SELECT 1 FROM yk_daily_claims WHERE user_id=? AND claim_day=?", (user_id, day)).fetchone():
                return 0
            permanent = self.balances(user_id, now=now).permanent
            amount = max(0, int(target) - permanent)
            self._db.execute("INSERT INTO yk_daily_claims VALUES (?,?,?,?)", (user_id, day, amount, now.isoformat()))
            if amount:
                self._db.execute("INSERT INTO yk_ledger VALUES (?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, user_id, amount, "daily", "daily_topup", day, (now + timedelta(days=1)).date().isoformat(), now.isoformat(), "{}"))
            return amount

    def reserve_translation(self, user_id: str, job_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        with self._lock, self._db:
            existing = self._db.execute("SELECT * FROM yk_reservations WHERE job_id=?", (job_id,)).fetchone()
            if existing:
                return dict(existing)
            if self.balances(user_id, now=now).available < TRANSLATION_COST_YK:
                raise RuntimeError("insufficient_yk")
            reservation_id = uuid.uuid4().hex
            self._db.execute("INSERT INTO yk_reservations VALUES (?,?,?,?,?,?,?)", (reservation_id, user_id, job_id, TRANSLATION_COST_YK, "reserved", now.isoformat(), now.isoformat()))
            return dict(self._db.execute("SELECT * FROM yk_reservations WHERE id=?", (reservation_id,)).fetchone())

    def consume_reservation(self, reservation_id: str, *, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM yk_reservations WHERE id=?", (reservation_id,)).fetchone()
            if not row or row["status"] != "reserved":
                return
            remaining = int(row["amount"])
            for bucket in ("daily", "subscription", "permanent"):
                balance = self.balances(row["user_id"], now=now)
                available = getattr(balance, bucket)
                take = min(remaining, available)
                if take:
                    self._db.execute("INSERT INTO yk_ledger VALUES (?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, row["user_id"], -take, bucket, "translation", row["job_id"], None, now.isoformat(), "{}"))
                    remaining -= take
                if not remaining:
                    break
            if remaining:
                raise RuntimeError("reservation_balance_changed")
            self._db.execute("UPDATE yk_reservations SET status='consumed',updated_at=? WHERE id=?", (now.isoformat(), reservation_id))

    def release_reservation(self, reservation_id: str, *, now: datetime | None = None) -> None:
        with self._lock:
            self._db.execute("UPDATE yk_reservations SET status='released',updated_at=? WHERE id=? AND status='reserved'", ((now or datetime.now(timezone.utc)).isoformat(), reservation_id))

