-- YK + ads foundation for the closed beta.
-- Bases on 20260915013128_chapter_level_translation_finalization (already applied
-- remotely).  Apply remotely only after owner review: this file moves money.
--
-- What it changes and why:
--   1. Daily debits inherit the expiry of the daily credit that funded them, so a
--      cycle's credit and debit leave the active balance together instead of the
--      debit poisoning every future cycle.
--   2. The daily top-up reads plans.daily_yk_target instead of a literal 5, so a
--      plan with target 0 mints nothing.
--   3. Passive ads become a server-stored user preference; the client may ask to
--      change it but never asserts it as part of a reservation payload.
--   4. The legacy client-callable finalizer loses its authenticated EXECUTE; the
--      only thing a client may still do is release work the provider never began.
--   5. ad_reward_events and the other money tables lose direct client privileges,
--      including TRUNCATE, which RLS does not cover.

-- ---------------------------------------------------------------------------
-- 1. Reservation carries the expiry of the daily balance that funded it.
-- ---------------------------------------------------------------------------
alter table public.yk_reservations
  add column if not exists daily_expires_at timestamptz;

-- Rewarded reward volume is plan-configurable rather than hard-coded.
alter table public.plans
  add column if not exists rewarded_daily_cap integer not null default 5;

-- ---------------------------------------------------------------------------
-- 2. Server-authoritative passive-ads preference.
-- ---------------------------------------------------------------------------
create table if not exists public.user_ad_preferences (
  user_id uuid primary key references auth.users(id) on delete cascade,
  passive_ads_enabled boolean not null default false,
  updated_at timestamptz not null default now()
);
alter table public.user_ad_preferences enable row level security;
drop policy if exists ad_prefs_self_read on public.user_ad_preferences;
create policy ad_prefs_self_read on public.user_ad_preferences
  for select using (auth.uid() = user_id);

create or replace function public.passive_ads_enabled(p_user uuid)
returns boolean language sql stable security definer
set search_path = pg_catalog, public as $$
  select coalesce(
    (select passive_ads_enabled from public.user_ad_preferences where user_id = p_user),
    false);
$$;

-- The client asks; the database decides and stores.  No reservation payload is
-- ever trusted for this value.
create or replace function public.set_passive_ads_enabled(p_enabled boolean)
returns jsonb language plpgsql security definer
set search_path = pg_catalog, public as $$
declare u uuid := auth.uid(); v boolean := coalesce(p_enabled, false);
begin
  if u is null then raise exception 'unauthorized'; end if;
  insert into public.user_ad_preferences(user_id, passive_ads_enabled, updated_at)
    values (u, v, now())
  on conflict (user_id) do update
    set passive_ads_enabled = excluded.passive_ads_enabled, updated_at = now();
  return jsonb_build_object('passive_ads_enabled', v);
end $$;

-- ---------------------------------------------------------------------------
-- 3. Active plan resolution shared by claim/reward paths.
-- ---------------------------------------------------------------------------
create or replace function public.active_plan_row(p_user uuid)
returns public.plans language sql stable security definer
set search_path = pg_catalog, public as $$
  select p.*
    from public.licenses l
    join public.plans p on p.id = l.plan_id
   where l.user_id = p_user
     and l.status = 'active'
     and (l.expires_at is null or l.expires_at > now())
     and p.active = true
   order by l.expires_at desc nulls last
   limit 1;
$$;

-- ---------------------------------------------------------------------------
-- 4. Daily top-up: plan target, once per cycle, never reducing permanent.
-- ---------------------------------------------------------------------------
create or replace function public.claim_daily_yk() returns jsonb
language plpgsql security definer
set search_path = pg_catalog, public as $$
declare
  u uuid := auth.uid();
  d date := current_date;
  plan public.plans%rowtype;
  target integer;
  permanent_balance integer;
  grant_amount integer;
begin
  if u is null then raise exception 'unauthorized'; end if;
  perform pg_advisory_xact_lock(hashtextextended(u::text, 0));
  if exists (select 1 from public.yk_daily_claims where user_id = u and claim_day = d) then
    return jsonb_build_object('amount', 0, 'already_claimed', true, 'claim_day', d);
  end if;

  select * into plan from public.active_plan_row(u);
  -- Fail closed: no active license or plan means no currency is minted.
  target := greatest(coalesce(plan.daily_yk_target, 0), 0);

  select coalesce(sum(amount), 0) into permanent_balance
    from public.yk_ledger where user_id = u and bucket = 'permanent';

  -- Top-up to the target, counting permanent that the user already holds.  The
  -- permanent balance is read, never reduced or converted.
  grant_amount := greatest(0, target - greatest(permanent_balance, 0));

  insert into public.yk_daily_claims(user_id, claim_day, amount) values (u, d, grant_amount);
  if grant_amount > 0 then
    insert into public.yk_ledger(user_id, amount, bucket, event_type, reference_id, expires_at)
      values (u, grant_amount, 'daily', 'daily_claim', d::text, (d + 1)::timestamptz);
  end if;
  return jsonb_build_object('amount', grant_amount, 'already_claimed', false,
                            'claim_day', d, 'daily_yk_target', target);
