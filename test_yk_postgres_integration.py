"""YK + ads foundation, executed against a real PostgreSQL.

``test_yk_ads_foundation.py`` proves the rules in an executable model and asserts
the SQL text.  This module proves the PL/pgSQL itself: the migration chain is
applied from scratch into a throwaway container and every money path is driven
through the real functions, with the real GRANT/REVOKE and the real RLS.

Nothing here touches the production Supabase project.  The container is created
and destroyed by the test session; it is skipped when Docker is unavailable.

Time travel: PostgreSQL's ``now()`` cannot be moved, so a "next cycle" is
produced by ageing the fixture backwards one day rather than by faking the
clock.  That is the honest direction — under the pre-fix code the daily debit
carried ``expires_at = NULL``, which ageing cannot expire, so the test still
fails loudly if the bug returns.
"""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import json
import socket
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

ROOT = Path(__file__).resolve().parent
POSTGRES_IMAGE = "postgres:15-alpine"

# Bootstrap first, then the real migrations that build the schema this feature
# lives in, in applied order, ending with the one under test.
MIGRATION_CHAIN = [
    ROOT / "tests/sql/supabase_bootstrap.sql",
    *[
        ROOT / "supabase/migrations" / name
        for name in (
            "20260908195000_commercial_beta_foundation.sql",
            "20260909090000_beta_control_plane_config.sql",
            "20260909100000_control_plane_persistence_foundation.sql",
            "20260909150000_wallet_progression_rpc.sql",
            "20260909160000_translation_lifecycle_rpc.sql",
            "20260909170000_control_plane_security_hardening.sql",
            "20260909180000_translation_rpc_grants_cleanup.sql",
            "20260909200000_deepl_server_provider_atomic_commit.sql",
            "20260911045108_server_authoritative_translation_pricing.sql",
            "20260911120000_resolve_reserve_rpc_overload.sql",
            "20260911130000_translation_replay_authority.sql",
            "20260911140000_translation_operation_claim_recovery.sql",
            "20260913005346_canonical_translation_commit_rpc.sql",
            "20260913005557_canonical_translation_commit_rpc_grants.sql",
            "20260915013128_chapter_level_translation_finalization.sql",
            "20260915140000_yk_ads_foundation.sql",
        )
    ],
]


def _docker_available() -> bool:
    try:
        return subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=30,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _postgres_container():
    if not _docker_available():
        pytest.skip("Docker daemon unavailable for the real-PostgreSQL YK integration tests")
    port = _free_local_port()
    name = f"yk-foundation-{uuid.uuid4().hex[:12]}"
    password = f"local-{uuid.uuid4().hex}"
    run = subprocess.run(
        ["docker", "run", "--rm", "-d", "--name", name,
         "-e", f"POSTGRES_PASSWORD={password}",
         "-e", "POSTGRES_DB=yk_foundation",
         "-p", f"127.0.0.1:{port}:5432", POSTGRES_IMAGE],
        capture_output=True, text=True,
    )
    if run.returncode != 0:
        pytest.skip(f"local postgres container unavailable: {run.stderr.strip()}")
    dsn = (f"host=127.0.0.1 port={port} dbname=yk_foundation "
           f"user=postgres password={password}")
    try:
        deadline = time.time() + 90
        while True:
            try:
                with psycopg.connect(dsn, connect_timeout=3) as probe:
                    probe.execute("select 1")
                break
            except psycopg.Error:
                if time.time() > deadline:
                    pytest.skip("local postgres container never became ready")
                time.sleep(0.5)
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)


@pytest.fixture(scope="session")
def migrated_dsn():
    """A database with the whole migration chain applied from scratch."""
    with _postgres_container() as dsn:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for path in MIGRATION_CHAIN:
                conn.execute(path.read_text(encoding="utf-8"))
        yield dsn


@pytest.fixture()
def db(migrated_dsn):
    with psycopg.connect(migrated_dsn, autocommit=True) as conn:
        yield Harness(conn)


