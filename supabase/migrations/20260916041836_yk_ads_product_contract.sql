-- Product contract follow-up for the closed beta.
-- Keeps the foundation's accounting rules, but makes the ad-supported claim
-- gate explicit and exposes the canonical usable balance name to the client.

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
  if not public.passive_ads_enabled(u) then
    raise exception 'passive_ads_disabled';
  end if;
  perform pg_advisory_xact_lock(hashtextextended(u::text, 0));
  if exists (select 1 from public.yk_daily_claims where user_id = u and claim_day = d) then
    return jsonb_build_object('amount', 0, 'already_claimed', true, 'claim_day', d);
  end if;

  select * into plan from public.active_plan_row(u);
  target := greatest(coalesce(plan.daily_yk_target, 0), 0);
  select coalesce(sum(amount), 0) into permanent_balance
    from public.yk_ledger where user_id = u and bucket = 'permanent';
  grant_amount := greatest(0, target - greatest(permanent_balance, 0));

  insert into public.yk_daily_claims(user_id, claim_day, amount) values (u, d, grant_amount);
  if grant_amount > 0 then
    insert into public.yk_ledger(user_id, amount, bucket, event_type, reference_id, expires_at)
      values (u, grant_amount, 'daily', 'daily_claim', d::text, (d + 1)::timestamptz);
  end if;
  return jsonb_build_object('amount', grant_amount, 'already_claimed', false,
                            'claim_day', d, 'daily_yk_target', target);
end $$;

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
  usable_now integer;
begin
  if u is null then raise exception 'unauthorized'; end if;
  ads_on := public.passive_ads_enabled(u);
  select coalesce(sum(amount), 0) into daily_stored from public.yk_ledger
   where user_id = u and bucket = 'daily' and (expires_at is null or expires_at > now());
  select coalesce(sum(amount), 0) into subscription_balance from public.yk_ledger
   where user_id = u and bucket = 'subscription' and (expires_at is null or expires_at > now());
  select coalesce(sum(amount), 0) into permanent_balance
    from public.yk_ledger where user_id = u and bucket = 'permanent';
  select coalesce(sum(amount), 0) into reserved_total
    from public.yk_reservations where user_id = u and status = 'reserved';
  daily_stored := greatest(daily_stored, 0);
  subscription_balance := greatest(subscription_balance, 0);
  permanent_balance := greatest(permanent_balance, 0);
  usable_now := greatest(0, (case when ads_on then daily_stored else 0 end)
    + subscription_balance + permanent_balance - greatest(reserved_total, 0));
  return jsonb_build_object(
    'daily', case when ads_on then daily_stored else 0 end,
    'daily_stored', daily_stored,
    'daily_usable', case when ads_on then daily_stored else 0 end,
    'subscription', subscription_balance,
    'permanent', permanent_balance,
    'reserved', reserved_total,
    'passive_ads_enabled', ads_on,
    'usable_now', usable_now,
    'usable_total', usable_now);
end $$;

revoke all on function public.claim_daily_yk() from public, anon;
grant execute on function public.claim_daily_yk() to authenticated, service_role, postgres;
revoke all on function public.wallet_summary() from public, anon;
grant execute on function public.wallet_summary() to authenticated, service_role, postgres;

create or replace function public.get_passive_ads_preference() returns jsonb
language sql stable security definer
set search_path = pg_catalog, public as $$
  select jsonb_build_object('passive_ads_enabled', public.passive_ads_enabled(auth.uid()));
$$;
revoke all on function public.get_passive_ads_preference() from public, anon;
grant execute on function public.get_passive_ads_preference() to authenticated, service_role, postgres;