end $$;

-- ---------------------------------------------------------------------------
-- 5. Wallet summary distinguishes stored from usable balance.
-- ---------------------------------------------------------------------------
create or replace function public.wallet_summary() returns jsonb
language plpgsql security definer
set search_path = pg_catalog, public as $$
declare
  u uuid := auth.uid();
  ads_on boolean;
  daily_stored integer;
  subscription_balance integer;
  permanent_balance integer;
  reserved_total integer;
begin
  if u is null then raise exception 'unauthorized'; end if;
  ads_on := public.passive_ads_enabled(u);
  select coalesce(sum(amount), 0) into daily_stored from public.yk_ledger
   where user_id = u and bucket = 'daily' and (expires_at is null or expires_at > now());
  select coalesce(sum(amount), 0) into subscription_balance from public.yk_ledger
   where user_id = u and bucket = 'subscription' and (expires_at is null or expires_at > now());
  select coalesce(sum(amount), 0) into permanent_balance from public.yk_ledger
   where user_id = u and bucket = 'permanent';
  select coalesce(sum(amount), 0) into reserved_total from public.yk_reservations
   where user_id = u and status = 'reserved';

  daily_stored := greatest(daily_stored, 0);
  subscription_balance := greatest(subscription_balance, 0);
  permanent_balance := greatest(permanent_balance, 0);

  return jsonb_build_object(
    'daily', case when ads_on then daily_stored else 0 end,
    'daily_stored', daily_stored,
    'daily_usable', case when ads_on then daily_stored else 0 end,
    'subscription', subscription_balance,
    'permanent', permanent_balance,
    'reserved', reserved_total,
    'passive_ads_enabled', ads_on,
    'usable_total', (case when ads_on then daily_stored else 0 end)
                    + subscription_balance + permanent_balance);
end $$;

-- ---------------------------------------------------------------------------
-- 6. Reservation: daily first, only while passive ads are on.
-- ---------------------------------------------------------------------------
create or replace function public.reserve_translation_yk(
  p_job_id text,
  p_device_id uuid
) returns jsonb
language plpgsql security definer
set search_path = pg_catalog, public as $$
declare
  u uuid := auth.uid();
  l uuid;
  plan_cost integer;
  rid uuid;
  ads_on boolean;
  d integer; s integer; p integer;
  rd integer; rs integer; rp integer;
  ad integer; asub integer; ap integer; rem integer;
  daily_expiry timestamptz;
