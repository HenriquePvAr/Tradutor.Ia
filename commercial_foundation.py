"""Server-authoritative commercial primitives for the controlled beta.

This module contains offline-testable policy and wallet logic only.  It never
decides entitlement from client input and ships no billing/ad credentials.
Remote adapters are intentionally disabled until configured server-side.

The wallet here is the executable model of the SQL in
``supabase/migrations/20260915140000_yk_ads_foundation.sql``.  Both must agree
on the same five rules:

* daily YK is a *top-up to the plan target*, never ``permanent + target``;
* the top-up happens at most once per cycle and is never refilled after spend;
* daily YK expires and its debit inherits the expiry of the credit that funded
  it, so credit and debit leave the active balance together;
* daily YK is only usable while the user keeps passive ads enabled, and turning
  ads off blocks it without deleting it;
* permanent YK never expires, is always usable, and may exceed the target.
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Mapping, Protocol


DAILY_TARGET = 5
TRANSLATION_COST_YK = 1
REWARDED_DAILY_CAP = 5


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
    rewarded_daily_cap: int = REWARDED_DAILY_CAP


PLANS: dict[str, Plan] = {
    "free": Plan("free", "Free", False, True, "yk"),
    "pro_monthly": Plan("pro_monthly", "Pro mensal", True, False, "quota", daily_yk_target=0),
    "pro_annual": Plan("pro_annual", "Pro anual", True, False, "quota", daily_yk_target=0),
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
    """Stored, unexpired balance per bucket — before the passive-ads gate."""

    daily: int
    subscription: int
    permanent: int

    @property
    def available(self) -> int:
        return self.daily + self.subscription + self.permanent


@dataclass(frozen=True, slots=True)
class WalletSummary:
    """What the client is allowed to render, with the gate already applied."""

    daily_stored: int
    daily_usable: int
    subscription: int
    permanent: int
    reserved: int
    passive_ads_enabled: bool

    @property
    def usable_total(self) -> int:
        return self.daily_usable + self.subscription + self.permanent


class YKWallet:
    """SQLite ledger implementing the YK rules above."""

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
              daily_amount INTEGER NOT NULL DEFAULT 0,
              subscription_amount INTEGER NOT NULL DEFAULT 0,
              permanent_amount INTEGER NOT NULL DEFAULT 0,
              daily_expires_at TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_ad_preferences (
              user_id TEXT PRIMARY KEY, passive_ads_enabled INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ad_reward_events (
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL, provider TEXT NOT NULL,
              provider_event_id TEXT UNIQUE NOT NULL, amount INTEGER NOT NULL,
              verified_at TEXT NOT NULL, metadata_sanitized TEXT
            );
            """
        )

    # -- time helpers -------------------------------------------------------
    @staticmethod
    def _day(now: datetime) -> str:
        return now.astimezone(timezone.utc).date().isoformat()

    @staticmethod
    def _cycle_end(now: datetime) -> str:
        """Midnight UTC after ``now`` — when this cycle's daily YK expires."""
        tomorrow = now.astimezone(timezone.utc).date() + timedelta(days=1)
        return datetime.combine(tomorrow, time.min, tzinfo=timezone.utc).isoformat()

    # -- preferences --------------------------------------------------------
    def passive_ads_enabled(self, user_id: str) -> bool:
        row = self._db.execute(
            "SELECT passive_ads_enabled FROM user_ad_preferences WHERE user_id=?", (user_id,)
        ).fetchone()
        # Closed-beta default: off until the user opts in.
        return bool(row["passive_ads_enabled"]) if row else False

    def set_passive_ads(self, user_id: str, enabled: bool, *, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO user_ad_preferences VALUES (?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET passive_ads_enabled=excluded.passive_ads_enabled, "
                "updated_at=excluded.updated_at",
                (user_id, 1 if enabled else 0, now.isoformat()),
            )
        return bool(enabled)

    # -- balances -----------------------------------------------------------
    def _bucket_total(self, user_id: str, bucket: str, now: datetime) -> int:
        """Active balance: rows that never expire plus rows not yet expired.

        Nothing is mutated or deleted — an expired credit and the debit that
        inherited its expiry simply stop matching the filter together.
        """
        stamp = now.astimezone(timezone.utc).isoformat()
        row = self._db.execute(
            "SELECT COALESCE(SUM(amount),0) AS total FROM yk_ledger "
            "WHERE user_id=? AND bucket=? AND (expires_at IS NULL OR expires_at>?)",
            (user_id, bucket, stamp),
        ).fetchone()
        return int(row["total"])

    def balances(self, user_id: str, *, now: datetime | None = None) -> WalletBalances:
        now = now or datetime.now(timezone.utc)
        with self._lock:
            return WalletBalances(
                max(0, self._bucket_total(user_id, "daily", now)),
                max(0, self._bucket_total(user_id, "subscription", now)),
                max(0, self._bucket_total(user_id, "permanent", now)),
            )

    def summary(self, user_id: str, *, now: datetime | None = None) -> WalletSummary:
        now = now or datetime.now(timezone.utc)
        with self._lock:
            stored = self.balances(user_id, now=now)
            ads_on = self.passive_ads_enabled(user_id)
            reserved = int(self._db.execute(
                "SELECT COALESCE(SUM(amount),0) AS total FROM yk_reservations "
                "WHERE user_id=? AND status='reserved'", (user_id,)
            ).fetchone()["total"])
            return WalletSummary(
                daily_stored=stored.daily,
                daily_usable=stored.daily if ads_on else 0,
                subscription=stored.subscription,
                permanent=stored.permanent,
                reserved=reserved,
                passive_ads_enabled=ads_on,
            )

    # -- daily top-up -------------------------------------------------------
    def claim_daily(self, user_id: str, *, now: datetime | None = None,
                    plan: Plan | None = None) -> int:
        """Top up to ``plan.daily_yk_target`` once per cycle.

        Fails closed: without an active plan nothing is minted.  The permanent
        balance is only read — never reduced, converted or cleared.
        """
        now = now or datetime.now(timezone.utc)
        day = self._day(now)
        target = max(0, int(plan.daily_yk_target)) if plan is not None else 0
        with self._lock, self._db:
            if self._db.execute(
                "SELECT 1 FROM yk_daily_claims WHERE user_id=? AND claim_day=?", (user_id, day)
            ).fetchone():
                return 0
            permanent = max(0, self._bucket_total(user_id, "permanent", now))
            amount = max(0, target - permanent)
            self._db.execute(
                "INSERT INTO yk_daily_claims VALUES (?,?,?,?)",
                (user_id, day, amount, now.isoformat()),
            )
            if amount:
                self._db.execute(
                    "INSERT INTO yk_ledger VALUES (?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, user_id, amount, "daily", "daily_claim", day,
                     self._cycle_end(now), now.isoformat(), "{}"),
                )
            return amount

    # -- reservations -------------------------------------------------------
    def _open_reservation_totals(self, user_id: str) -> tuple[int, int, int]:
        row = self._db.execute(
            "SELECT COALESCE(SUM(daily_amount),0) AS d, COALESCE(SUM(subscription_amount),0) AS s, "
            "COALESCE(SUM(permanent_amount),0) AS p FROM yk_reservations "
            "WHERE user_id=? AND status='reserved'", (user_id,)
        ).fetchone()
        return int(row["d"]), int(row["s"]), int(row["p"])

    def reserve_translation(self, user_id: str, job_id: str, *, now: datetime | None = None,
                            plan: Plan | None = None) -> dict[str, Any]:
        """Reserve one chapter's cost, spending the expiring money first.

        The bucket split is decided here and frozen on the reservation, so a
        balance change mid-job cannot move the cost to another bucket.
        """
        now = now or datetime.now(timezone.utc)
        cost = max(1, int(plan.yk_cost_per_translation)) if plan is not None else TRANSLATION_COST_YK
        with self._lock, self._db:
            existing = self._db.execute(
                "SELECT * FROM yk_reservations WHERE job_id=?", (job_id,)
            ).fetchone()
            if existing:
                return dict(existing)

            ads_on = self.passive_ads_enabled(user_id)
            daily_expiry: str | None = None
            if ads_on:
                daily = max(0, self._bucket_total(user_id, "daily", now))
                stamp = now.astimezone(timezone.utc).isoformat()
                daily_expiry = self._db.execute(
                    "SELECT MAX(expires_at) AS e FROM yk_ledger WHERE user_id=? AND bucket='daily' "
                    "AND (expires_at IS NULL OR expires_at>?)", (user_id, stamp)
                ).fetchone()["e"]
            else:
                daily = 0
            subscription = max(0, self._bucket_total(user_id, "subscription", now))
            permanent = max(0, self._bucket_total(user_id, "permanent", now))

            open_d, open_s, open_p = self._open_reservation_totals(user_id)
            daily = max(0, daily - open_d)
            subscription = max(0, subscription - open_s)
            permanent = max(0, permanent - open_p)

            if daily + subscription + permanent < cost:
                raise RuntimeError("insufficient_yk")

            take_daily = min(daily, cost)
            remaining = cost - take_daily
            take_subscription = min(subscription, remaining)
            remaining -= take_subscription
            take_permanent = remaining
            if not take_daily:
                daily_expiry = None

            reservation_id = uuid.uuid4().hex
            self._db.execute(
                "INSERT INTO yk_reservations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (reservation_id, user_id, job_id, cost, "reserved", take_daily,
                 take_subscription, take_permanent, daily_expiry,
                 now.isoformat(), now.isoformat()),
            )
            return dict(self._db.execute(
                "SELECT * FROM yk_reservations WHERE id=?", (reservation_id,)
            ).fetchone())

    def consume_reservation(self, reservation_id: str, *, now: datetime | None = None) -> None:
        """Debit exactly what the reservation froze, once."""
        now = now or datetime.now(timezone.utc)
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT * FROM yk_reservations WHERE id=?", (reservation_id,)
            ).fetchone()
            if not row or row["status"] != "reserved":
                return
            debits = (
                ("daily", int(row["daily_amount"]), row["daily_expires_at"]),
                ("subscription", int(row["subscription_amount"]), None),
                ("permanent", int(row["permanent_amount"]), None),
            )
            for bucket, amount, expires_at in debits:
                if not amount:
                    continue
                # The daily debit inherits its credit's expiry so it leaves the
                # active balance on the same tick instead of living forever.
                self._db.execute(
                    "INSERT INTO yk_ledger VALUES (?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, row["user_id"], -amount, bucket, "translation_debit",
                     row["job_id"], expires_at, now.isoformat(), "{}"),
                )
            self._db.execute(
                "UPDATE yk_reservations SET status='consumed',updated_at=? WHERE id=?",
                (now.isoformat(), reservation_id),
            )

    def release_reservation(self, reservation_id: str, *, now: datetime | None = None) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE yk_reservations SET status='released',updated_at=? "
                "WHERE id=? AND status='reserved'",
                ((now or datetime.now(timezone.utc)).isoformat(), reservation_id),
            )

    # -- rewarded ads -------------------------------------------------------
    def credit_rewarded_ad(self, user_id: str, *, provider: str, provider_event_id: str,
                           amount: int = 1, plan: Plan | None = None,
                           global_enabled: bool = False,
                           now: datetime | None = None) -> dict[str, Any]:
        """Credit permanent YK for one verified reward event.

        No provider is integrated yet; this is the shape the verified server-side
        callback will use.  ``provider_event_id`` uniqueness is the anti-replay
        authority, so a repeated callback inserts nothing and credits nothing.
        """
        now = now or datetime.now(timezone.utc)
        if not str(provider).strip() or not str(provider_event_id).strip():
            raise RuntimeError("invalid_reward_event")
        if not global_enabled or plan is None or not plan.rewarded_ads_enabled:
            raise RuntimeError("rewarded_ads_disabled")
        amount = max(1, int(amount))
        day = self._day(now)
        with self._lock, self._db:
            granted_today = int(self._db.execute(
                "SELECT COUNT(*) AS n FROM ad_reward_events WHERE user_id=? AND verified_at>=?",
                (user_id, day),
            ).fetchone()["n"])
            if granted_today >= max(0, int(plan.rewarded_daily_cap)):
                raise RuntimeError("rewarded_daily_cap_reached")
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO ad_reward_events VALUES (?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, user_id, str(provider).strip(),
                 str(provider_event_id).strip(), amount, now.isoformat(), "{}"),
            )
            if cursor.rowcount != 1:
                return {"credited": False, "amount": 0, "replay": True}
            self._db.execute(
                "INSERT INTO yk_ledger VALUES (?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, user_id, amount, "permanent", "rewarded_ad",
                 str(provider_event_id).strip(), None, now.isoformat(), "{}"),
            )
            return {"credited": True, "amount": amount, "replay": False}