class Harness:
    """Thin driver: creates users and calls RPCs the way PostgREST would."""

    def __init__(self, conn):
        self.conn = conn

    # -- fixtures -------------------------------------------------------
    def user(self, plan: str = "free"):
        uid = uuid.uuid4()
        self.conn.execute("insert into auth.users(id,email) values(%s,%s)",
                          (uid, f"{uid}@test.local"))
        plan_id = self.conn.execute(
            "select id from public.plans where slug=%s", (plan,)).fetchone()[0]
        license_id = self.conn.execute(
            "insert into public.licenses(user_id,status,plan_id) values(%s,'active',%s) "
            "returning id", (uid, plan_id)).fetchone()[0]
        device_id = self.conn.execute(
            "insert into public.license_devices(license_id,user_id,device_id) "
            "values(%s,%s,%s) returning id",
            (license_id, uid, "dev-" + uuid.uuid4().hex[:8])).fetchone()[0]
        return uid, license_id, device_id

    # -- calling as roles -----------------------------------------------
    def as_user(self, uid, sql, params=()):
        """Call as the authenticated role with auth.uid() bound to uid."""
        with self.conn.transaction():
            self.conn.execute("select set_config('request.jwt.claim.sub',%s,true)", (str(uid),))
            self.conn.execute("set local role authenticated")
            row = self.conn.execute(sql, params).fetchone()
        return row[0] if row and len(row) == 1 else row

    def as_role(self, role, uid, sql, params=()):
        with self.conn.transaction():
            self.conn.execute("select set_config('request.jwt.claim.sub',%s,true)",
                              (str(uid) if uid else "",))
            self.conn.execute(f"set local role {role}")
            row = self.conn.execute(sql, params).fetchone()
        return row[0] if row and len(row) == 1 else row

    def as_service(self, sql, params=()):
        row = self.conn.execute(sql, params).fetchone()
        return row[0] if row and len(row) == 1 else row

    # -- helpers ---------------------------------------------------------
    def credit(self, uid, amount, bucket, expires_in_days=None):
        """Grant YK directly. ``expires_in_days`` is evaluated by the server."""
        if expires_in_days is None:
            self.conn.execute(
                "insert into public.yk_ledger(user_id,amount,bucket,event_type,expires_at) "
                "values(%s,%s,%s,'test_grant',null)", (uid, amount, bucket))
        else:
            self.conn.execute(
                "insert into public.yk_ledger(user_id,amount,bucket,event_type,expires_at) "
                "values(%s,%s,%s,'test_grant', now() + make_interval(days => %s))",
                (uid, amount, bucket, int(expires_in_days)))

    def translate_chapter(self, uid, license_id, device_id, job_id, *, batches=2):
        """One logical chapter across N provider batches."""
        reservation = self.as_user(
            uid, "select public.reserve_translation_yk(%s,%s)", (job_id, device_id))
        rid = reservation["reservation_id"]
        for index in range(batches):
            self.conn.execute(
                "insert into public.translation_requests"
                "(request_id,user_id,license_id,device_id,job_id,reservation_id,provider,status)"
                " values(%s,%s,%s,%s,%s,%s,'deepl','completed')",
                (f"{job_id}-r{index}", uid, license_id, device_id, job_id, rid))
        return reservation

    def age_one_day(self, uid):
        """Move this user's history back one day so 'now' is the next cycle."""
        self.conn.execute(
            "update public.yk_ledger set expires_at = expires_at - interval '1 day', "
            "created_at = created_at - interval '1 day' where user_id=%s", (uid,))
        self.conn.execute(
            "update public.yk_daily_claims set claim_day = claim_day - 1 where user_id=%s", (uid,))

    def balances(self, uid):
        rows = self.conn.execute(
            "select bucket, coalesce(sum(amount),0) from public.yk_ledger "
            "where user_id=%s and (expires_at is null or expires_at > now()) "
            "group by bucket", (uid,)).fetchall()
        return {bucket: int(total) for bucket, total in rows}


# ---------------------------------------------------------------------------
# Migration execution
# ---------------------------------------------------------------------------
def test_migration_chain_applies_from_scratch(db):
    """The fixture itself is the assertion; this pins what it produced."""
    functions = {
        row[0] for row in db.conn.execute(
            "select p.proname from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
            "where n.nspname='public'").fetchall()
    }
    for name in ("claim_daily_yk", "reserve_translation_yk", "finalize_translation_job",
                 "release_yk_reservation", "credit_rewarded_ad",
                 "set_passive_ads_enabled", "passive_ads_enabled", "active_plan_row",
                 "wallet_summary", "finalize_yk_reservation"):
        assert name in functions, name
    assert db.conn.execute(
        "select 1 from information_schema.columns where table_name='yk_reservations' "
        "and column_name='daily_expires_at'").fetchone()
    assert db.conn.execute(
        "select 1 from information_schema.tables where table_name='user_ad_preferences'"
    ).fetchone()