begin
  if u is null then raise exception 'unauthorized'; end if;
  perform pg_advisory_xact_lock(hashtextextended(u::text, 0));
  if nullif(trim(p_job_id), '') is null then raise exception 'invalid_reservation'; end if;

  select id into l
    from public.licenses
   where user_id = u and status = 'active'
     and (expires_at is null or expires_at > now())
   order by expires_at desc nulls last limit 1;
  if l is null then raise exception 'license_not_found'; end if;
  if not exists (
    select 1 from public.license_devices
     where id = p_device_id and user_id = u and license_id = l and status = 'active'
  ) then raise exception 'device_not_found'; end if;

  select pl.yk_cost_per_translation into plan_cost
    from public.licenses lic
    join public.plans pl on pl.id = lic.plan_id
   where lic.id = l and pl.active = true;
  if plan_cost is null or plan_cost < 1 then raise exception 'pricing_unavailable'; end if;

  -- One job keeps one reservation, however many provider batches it takes.
  select id into rid from public.yk_reservations
   where user_id = u and job_id = trim(p_job_id);
  if rid is not null then
    return (select jsonb_build_object(
      'reservation_id', id, 'required_yk', amount, 'amount', amount,
      'daily_amount', daily_amount, 'subscription_amount', subscription_amount,
      'permanent_amount', permanent_amount, 'daily_expires_at', daily_expires_at,
      'status', status, 'already_reserved', true)
      from public.yk_reservations where id = rid);
  end if;

  ads_on := public.passive_ads_enabled(u);

  -- Passive ads gate usability only.  The daily ledger is never cleared, and
  -- re-enabling inside the same cycle brings the unexpired remainder back.
  if ads_on then
    select coalesce(sum(amount), 0), max(expires_at) into d, daily_expiry
      from public.yk_ledger
     where user_id = u and bucket = 'daily'
       and (expires_at is null or expires_at > now());
  else
    d := 0; daily_expiry := null;
  end if;
  select coalesce(sum(amount), 0) into s from public.yk_ledger
   where user_id = u and bucket = 'subscription'
     and (expires_at is null or expires_at > now());
  select coalesce(sum(amount), 0) into p from public.yk_ledger
   where user_id = u and bucket = 'permanent';

  select coalesce(sum(daily_amount), 0), coalesce(sum(subscription_amount), 0),
         coalesce(sum(permanent_amount), 0)
    into rd, rs, rp
    from public.yk_reservations where user_id = u and status = 'reserved';
  d := greatest(0, d - rd); s := greatest(0, s - rs); p := greatest(0, p - rp);

  if d + s + p < plan_cost then raise exception 'insufficient_yk'; end if;

  -- Spend the expiring money first.
  ad := least(d, plan_cost); rem := plan_cost - ad;
  asub := least(s, rem); rem := rem - asub; ap := rem;
  if ad = 0 then daily_expiry := null; end if;

  insert into public.yk_reservations(
      user_id, job_id, amount, status,
      daily_amount, subscription_amount, permanent_amount, daily_expires_at)
    values (u, trim(p_job_id), plan_cost, 'reserved', ad, asub, ap, daily_expiry)
    returning id into rid;

  return jsonb_build_object('reservation_id', rid, 'required_yk', plan_cost,
    'amount', plan_cost, 'daily_amount', ad, 'subscription_amount', asub,
    'permanent_amount', ap, 'daily_expires_at', daily_expiry, 'status', 'reserved');
end $$;

-- ---------------------------------------------------------------------------
-- 7. Canonical finalizer: one chapter, one YK, symmetric daily expiry.
-- ---------------------------------------------------------------------------
create or replace function public.finalize_translation_job(
  p_job_id text,
  p_reservation_id uuid
) returns jsonb
language plpgsql security definer
set search_path = pg_catalog, public as $$
declare
  q public.translation_requests%rowtype;
  r public.yk_reservations%rowtype;
  total_requests integer := 0;
  completed_requests integer := 0;
  v_user uuid;
  progression_inserted integer := 0;
begin
  if nullif(trim(p_job_id), '') is null or p_reservation_id is null then
    raise exception 'invalid_translation_job';
  end if;

  for q in select * from public.translation_requests
            where job_id = trim(p_job_id) order by request_id for update loop
    total_requests := total_requests + 1;
    if q.status = 'completed' then completed_requests := completed_requests + 1; end if;
    if q.reservation_id is distinct from p_reservation_id then
      raise exception 'reservation_job_mismatch';
    end if;
    if q.provider <> 'deepl' then raise exception 'translation_provider_mismatch'; end if;
    v_user := q.user_id;
  end loop;
  if total_requests = 0 then raise exception 'translation_job_not_found'; end if;
  if completed_requests <> total_requests then raise exception 'translation_job_not_complete'; end if;

  select * into r from public.yk_reservations
   where id = p_reservation_id and job_id = trim(p_job_id) and user_id = v_user for update;
  if r.id is null then raise exception 'reservation_not_found'; end if;
  if r.status = 'consumed' then
    return jsonb_build_object('job_id', trim(p_job_id), 'reservation_id', r.id,
      'status', 'consumed', 'idempotent', true, 'yk_debited', 0, 'xp_credited', 0);
  end if;
  if r.status <> 'reserved' then raise exception 'reservation_not_reserved'; end if;

  update public.yk_reservations set status = 'consumed', updated_at = now() where id = r.id;

  -- The daily debit inherits the expiry captured at reservation time so it
  -- leaves the active balance together with the credit that funded it.
  if r.daily_amount > 0 then
    insert into public.yk_ledger(user_id, amount, bucket, event_type, reference_id, expires_at)
      values (v_user, -r.daily_amount, 'daily', 'translation_debit', trim(p_job_id), r.daily_expires_at);
  end if;
  if r.subscription_amount > 0 then
    insert into public.yk_ledger(user_id, amount, bucket, event_type, reference_id)
      values (v_user, -r.subscription_amount, 'subscription', 'translation_debit', trim(p_job_id));
  end if;
  if r.permanent_amount > 0 then
    insert into public.yk_ledger(user_id, amount, bucket, event_type, reference_id)
      values (v_user, -r.permanent_amount, 'permanent', 'translation_debit', trim(p_job_id));
  end if;

  insert into public.progression_ledger(user_id, amount, event_type, reference_id)
    values (v_user, 10, 'translation', trim(p_job_id)) on conflict do nothing;
  get diagnostics progression_inserted = row_count;

  return jsonb_build_object('job_id', trim(p_job_id), 'reservation_id', r.id,
    'status', 'consumed', 'idempotent', false, 'yk_debited', r.amount,
    'xp_credited', case when progression_inserted = 1 then 10 else 0 end);
