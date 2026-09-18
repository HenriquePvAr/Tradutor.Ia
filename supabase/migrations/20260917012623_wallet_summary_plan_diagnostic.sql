-- Read-only plan diagnostics; no wallet, claim, cooldown, or accounting changes.
create or replace function public.wallet_summary() returns jsonb
language plpgsql security definer
set search_path = pg_catalog, public as $$
declare
  u uuid := auth.uid(); ads_on boolean; daily_stored integer;
  subscription_balance integer; permanent_balance integer; reserved_total integer;
  usable_now integer; plan public.plans%rowtype; has_plan boolean; target integer;
begin
  if u is null then raise exception 'unauthorized'; end if;
  ads_on := public.passive_ads_enabled(u);
  select * into plan from public.active_plan_row(u);
  has_plan := plan.id is not null;
  target := greatest(coalesce(plan.daily_yk_target, 0), 0);
  select coalesce(sum(amount), 0) into daily_stored from public.yk_ledger where user_id = u and bucket = 'daily' and (expires_at is null or expires_at > now());
  select coalesce(sum(amount), 0) into subscription_balance from public.yk_ledger where user_id = u and bucket = 'subscription' and (expires_at is null or expires_at > now());
  select coalesce(sum(amount), 0) into permanent_balance from public.yk_ledger where user_id = u and bucket = 'permanent';
  select coalesce(sum(amount), 0) into reserved_total from public.yk_reservations where user_id = u and status = 'reserved';
  daily_stored := greatest(daily_stored, 0); subscription_balance := greatest(subscription_balance, 0); permanent_balance := greatest(permanent_balance, 0);
  usable_now := greatest(0, (case when ads_on then daily_stored else 0 end) + subscription_balance + permanent_balance - greatest(reserved_total, 0));
  return jsonb_build_object('daily', case when ads_on then daily_stored else 0 end, 'daily_stored', daily_stored,
    'daily_usable', case when ads_on then daily_stored else 0 end, 'subscription', subscription_balance,
    'permanent', permanent_balance, 'reserved', reserved_total, 'passive_ads_enabled', ads_on,
    'usable_now', usable_now, 'usable_total', usable_now, 'has_active_plan', has_plan,
    'resolved_plan', case when has_plan then plan.slug else null end, 'daily_yk_target', target);
end $$;
revoke all on function public.wallet_summary() from public, anon;
grant execute on function public.wallet_summary() to authenticated, service_role, postgres;