# ---------------------------------------------------------------------------
# Plan target
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("permanent,expected", [(0, 5), (1, 4), (2, 3), (3, 2), (4, 1),
                                                (5, 0), (12, 0)])
def test_daily_topup_reaches_plan_target(db, permanent, expected):
    uid, _, _ = db.user("free")
    if permanent:
        db.credit(uid, permanent, "permanent")
    result = db.as_user(uid, "select public.claim_daily_yk()")
    assert result["amount"] == expected
    assert result["daily_yk_target"] == 5
    # The top-up reads permanent; it never reduces it.
    assert db.balances(uid).get("permanent", 0) == permanent


def test_plan_with_zero_target_grants_nothing(db):
    uid, _, _ = db.user("pro_monthly")
    assert db.as_user(uid, "select public.claim_daily_yk()")["amount"] == 0
    assert db.balances(uid).get("daily", 0) == 0


def test_user_without_active_license_mints_nothing(db):
    uid = uuid.uuid4()
    db.conn.execute("insert into auth.users(id,email) values(%s,%s)", (uid, f"{uid}@t.local"))
    assert db.as_user(uid, "select public.claim_daily_yk()")["amount"] == 0


def test_no_refill_after_spending_in_the_same_cycle(db):
    uid, license_id, device_id = db.user("free")
    db.credit(uid, 3, "permanent")
    db.as_user(uid, "select public.set_passive_ads_enabled(true)")
    assert db.as_user(uid, "select public.claim_daily_yk()")["amount"] == 2

    for index in range(2):
        job = f"refill-{uid}-{index}"
        reservation = db.translate_chapter(uid, license_id, device_id, job)
        db.as_service("select public.finalize_translation_job(%s,%s)",
                      (job, reservation["reservation_id"]))

    balances = db.balances(uid)
    assert balances.get("daily", 0) == 0
    assert balances.get("permanent", 0) == 3
    # Usable is below target again, but the cycle already granted.
    assert db.as_user(uid, "select public.claim_daily_yk()")["already_claimed"] is True


# ---------------------------------------------------------------------------
# The expiry bug, in real Postgres
# ---------------------------------------------------------------------------
def test_daily_debit_inherits_the_credit_expiry(db):
    uid, license_id, device_id = db.user("free")
    db.as_user(uid, "select public.set_passive_ads_enabled(true)")
    db.as_user(uid, "select public.claim_daily_yk()")

    job = f"expiry-{uid}"
    reservation = db.translate_chapter(uid, license_id, device_id, job)
    assert reservation["daily_amount"] == 1
    assert reservation["daily_expires_at"] is not None
    db.as_service("select public.finalize_translation_job(%s,%s)",
                  (job, reservation["reservation_id"]))

    credit_expiry, debit_expiry = db.conn.execute(
        "select max(expires_at) filter (where amount > 0), "
        "       max(expires_at) filter (where amount < 0) "
        "  from public.yk_ledger where user_id=%s and bucket='daily'", (uid,)).fetchone()
    assert debit_expiry is not None, "the daily debit must not be eternal"
    assert debit_expiry == credit_expiry


def test_next_cycle_starts_clean_instead_of_negative(db):
    uid, license_id, device_id = db.user("free")
    db.as_user(uid, "select public.set_passive_ads_enabled(true)")
    assert db.as_user(uid, "select public.claim_daily_yk()")["amount"] == 5

    for index in range(2):
        job = f"cycle-{uid}-{index}"
        reservation = db.translate_chapter(uid, license_id, device_id, job)
        db.as_service("select public.finalize_translation_job(%s,%s)",
                      (job, reservation["reservation_id"]))
    assert db.balances(uid).get("daily", 0) == 3

    db.age_one_day(uid)

    # Credit and debit left the active balance together: not 3, not -2, not 7.
    assert db.balances(uid).get("daily", 0) == 0
    # History is intact.
    assert int(db.conn.execute(
        "select count(*) from public.yk_ledger where user_id=%s and bucket='daily'",
        (uid,)).fetchone()[0]) == 3
    # And the new cycle grants the full target.
    assert db.as_user(uid, "select public.claim_daily_yk()")["amount"] == 5
    assert db.balances(uid).get("daily", 0) == 5


