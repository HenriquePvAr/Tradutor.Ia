"""YK + ads foundation.

Two layers are asserted here:

* behaviour, against the executable model in ``commercial_foundation.YKWallet``;
* contract, against the SQL text of the migration that has to carry the same
  rules into Postgres, plus the privilege boundary around money.

Nothing in this file touches the network, Supabase or DeepL.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

from commercial_foundation import PLANS, Plan, YKWallet

ROOT = Path(__file__).resolve().parent
MIGRATION = ROOT / "supabase/migrations/20260915140000_yk_ads_foundation.sql"
SQL = MIGRATION.read_text(encoding="utf-8")

FREE = PLANS["free"]
PRO = PLANS["pro_monthly"]
DAY1 = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
DAY2 = DAY1 + timedelta(days=1)
USER = "user-1"


def wallet(*, passive_ads: bool = True) -> YKWallet:
    w = YKWallet(sqlite3.connect(":memory:", check_same_thread=False))
    w.set_passive_ads(USER, passive_ads, now=DAY1)
    return w


def credit(w: YKWallet, amount: int, bucket: str, *, expires_at: str | None = None) -> None:
    import uuid

    w._db.execute(
        "INSERT INTO yk_ledger VALUES (?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, USER, amount, bucket, "test", None, expires_at, DAY1.isoformat(), "{}"),
    )
    w._db.commit()


def translate(w: YKWallet, job_id: str, *, now: datetime = DAY1, plan: Plan = FREE) -> dict:
    reservation = w.reserve_translation(USER, job_id, now=now, plan=plan)
    w.consume_reservation(reservation["id"], now=now)
    return reservation


# ---------------------------------------------------------------------------
# Daily top-up
# ---------------------------------------------------------------------------
class DailyTopUpTests(unittest.TestCase):
    def test_top_up_reaches_plan_target_counting_permanent(self):
        for permanent, expected in ((0, 5), (1, 4), (2, 3), (3, 2), (4, 1), (5, 0), (10, 0)):
            w = wallet()
            if permanent:
                credit(w, permanent, "permanent")
            self.assertEqual(
                w.claim_daily(USER, now=DAY1, plan=FREE), expected,
                f"permanent={permanent}",
            )

    def test_plan_target_zero_grants_nothing(self):
        for permanent in (0, 1, 5, 10):
            w = wallet()
            if permanent:
                credit(w, permanent, "permanent")
            self.assertEqual(w.claim_daily(USER, now=DAY1, plan=PRO), 0)

    def test_without_active_plan_nothing_is_minted(self):
        self.assertEqual(wallet().claim_daily(USER, now=DAY1, plan=None), 0)

    def test_top_up_never_reduces_permanent(self):
        w = wallet()
        credit(w, 3, "permanent")
        w.claim_daily(USER, now=DAY1, plan=FREE)
        self.assertEqual(w.balances(USER, now=DAY1).permanent, 3)

    def test_no_continuous_refill_after_spending_in_the_same_cycle(self):
        w = wallet()
        credit(w, 3, "permanent")
        self.assertEqual(w.claim_daily(USER, now=DAY1, plan=FREE), 2)
        translate(w, "job-1")
        translate(w, "job-2")
        # The two daily went first; three permanent remain.
        balances = w.balances(USER, now=DAY1)
        self.assertEqual((balances.daily, balances.permanent), (0, 3))
        # Same cycle, no second grant even though usable is now below target.
        self.assertEqual(w.claim_daily(USER, now=DAY1, plan=FREE), 0)

    def test_next_cycle_recalculates(self):
        w = wallet()
        credit(w, 3, "permanent")
        self.assertEqual(w.claim_daily(USER, now=DAY1, plan=FREE), 2)
        self.assertEqual(w.claim_daily(USER, now=DAY2, plan=FREE), 2)


# ---------------------------------------------------------------------------
# The expiry bug
# ---------------------------------------------------------------------------
class DailyExpiryTests(unittest.TestCase):
    def test_daily_debit_does_not_outlive_its_credit(self):
        w = wallet()
        self.assertEqual(w.claim_daily(USER, now=DAY1, plan=FREE), 5)
        translate(w, "job-1")
        translate(w, "job-2")
        self.assertEqual(w.balances(USER, now=DAY1).daily, 3)

        # Next cycle: the credit expired and the debit that it funded expired
        # with it, so the bucket starts clean rather than stuck at -2.
        day2 = DAY2 + timedelta(minutes=1)
        self.assertEqual(w.balances(USER, now=day2).daily, 0)
        self.assertEqual(w.claim_daily(USER, now=day2, plan=FREE), 5)
        self.assertEqual(w.balances(USER, now=day2).daily, 5)

    def test_expired_rows_are_kept_for_audit(self):
        w = wallet()
        w.claim_daily(USER, now=DAY1, plan=FREE)
        translate(w, "job-1")
        rows = w._db.execute(
            "SELECT amount FROM yk_ledger WHERE bucket='daily' ORDER BY amount"
        ).fetchall()
        self.assertEqual([int(r["amount"]) for r in rows], [-1, 5])

    def test_reservation_captures_the_funding_expiry(self):
        w = wallet()
        w.claim_daily(USER, now=DAY1, plan=FREE)
        reservation = w.reserve_translation(USER, "job-1", now=DAY1, plan=FREE)
        self.assertEqual(reservation["daily_amount"], 1)
        self.assertTrue(reservation["daily_expires_at"].startswith("2026-09-09T00:00"))
        w.consume_reservation(reservation["id"], now=DAY1)
        debit = w._db.execute(
            "SELECT expires_at FROM yk_ledger WHERE bucket='daily' AND amount<0"
        ).fetchone()
        self.assertEqual(debit["expires_at"], reservation["daily_expires_at"])

    def test_permanent_debit_never_expires(self):
        w = wallet(passive_ads=False)
        credit(w, 2, "permanent")
        translate(w, "job-1")
        debit = w._db.execute(
            "SELECT expires_at FROM yk_ledger WHERE bucket='permanent' AND amount<0"
        ).fetchone()
        self.assertIsNone(debit["expires_at"])


# ---------------------------------------------------------------------------
# Permanent YK
# ---------------------------------------------------------------------------
class PermanentTests(unittest.TestCase):
    def test_permanent_does_not_expire(self):
        w = wallet()
        credit(w, 4, "permanent")
        far = DAY1 + timedelta(days=365)
        self.assertEqual(w.balances(USER, now=far).permanent, 4)

    def test_permanent_may_exceed_the_daily_target(self):
        w = wallet()
        credit(w, 12, "permanent")
        self.assertEqual(w.balances(USER, now=DAY1).permanent, 12)
        self.assertEqual(w.claim_daily(USER, now=DAY1, plan=FREE), 0)
        self.assertEqual(w.balances(USER, now=DAY1).permanent, 12)

    def test_permanent_is_consumable(self):
        w = wallet(passive_ads=False)
        credit(w, 3, "permanent")
        translate(w, "job-1")
        self.assertEqual(w.balances(USER, now=DAY1).permanent, 2)


# ---------------------------------------------------------------------------
# Passive ads gate
# ---------------------------------------------------------------------------
class PassiveAdsTests(unittest.TestCase):
    def test_default_is_off(self):
        w = YKWallet(sqlite3.connect(":memory:", check_same_thread=False))
        self.assertFalse(w.passive_ads_enabled(USER))

    def test_ads_on_makes_daily_usable_and_spent_first(self):
        w = wallet()
        credit(w, 3, "daily", expires_at=DAY2.isoformat())
        credit(w, 2, "permanent")
        summary = w.summary(USER, now=DAY1)
        self.assertEqual((summary.daily_usable, summary.permanent, summary.usable_total), (3, 2, 5))
        reservation = translate(w, "job-1")
        self.assertEqual(reservation["daily_amount"], 1)
        self.assertEqual(reservation["permanent_amount"], 0)

    def test_ads_off_blocks_daily_but_keeps_it_stored(self):
        w = wallet(passive_ads=False)
        credit(w, 3, "daily", expires_at=DAY2.isoformat())
        credit(w, 2, "permanent")
        summary = w.summary(USER, now=DAY1)
        self.assertEqual(summary.daily_stored, 3)
        self.assertEqual(summary.daily_usable, 0)
        self.assertEqual(summary.usable_total, 2)

        reservation = translate(w, "job-1")
        self.assertEqual(reservation["daily_amount"], 0)
        self.assertEqual(reservation["permanent_amount"], 1)
        # Daily was not touched, only blocked.
        self.assertEqual(w.balances(USER, now=DAY1).daily, 3)

    def test_ads_off_with_only_daily_cannot_translate(self):
        w = wallet(passive_ads=False)
        credit(w, 3, "daily", expires_at=DAY2.isoformat())
        with self.assertRaisesRegex(RuntimeError, "insufficient_yk"):
            w.reserve_translation(USER, "job-1", now=DAY1, plan=FREE)

    def test_re_enabling_restores_unexpired_daily_in_the_same_cycle(self):
        w = wallet(passive_ads=False)
        credit(w, 3, "daily", expires_at=DAY2.isoformat())
        self.assertEqual(w.summary(USER, now=DAY1).daily_usable, 0)
        w.set_passive_ads(USER, True, now=DAY1)
        self.assertEqual(w.summary(USER, now=DAY1).daily_usable, 3)

    def test_re_enabling_does_not_grant_again(self):
        w = wallet()
        self.assertEqual(w.claim_daily(USER, now=DAY1, plan=FREE), 5)
        w.set_passive_ads(USER, False, now=DAY1)
        w.set_passive_ads(USER, True, now=DAY1)
        self.assertEqual(w.claim_daily(USER, now=DAY1, plan=FREE), 0)


# ---------------------------------------------------------------------------
# Chapter accounting
# ---------------------------------------------------------------------------
class ChapterAccountingTests(unittest.TestCase):
    def test_one_job_one_reservation_one_yk(self):
        w = wallet()
        w.claim_daily(USER, now=DAY1, plan=FREE)
        first = w.reserve_translation(USER, "chapter-1", now=DAY1, plan=FREE)
        # Two provider batches for the same logical chapter.
        second = w.reserve_translation(USER, "chapter-1", now=DAY1, plan=FREE)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["amount"], 1)
        w.consume_reservation(first["id"], now=DAY1)
        self.assertEqual(w.balances(USER, now=DAY1).daily, 4)

    def test_finalization_replay_debits_once(self):
        w = wallet()
        w.claim_daily(USER, now=DAY1, plan=FREE)
        reservation = w.reserve_translation(USER, "chapter-1", now=DAY1, plan=FREE)
        for _ in range(5):
            w.consume_reservation(reservation["id"], now=DAY1)
        debits = w._db.execute(
            "SELECT COUNT(*) AS n FROM yk_ledger WHERE amount<0"
        ).fetchone()["n"]
        self.assertEqual(int(debits), 1)
        self.assertEqual(w.balances(USER, now=DAY1).daily, 4)

    def test_released_reservation_frees_the_balance(self):
        w = wallet()
        w.claim_daily(USER, now=DAY1, plan=FREE)
        reservation = w.reserve_translation(USER, "chapter-1", now=DAY1, plan=FREE)
        w.release_reservation(reservation["id"], now=DAY1)
        self.assertEqual(w.summary(USER, now=DAY1).reserved, 0)
        self.assertEqual(w.balances(USER, now=DAY1).daily, 5)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
class ConcurrencyTests(unittest.TestCase):
    def test_two_devices_claiming_at_once_grant_once(self):
        w = wallet()
        results: list[int] = []

        def claim():
            results.append(w.claim_daily(USER, now=DAY1, plan=FREE))

        threads = [threading.Thread(target=claim) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [0] * 7 + [5])
        self.assertEqual(w.balances(USER, now=DAY1).daily, 5)

    def test_parallel_reservations_cannot_double_spend(self):
        w = wallet()
        w.claim_daily(USER, now=DAY1, plan=FREE)  # 5 YK
        outcomes: list[str] = []

        def reserve(index: int):
            try:
                w.reserve_translation(USER, f"job-{index}", now=DAY1, plan=FREE)
                outcomes.append("ok")
            except RuntimeError as exc:
                outcomes.append(str(exc))

        threads = [threading.Thread(target=reserve, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(outcomes.count("ok"), 5)
        self.assertEqual(outcomes.count("insufficient_yk"), 7)

    def test_same_job_retried_concurrently_keeps_one_reservation(self):
        w = wallet()
        w.claim_daily(USER, now=DAY1, plan=FREE)
        ids: list[str] = []

        def reserve():
            ids.append(w.reserve_translation(USER, "same-job", now=DAY1, plan=FREE)["id"])

        threads = [threading.Thread(target=reserve) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(ids)), 1)


# ---------------------------------------------------------------------------
# Rewarded ads
# ---------------------------------------------------------------------------
class RewardedAdTests(unittest.TestCase):
    def test_reward_credits_permanent_yk(self):
        w = wallet()
        result = w.credit_rewarded_ad(
            USER, provider="test", provider_event_id="evt-1", plan=FREE,
            global_enabled=True, now=DAY1,
        )
        self.assertTrue(result["credited"])
        self.assertEqual(w.balances(USER, now=DAY1).permanent, 1)
        # Permanent, therefore still there next cycle.
        self.assertEqual(w.balances(USER, now=DAY1 + timedelta(days=30)).permanent, 1)

    def test_replayed_provider_event_credits_once(self):
        w = wallet()
        for _ in range(10):
            w.credit_rewarded_ad(
                USER, provider="test", provider_event_id="evt-1", plan=FREE,
                global_enabled=True, now=DAY1,
            )
        self.assertEqual(w.balances(USER, now=DAY1).permanent, 1)

    def test_daily_cap_is_enforced(self):
        w = wallet()
        for i in range(FREE.rewarded_daily_cap):
            w.credit_rewarded_ad(
                USER, provider="test", provider_event_id=f"evt-{i}", plan=FREE,
                global_enabled=True, now=DAY1,
            )
        with self.assertRaisesRegex(RuntimeError, "rewarded_daily_cap_reached"):
            w.credit_rewarded_ad(
                USER, provider="test", provider_event_id="evt-over", plan=FREE,
                global_enabled=True, now=DAY1,
            )

    def test_disabled_globally_or_by_plan_credits_nothing(self):
        w = wallet()
        with self.assertRaisesRegex(RuntimeError, "rewarded_ads_disabled"):
            w.credit_rewarded_ad(
                USER, provider="test", provider_event_id="evt-1", plan=FREE,
                global_enabled=False, now=DAY1,
            )
        with self.assertRaisesRegex(RuntimeError, "rewarded_ads_disabled"):
            w.credit_rewarded_ad(
                USER, provider="test", provider_event_id="evt-2", plan=PRO,
                global_enabled=True, now=DAY1,
            )
        self.assertEqual(w.balances(USER, now=DAY1).permanent, 0)


# ---------------------------------------------------------------------------
# SQL contract — the migration has to carry the same rules into Postgres
# ---------------------------------------------------------------------------
class MigrationContractTests(unittest.TestCase):
    def test_migration_sorts_after_the_applied_remote_head(self):
        names = sorted(p.name for p in (ROOT / "supabase/migrations").glob("*.sql"))
        self.assertIn(MIGRATION.name, names)
        self.assertGreater(names.index(MIGRATION.name), names.index("20260913005557_canonical_translation_commit_rpc_grants.sql"))
        self.assertGreater(MIGRATION.name, "20260915013128_chapter_level_translation_finalization.sql")

    def test_daily_target_comes_from_the_plan_not_a_literal(self):
        claim = _function_body("claim_daily_yk")
        self.assertIn("daily_yk_target", claim)
        self.assertIn("active_plan_row", claim)
        self.assertNotRegex(claim, r"greatest\s*\(\s*0\s*,\s*5\s*-")

    def test_claim_is_guarded_by_the_claim_table_and_a_lock(self):
        claim = _function_body("claim_daily_yk")
        self.assertIn("yk_daily_claims", claim)
        self.assertIn("pg_advisory_xact_lock", claim)

    def test_reservation_stores_the_daily_expiry(self):
        self.assertIn("add column if not exists daily_expires_at timestamptz", SQL)
        reserve = _function_body("reserve_translation_yk")
        self.assertIn("max(expires_at)", reserve)
        self.assertIn("daily_expires_at", reserve)

    def test_reservation_zeroes_daily_when_passive_ads_are_off(self):
        reserve = _function_body("reserve_translation_yk")
        self.assertIn("public.passive_ads_enabled(u)", reserve)
        self.assertRegex(reserve, r"else\s+d\s*:=\s*0;")

    def test_finalizer_gives_the_daily_debit_an_expiry(self):
        for name in ("finalize_translation_job", "finalize_yk_reservation"):
            body = _function_body(name)
            self.assertRegex(
                body,
                r"'daily',\s*'translation_debit',.*?daily_expires_at",
                f"{name} must inherit the reservation daily expiry",
            )

    def test_daily_is_spent_before_subscription_and_permanent(self):
        reserve = _function_body("reserve_translation_yk")
        self.assertIn("ad := least(d, plan_cost)", reserve)
        self.assertIn("asub := least(s, rem)", reserve)
        self.assertIn("ap := rem", reserve)

    def test_passive_ads_preference_is_server_side(self):
        self.assertIn("create table if not exists public.user_ad_preferences", SQL)
        self.assertIn("passive_ads_enabled boolean not null default false", SQL)
        self.assertIn("set_passive_ads_enabled", SQL)
        # The client may ask, never write the table directly.
        self.assertRegex(
            SQL,
            r"revoke insert, update, delete, truncate, references, trigger\s+"
            r"on table public\.user_ad_preferences from authenticated",
        )

    def test_wallet_summary_exposes_stored_and_usable(self):
        summary = _function_body("wallet_summary")
        for key in ("daily_stored", "daily_usable", "usable_total", "passive_ads_enabled"):
            self.assertIn(key, summary)

    def test_legacy_finalizer_loses_authenticated_execute(self):
        self.assertIn(
            "revoke all on function public.finalize_yk_reservation(uuid, boolean) "
            "from public, anon, authenticated;",
            SQL,
        )
        self.assertNotRegex(
            SQL, r"grant execute on function public\.finalize_yk_reservation[^;]*authenticated"
        )

    def test_canonical_finalizer_is_backend_only(self):
        self.assertIn(
            "revoke all on function public.finalize_translation_job(text, uuid) "
            "from public, anon, authenticated;",
            SQL,
        )
        self.assertNotRegex(
            SQL, r"grant execute on function public\.finalize_translation_job[^;]*authenticated"
        )

    def test_clients_may_only_release_work_the_provider_never_started(self):
        release = _function_body("release_yk_reservation")
        self.assertIn("reservation_in_flight", release)
        self.assertIn("status in ('processing', 'completed')", release)
        # It writes no ledger rows at all.
        self.assertNotIn("yk_ledger", release)
        self.assertIn(
            "grant execute on function public.release_yk_reservation(uuid) "
            "to authenticated, service_role, postgres;",
            SQL,
        )

    def test_ad_reward_events_has_no_client_privileges(self):
        self.assertIn(
            "revoke all on table public.ad_reward_events from public, anon, authenticated;",
            SQL,
        )
        self.assertIn("grant select, insert on table public.ad_reward_events to service_role;", SQL)
        self.assertNotRegex(
            SQL, r"grant[^;]*on table public\.ad_reward_events[^;]*\b(anon|authenticated)\b"
        )

    def test_money_tables_lose_write_and_truncate(self):
        self.assertRegex(
            SQL,
            r"revoke insert, update, delete, truncate, references, trigger\s+"
            r"on table public\.yk_ledger, public\.yk_reservations\s+"
            r"from public, anon, authenticated;",
        )
        self.assertIn(
            "revoke all on table public.yk_daily_claims from public, anon, authenticated;", SQL
        )

    def test_reward_credit_is_backend_only_and_anti_replay(self):
        reward = _function_body("credit_rewarded_ad")
        self.assertIn("on conflict (provider_event_id) do nothing", reward)
        self.assertIn("'permanent', 'rewarded_ad'", reward)
        self.assertIn("rewarded_daily_cap_reached", reward)
        # Both switches, and no client role may call it.
        self.assertIn("beta_feature_flags", reward)
        self.assertIn("plan.rewarded_ads_enabled is not true", reward)
        self.assertIn(
            "revoke all on function public.credit_rewarded_ad(uuid, text, text, integer, jsonb) "
            "from public, anon, authenticated;",
            SQL,
        )

    def test_migration_does_not_mutate_production_rows(self):
        lowered = SQL.lower()
        for forbidden in ("delete from public.yk_", "update public.yk_ledger",
                          "truncate table", "drop table"):
            self.assertNotIn(forbidden, lowered)


class EdgeContractTests(unittest.TestCase):
    def test_wallet_finalize_no_longer_reaches_the_legacy_finalizer(self):
        edge = (ROOT / "supabase/functions/wallet-finalize/index.ts").read_text(encoding="utf-8")
        self.assertNotIn("finalize_yk_reservation", edge)
        self.assertIn("release_yk_reservation", edge)

    def test_rewarded_edges_are_fail_closed_and_server_authoritative(self):
        session = (ROOT / "supabase/functions/rewarded-ad-session/index.ts").read_text(encoding="utf-8")
        callback = (ROOT / "supabase/functions/rewarded-ad-callback/index.ts").read_text(encoding="utf-8")
        self.assertIn("create_rewarded_ad_session", session)
        self.assertIn("requireAuth", session)
        self.assertIn("AYET_PUBLISHER_API_KEY", callback)
        self.assertIn("credit_ayet_rewarded_ad", callback)
        self.assertNotIn("yk_ledger", callback)


def _function_body(name: str) -> str:
    """Return the ``$$ ... $$`` body of a function defined in the migration."""
    match = re.search(
        rf"create or replace function public\.{re.escape(name)}\b.*?\$\$(.*?)\$\$;",
        SQL,
        re.DOTALL,
    )
    if not match:
        raise AssertionError(f"{name} not found in {MIGRATION.name}")
    return match.group(1)


if __name__ == "__main__":
    unittest.main()