end $$;

-- The legacy finalizer keeps the same symmetric expiry so that a backend-only
-- call can never reintroduce the eternal-debit bug either.
create or replace function public.finalize_yk_reservation(
  p_reservation_id uuid,
  p_release boolean default false
) returns jsonb
language plpgsql security definer
set search_path = pg_catalog, public as $$
declare r public.yk_reservations%rowtype;
begin
  select * into r from public.yk_reservations where id = p_reservation_id for update;
  if r.id is null then raise exception 'reservation_not_found'; end if;
  if r.status <> 'reserved' then
    return jsonb_build_object('reservation_id', r.id, 'status', r.status, 'idempotent', true);
  end if;
  if p_release then
    update public.yk_reservations set status = 'released', updated_at = now() where id = r.id;
  else
    update public.yk_reservations set status = 'consumed', updated_at = now() where id = r.id;
    if r.daily_amount > 0 then
      insert into public.yk_ledger(user_id, amount, bucket, event_type, reference_id, expires_at)
        values (r.user_id, -r.daily_amount, 'daily', 'translation_debit', r.job_id, r.daily_expires_at);
    end if;
    if r.subscription_amount > 0 then
      insert into public.yk_ledger(user_id, amount, bucket, event_type, reference_id)
        values (r.user_id, -r.subscription_amount, 'subscription', 'translation_debit', r.job_id);
    end if;
    if r.permanent_amount > 0 then
      insert into public.yk_ledger(user_id, amount, bucket, event_type, reference_id)
        values (r.user_id, -r.permanent_amount, 'permanent', 'translation_debit', r.job_id);
    end if;
  end if;
  return jsonb_build_object('reservation_id', r.id,
    'status', case when p_release then 'released' else 'consumed' end);
end $$;

-- The only reservation transition a client still owns: abandoning work the
-- provider never started.  Anything already processing or completed must settle
-- through finalize_translation_job.
create or replace function public.release_yk_reservation(p_reservation_id uuid)
returns jsonb
language plpgsql security definer
set search_path = pg_catalog, public as $$
declare u uuid := auth.uid(); r public.yk_reservations%rowtype; started integer;
begin
  if u is null then raise exception 'unauthorized'; end if;
  select * into r from public.yk_reservations
   where id = p_reservation_id and user_id = u for update;
  if r.id is null then raise exception 'reservation_not_found'; end if;
  if r.status <> 'reserved' then
    return jsonb_build_object('reservation_id', r.id, 'status', r.status, 'idempotent', true);
  end if;
  select count(*) into started from public.translation_requests
   where job_id = r.job_id and status in ('processing', 'completed');
  if started > 0 then raise exception 'reservation_in_flight'; end if;
  update public.yk_reservations set status = 'released', updated_at = now() where id = r.id;
  return jsonb_build_object('reservation_id', r.id, 'status', 'released', 'idempotent', false);
end $$;

-- ---------------------------------------------------------------------------
-- 8. Rewarded-ad credit boundary.  No provider is integrated yet; this is the
--    only door a verified server-side callback will be allowed to use, and it
--    is unreachable from any client role.
-- ---------------------------------------------------------------------------
create or replace function public.credit_rewarded_ad(
  p_user uuid,
  p_provider text,
  p_provider_event_id text,
  p_amount integer default 1,
  p_metadata jsonb default '{}'::jsonb
) returns jsonb
language plpgsql security definer
set search_path = pg_catalog, public as $$
declare
  plan public.plans%rowtype;
  cap integer;
  granted_today integer;
  amt integer := greatest(coalesce(p_amount, 1), 1);
  inserted integer;