def test_permanent_never_expires_and_may_exceed_target(db):
    uid, license_id, device_id = db.user("free")
    db.credit(uid, 12, "permanent")
    db.age_one_day(uid)
    assert db.balances(uid)["permanent"] == 12
    assert db.as_user(uid, "select public.claim_daily_yk()")["amount"] == 0

    job = f"perm-{uid}"
    reservation = db.translate_chapter(uid, license_id, device_id, job)
    assert reservation["permanent_amount"] == 1
    db.as_service("select public.finalize_translation_job(%s,%s)",
                  (job, reservation["reservation_id"]))
    assert db.balances(uid)["permanent"] == 11
    assert db.conn.execute(
        "select expires_at from public.yk_ledger where user_id=%s and bucket='permanent' "
        "and amount<0", (uid,)).fetchone()[0] is None


# ---------------------------------------------------------------------------
# Passive ads gate
# ---------------------------------------------------------------------------
def test_passive_ads_default_is_off(db):
    uid, _, _ = db.user("free")
    assert db.as_user(uid, "select public.wallet_summary()")["passive_ads_enabled"] is False


def test_ads_off_blocks_daily_and_spends_permanent(db):
    uid, license_id, device_id = db.user("free")
    db.credit(uid, 3, "daily", expires_in_days=1)
    db.credit(uid, 2, "permanent")

    summary = db.as_user(uid, "select public.wallet_summary()")
    assert summary["daily_stored"] == 3
    assert summary["daily_usable"] == 0
    assert summary["permanent"] == 2
    assert summary["usable_total"] == 2

    job = f"adsoff-{uid}"
    reservation = db.translate_chapter(uid, license_id, device_id, job)
    assert reservation["daily_amount"] == 0
    assert reservation["permanent_amount"] == 1
    # Blocked, never deleted.
    assert db.balances(uid)["daily"] == 3


def test_ads_off_with_only_daily_cannot_reserve(db):
    uid, _, device_id = db.user("free")
    db.credit(uid, 3, "daily", expires_in_days=1)
    with pytest.raises(psycopg.errors.RaiseException, match="insufficient_yk"):
        db.as_user(uid, "select public.reserve_translation_yk(%s,%s)",
                   (f"blocked-{uid}", device_id))


def test_re_enabling_restores_unexpired_daily_without_a_new_grant(db):
    uid, _, _ = db.user("free")
    db.as_user(uid, "select public.set_passive_ads_enabled(true)")
    assert db.as_user(uid, "select public.claim_daily_yk()")["amount"] == 5

    db.as_user(uid, "select public.set_passive_ads_enabled(false)")
    assert db.as_user(uid, "select public.wallet_summary()")["daily_usable"] == 0

    db.as_user(uid, "select public.set_passive_ads_enabled(true)")
    assert db.as_user(uid, "select public.wallet_summary()")["daily_usable"] == 5
    # Toggling is not a way to farm a second grant.
    assert db.as_user(uid, "select public.claim_daily_yk()")["amount"] == 0


def test_preference_is_not_writable_by_the_client(db):
    uid, _, _ = db.user("free")
    db.as_user(uid, "select public.set_passive_ads_enabled(true)")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.as_user(uid,
                   "insert into public.user_ad_preferences(user_id,passive_ads_enabled) "
                   "values(%s,true) returning 1", (uid,))


# ---------------------------------------------------------------------------
# Chapter accounting
# ---------------------------------------------------------------------------
def test_one_chapter_costs_one_yk_across_two_batches(db):
    uid, license_id, device_id = db.user("free")
    db.as_user(uid, "select public.set_passive_ads_enabled(true)")
    db.as_user(uid, "select public.claim_daily_yk()")

    job = f"chapter-{uid}"
    reservation = db.translate_chapter(uid, license_id, device_id, job, batches=2)
    # Asking twice for the same job returns the same reservation.
    again = db.as_user(uid, "select public.reserve_translation_yk(%s,%s)", (job, device_id))
    assert again["reservation_id"] == reservation["reservation_id"]
    assert again["already_reserved"] is True

    result = db.as_service("select public.finalize_translation_job(%s,%s)",
                           (job, reservation["reservation_id"]))
    assert result["yk_debited"] == 1
    assert result["xp_credited"] == 10
    assert result["idempotent"] is False

    assert int(db.conn.execute(
        "select count(*) from public.yk_ledger where user_id=%s and amount<0",
        (uid,)).fetchone()[0]) == 1
    assert int(db.conn.execute(
        "select coalesce(sum(amount),0) from public.progression_ledger where user_id=%s",
        (uid,)).fetchone()[0]) == 10


def test_finalization_replay_debits_and_credits_once(db):
    uid, license_id, device_id = db.user("free")
    db.as_user(uid, "select public.set_passive_ads_enabled(true)")
    db.as_user(uid, "select public.claim_daily_yk()")
    job = f"replay-{uid}"
    reservation = db.translate_chapter(uid, license_id, device_id, job)

    first = db.as_service("select public.finalize_translation_job(%s,%s)",
                          (job, reservation["reservation_id"]))
    assert first["idempotent"] is False
    for _ in range(4):
        again = db.as_service("select public.finalize_translation_job(%s,%s)",
                              (job, reservation["reservation_id"]))
        assert again["idempotent"] is True
        assert again["yk_debited"] == 0
        assert again["xp_credited"] == 0

    assert int(db.conn.execute(
        "select count(*) from public.yk_ledger where user_id=%s and amount<0",
        (uid,)).fetchone()[0]) == 1
    assert int(db.conn.execute(
        "select coalesce(sum(amount),0) from public.progression_ledger where user_id=%s",
        (uid,)).fetchone()[0]) == 10


def test_incomplete_chapter_is_not_charged(db):
    uid, license_id, device_id = db.user("free")
    db.as_user(uid, "select public.set_passive_ads_enabled(true)")
    db.as_user(uid, "select public.claim_daily_yk()")
    job = f"partial-{uid}"
    reservation = db.as_user(uid, "select public.reserve_translation_yk(%s,%s)",
                             (job, device_id))
    rid = reservation["reservation_id"]
    for index, status in enumerate(("completed", "processing")):
        db.conn.execute(
            "insert into public.translation_requests"
            "(request_id,user_id,license_id,device_id,job_id,reservation_id,provider,status)"
            " values(%s,%s,%s,%s,%s,%s,'deepl',%s)",
            (f"{job}-r{index}", uid, license_id, device_id, job, rid, status))
    with pytest.raises(psycopg.errors.RaiseException, match="translation_job_not_complete"):
        db.as_service("select public.finalize_translation_job(%s,%s)", (job, rid))
    assert db.balances(uid)["daily"] == 5


# ---------------------------------------------------------------------------
# Release boundary
# ---------------------------------------------------------------------------
def test_client_may_release_work_the_provider_never_started(db):
    uid, _, device_id = db.user("free")
    db.as_user(uid, "select public.set_passive_ads_enabled(true)")
    db.as_user(uid, "select public.claim_daily_yk()")
    job = f"release-{uid}"
    reservation = db.as_user(uid, "select public.reserve_translation_yk(%s,%s)",
                             (job, device_id))
    released = db.as_user(uid, "select public.release_yk_reservation(%s)",
                          (reservation["reservation_id"],))
    assert released["status"] == "released"
    assert db.balances(uid)["daily"] == 5
    # No ledger row was written by the release.
    assert int(db.conn.execute(
        "select count(*) from public.yk_ledger where user_id=%s and amount<0",
        (uid,)).fetchone()[0]) == 0


def test_client_cannot_release_a_chapter_already_in_flight(db):
    uid, license_id, device_id = db.user("free")
    db.as_user(uid, "select public.set_passive_ads_enabled(true)")
    db.as_user(uid, "select public.claim_daily_yk()")
    job = f"inflight-{uid}"
    reservation = db.as_user(uid, "select public.reserve_translation_yk(%s,%s)",
                             (job, device_id))
    db.conn.execute(
        "insert into public.translation_requests"
        "(request_id,user_id,license_id,device_id,job_id,reservation_id,provider,status)"
        " values(%s,%s,%s,%s,%s,%s,'deepl','processing')",
        (f"{job}-r0", uid, license_id, device_id, job, reservation["reservation_id"]))
    with pytest.raises(psycopg.errors.RaiseException, match="reservation_in_flight"):
        db.as_user(uid, "select public.release_yk_reservation(%s)",
                   (reservation["reservation_id"],))


def test_client_cannot_release_another_users_reservation(db):
    owner, _, device_id = db.user("free")
    intruder, _, _ = db.user("free")
    db.as_user(owner, "select public.set_passive_ads_enabled(true)")
    db.as_user(owner, "select public.claim_daily_yk()")
    reservation = db.as_user(owner, "select public.reserve_translation_yk(%s,%s)",
                             (f"steal-{owner}", device_id))
    with pytest.raises(psycopg.errors.RaiseException, match="reservation_not_found"):
        db.as_user(intruder, "select public.release_yk_reservation(%s)",
                   (reservation["reservation_id"],))