begin
  if p_user is null
     or nullif(trim(coalesce(p_provider, '')), '') is null
     or nullif(trim(coalesce(p_provider_event_id, '')), '') is null then
    raise exception 'invalid_reward_event';
  end if;
  -- Both switches must be on: the global beta flag and the plan's own flag.
  if not coalesce((select enabled from public.beta_feature_flags
                    where name = 'rewarded_ads_enabled'), false) then
    raise exception 'rewarded_ads_disabled';
  end if;
  perform pg_advisory_xact_lock(hashtextextended(p_user::text, 0));
  select * into plan from public.active_plan_row(p_user);
  if plan.id is null or plan.rewarded_ads_enabled is not true then
    raise exception 'rewarded_ads_disabled';
  end if;

  cap := greatest(coalesce(plan.rewarded_daily_cap, 0), 0);
  select count(*) into granted_today from public.ad_reward_events
   where user_id = p_user and verified_at >= date_trunc('day', now());
  if granted_today >= cap then raise exception 'rewarded_daily_cap_reached'; end if;

  -- provider_event_id UNIQUE is the anti-replay authority: a repeated callback
  -- inserts nothing and therefore credits nothing.
  insert into public.ad_reward_events(user_id, provider, provider_event_id, amount, metadata_sanitized)
    values (p_user, trim(p_provider), trim(p_provider_event_id), amt, coalesce(p_metadata, '{}'::jsonb))
  on conflict (provider_event_id) do nothing;
  get diagnostics inserted = row_count;
  if inserted <> 1 then
    return jsonb_build_object('credited', false, 'amount', 0, 'replay', true);
  end if;

  -- Rewarded ads mint permanent YK, which never expires.
  insert into public.yk_ledger(user_id, amount, bucket, event_type, reference_id)
    values (p_user, amt, 'permanent', 'rewarded_ad', trim(p_provider_event_id));
  return jsonb_build_object('credited', true, 'amount', amt, 'replay', false);
end $$;

-- ---------------------------------------------------------------------------
-- 9. Function privileges.
-- ---------------------------------------------------------------------------
revoke all on function public.finalize_yk_reservation(uuid, boolean) from public, anon, authenticated;
grant execute on function public.finalize_yk_reservation(uuid, boolean) to service_role, postgres;

revoke all on function public.finalize_translation_job(text, uuid) from public, anon, authenticated;
grant execute on function public.finalize_translation_job(text, uuid) to service_role, postgres;

revoke all on function public.credit_rewarded_ad(uuid, text, text, integer, jsonb) from public, anon, authenticated;
grant execute on function public.credit_rewarded_ad(uuid, text, text, integer, jsonb) to service_role, postgres;

revoke all on function public.active_plan_row(uuid) from public, anon, authenticated;
grant execute on function public.active_plan_row(uuid) to service_role, postgres;

revoke all on function public.passive_ads_enabled(uuid) from public, anon, authenticated;
grant execute on function public.passive_ads_enabled(uuid) to service_role, postgres;

revoke all on function public.release_yk_reservation(uuid) from public, anon;
grant execute on function public.release_yk_reservation(uuid) to authenticated, service_role, postgres;

revoke all on function public.set_passive_ads_enabled(boolean) from public, anon;
grant execute on function public.set_passive_ads_enabled(boolean) to authenticated, service_role, postgres;

revoke all on function public.claim_daily_yk() from public, anon;
grant execute on function public.claim_daily_yk() to authenticated, service_role, postgres;

revoke all on function public.wallet_summary() from public, anon;
grant execute on function public.wallet_summary() to authenticated, service_role, postgres;

revoke all on function public.reserve_translation_yk(text, uuid) from public, anon;
grant execute on function public.reserve_translation_yk(text, uuid) to authenticated, service_role, postgres;

-- ---------------------------------------------------------------------------
-- 10. Table privileges.  RLS does not restrict TRUNCATE, so the privilege has
--     to be taken away directly.
-- ---------------------------------------------------------------------------
revoke all on table public.ad_reward_events from public, anon, authenticated;
grant select, insert on table public.ad_reward_events to service_role;

revoke all on table public.yk_daily_claims from public, anon, authenticated;
grant select, insert, update, delete on table public.yk_daily_claims to service_role;

revoke insert, update, delete, truncate, references, trigger
  on table public.yk_ledger, public.yk_reservations
  from public, anon, authenticated;
revoke select on table public.yk_ledger, public.yk_reservations from public, anon;
grant select on table public.yk_ledger, public.yk_reservations to authenticated;
grant select, insert, update, delete on table public.yk_ledger, public.yk_reservations to service_role;

revoke all on table public.user_ad_preferences from public, anon;
revoke insert, update, delete, truncate, references, trigger
  on table public.user_ad_preferences from authenticated;
grant select on table public.user_ad_preferences to authenticated;
grant select, insert, update, delete on table public.user_ad_preferences to service_role;