# ---------------------------------------------------------------------------
# Privilege boundary
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("call", [
    "select public.finalize_yk_reservation(gen_random_uuid(), false)",
    "select public.finalize_translation_job('x', gen_random_uuid())",
    "select public.credit_rewarded_ad(gen_random_uuid(), 'p', 'e', 1)",
    "select public.active_plan_row(gen_random_uuid())",
    "select public.passive_ads_enabled(gen_random_uuid())",
])
def test_client_roles_cannot_execute_backend_money_functions(db, call):
    uid, _, _ = db.user("free")
    for role in ("authenticated", "anon"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.as_role(role, uid, call)


@pytest.mark.parametrize("table", ["ad_reward_events", "yk_ledger",
                                   "yk_reservations", "yk_daily_claims"])
@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_client_roles_cannot_truncate_money_tables(db, table, role):
    # RLS does not restrict TRUNCATE, so only the revoked privilege stops this.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.as_role(role, None, f"truncate table public.{table}")


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_client_roles_cannot_write_ad_reward_events(db, role):
    uid, _, _ = db.user("free")
    for statement in (
        "insert into public.ad_reward_events(user_id,provider,provider_event_id,amount) "
        "values(%(u)s,'p','e-'||gen_random_uuid(),1)",
        "update public.ad_reward_events set amount=99",
        "delete from public.ad_reward_events",
        "select count(*) from public.ad_reward_events",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.as_role(role, uid, statement, {"u": uid})


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_client_roles_cannot_forge_ledger_rows(db, role):
    uid, _, _ = db.user("free")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.as_role(role, uid,
                   "insert into public.yk_ledger(user_id,amount,bucket,event_type) "
                   "values(%(u)s,9999,'permanent','forged')", {"u": uid})
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.as_role(role, uid,
                   "update public.yk_reservations set status='released'")


def test_authenticated_still_reads_only_its_own_ledger(db):
    owner, _, _ = db.user("free")
    other, _, _ = db.user("free")
    db.credit(owner, 4, "permanent")
    db.conn.execute("alter table public.yk_ledger enable row level security")
    mine = db.as_role("authenticated", owner,
                      "select coalesce(sum(amount),0) from public.yk_ledger")
    theirs = db.as_role("authenticated", other,
                        "select coalesce(sum(amount),0) from public.yk_ledger")
    assert int(mine) == 4
    assert int(theirs) == 0


# ---------------------------------------------------------------------------
# Rewarded accounting
# ---------------------------------------------------------------------------
@pytest.fixture()
def rewarded_enabled(db):
    db.conn.execute(
        "insert into public.beta_feature_flags(name,enabled) values('rewarded_ads_enabled',true) "
        "on conflict (name) do update set enabled=true")
    yield
    db.conn.execute(
        "update public.beta_feature_flags set enabled=false where name='rewarded_ads_enabled'")


def test_verified_reward_credits_one_permanent_yk(db, rewarded_enabled):
    uid, _, _ = db.user("free")
    result = db.as_service(
        "select public.credit_rewarded_ad(%s,'testprovider',%s,1)", (uid, f"evt-{uid}-1"))
    assert result == {"credited": True, "amount": 1, "replay": False}
    assert db.balances(uid)["permanent"] == 1
    db.age_one_day(uid)
    assert db.balances(uid)["permanent"] == 1, "permanent must not expire"


def test_replayed_callback_credits_once(db, rewarded_enabled):
    uid, _, _ = db.user("free")
    event = f"evt-{uid}-replay"
    outcomes = [db.as_service("select public.credit_rewarded_ad(%s,'testprovider',%s,1)",
                              (uid, event)) for _ in range(10)]
    assert outcomes[0]["credited"] is True
    assert all(o["replay"] is True for o in outcomes[1:])
    assert db.balances(uid)["permanent"] == 1
    assert int(db.conn.execute(
        "select count(*) from public.ad_reward_events where user_id=%s", (uid,)
    ).fetchone()[0]) == 1


def test_daily_rewarded_cap_stops_the_sixth_reward(db, rewarded_enabled):
    uid, _, _ = db.user("free")
    for index in range(5):
        assert db.as_service("select public.credit_rewarded_ad(%s,'testprovider',%s,1)",
                             (uid, f"evt-{uid}-{index}"))["credited"] is True
    with pytest.raises(psycopg.errors.RaiseException, match="rewarded_daily_cap_reached"):
        db.as_service("select public.credit_rewarded_ad(%s,'testprovider',%s,1)",
                      (uid, f"evt-{uid}-over"))
    assert db.balances(uid)["permanent"] == 5
    # The cap limits earning, not the balance.
    db.credit(uid, 7, "permanent")
    assert db.balances(uid)["permanent"] == 12


def test_reward_denied_when_either_flag_is_off(db):
    uid, _, _ = db.user("free")
    # Global flag off (repository default).
    with pytest.raises(psycopg.errors.RaiseException, match="rewarded_ads_disabled"):
        db.as_service("select public.credit_rewarded_ad(%s,'testprovider',%s,1)",
                      (uid, f"evt-{uid}-globaloff"))
    assert db.balances(uid).get("permanent", 0) == 0


def test_reward_denied_for_a_plan_without_rewarded_ads(db, rewarded_enabled):
    uid, _, _ = db.user("pro_monthly")
    with pytest.raises(psycopg.errors.RaiseException, match="rewarded_ads_disabled"):
        db.as_service("select public.credit_rewarded_ad(%s,'testprovider',%s,1)",
                      (uid, f"evt-{uid}-planoff"))


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
def test_simultaneous_claims_from_two_devices_grant_once(db, migrated_dsn):
    uid, _, _ = db.user("free")
    results: list = []
    errors: list = []

    def claim():
        try:
            with psycopg.connect(migrated_dsn, autocommit=True) as conn:
                with conn.transaction():
                    conn.execute("select set_config('request.jwt.claim.sub',%s,true)", (str(uid),))
                    conn.execute("set local role authenticated")
                    results.append(conn.execute("select public.claim_daily_yk()").fetchone()[0])
        except Exception as exc:  # a unique-violation loser is also an acceptable outcome
            errors.append(str(exc))

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    granted = [r for r in results if r.get("amount", 0) > 0]
    assert len(granted) == 1, f"exactly one grant expected, got {results} / {errors}"
    assert granted[0]["amount"] == 5
    assert db.balances(uid)["daily"] == 5


def test_parallel_reservations_cannot_double_spend(db, migrated_dsn):
    uid, _, device_id = db.user("free")
    db.as_user(uid, "select public.set_passive_ads_enabled(true)")
    db.as_user(uid, "select public.claim_daily_yk()")  # 5 YK
    ok: list = []
    denied: list = []

    def reserve(index: int):
        try:
            with psycopg.connect(migrated_dsn, autocommit=True) as conn:
                with conn.transaction():
                    conn.execute("select set_config('request.jwt.claim.sub',%s,true)", (str(uid),))
                    conn.execute("set local role authenticated")
                    conn.execute("select public.reserve_translation_yk(%s,%s)",
                                 (f"par-{uid}-{index}", device_id))
            ok.append(index)
        except Exception as exc:
            denied.append(str(exc))

    threads = [threading.Thread(target=reserve, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(ok) == 5, f"only 5 reservations fit in 5 YK, got {len(ok)}: {denied[:2]}"
    assert all("insufficient_yk" in message for message in denied)


def test_same_job_retried_concurrently_keeps_one_reservation(db, migrated_dsn):
    uid, _, device_id = db.user("free")
    db.as_user(uid, "select public.set_passive_ads_enabled(true)")
    db.as_user(uid, "select public.claim_daily_yk()")
    ids: list = []
    errors: list = []

    def reserve():
        try:
            with psycopg.connect(migrated_dsn, autocommit=True) as conn:
                with conn.transaction():
                    conn.execute("select set_config('request.jwt.claim.sub',%s,true)", (str(uid),))
                    conn.execute("set local role authenticated")
                    row = conn.execute("select public.reserve_translation_yk(%s,%s)",
                                       (f"same-{uid}", device_id)).fetchone()[0]
            ids.append(row["reservation_id"])
        except Exception as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=reserve) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(ids)) == 1, f"one job must keep one reservation: {set(ids)} / {errors[:2]}"
    assert int(db.conn.execute(
        "select count(*) from public.yk_reservations where user_id=%s", (uid,)
    ).fetchone()[0]) == 1
