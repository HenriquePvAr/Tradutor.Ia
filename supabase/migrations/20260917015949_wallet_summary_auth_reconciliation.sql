-- LOCAL READ-ONLY DIAGNOSTIC ONLY. Do not apply to production without explicit approval.
-- Preserves all functional wallet calculations and exposes only auth-scoped booleans/totals.
create or replace function public.wallet_summary() returns jsonb
language plpgsql security definer
set search_path = pg_catalog, public as $$
declare
  u uuid := auth.uid();
  d date := current_date;
  ads_on boolean; daily_stored integer;
  subscription_balance integer; permanent_balance integer; reserved_total integer;
  usable_now integer; plan public.plans%rowtype; has_plan boolean; target integer;
  claim_exists boolean; claim_amount integer;
  valid_daily_credit_total integer; daily_debit_total integer;
  active_daily_reserved_total integer; recomputed_daily_net integer;
begin
  if u is null then raise exception 'unauthorized'; end if;
  ads_on := public.passive_ads_enabled(u);
  select * into plan from public.active_plan_row(u);
  has_plan := plan.id is not null;
  target := greatest(coalesce(plan.daily_yk_target, 0), 0);
  select exists(select 1 from public.yk_daily_claims where user_id=u and claim_day=d),
         coalesce((select amount from public.yk_daily_claims where user_id=u and claim_day=d order by created_at desc limit 1),0)
    into claim_exists, claim_amount;
  select coalesce(sum(amount) filter (where amount > 0),0),
         coalesce(sum(-amount) filter (where amount < 0),0),
         coalesce(sum(amount),0)
    into valid_daily_credit_total, daily_debit_total, recomputed_daily_net
    from public.yk_ledger
   where user_id=u and bucket='daily' and (expires_at is null or expires_at > now());
  select coalesce(sum(daily_amount),0) into active_daily_reserved_total
    from public.yk_reservations where user_id=u and status='reserved';
  select coalesce(sum(amount), 0) into daily_stored from public.yk_ledger
    where user_id = u and bucket = 'daily' and (expires_at is null or expires_at > now());
  select coalesce(sum(amount), 0) into subscription_balance from public.yk_ledger
    where user_id = u and bucket = 'subscription' and (expires_at is null or expires_at > now());
  select coalesce(sum(amount), 0) into permanent_balance
    from public.yk_ledger where user_id = u and bucket = 'permanent';
  select coalesce(sum(amount), 0) into reserved_total
    from public.yk_reservations where user_id = u and status = 'reserved';
  daily_stored := greatest(daily_stored, 0); subscription_balance := greatest(subscription_balance, 0); permanent_balance := greatest(permanent_balance, 0);
  recomputed_daily_net := greatest(recomputed_daily_net, 0);
  usable_now := greatest(0, (case when ads_on then daily_stored else 0 end) + subscription_balance + permanent_balance - greatest(reserved_total, 0));
  return jsonb_build_object(
    'daily', case when ads_on then daily_stored else 0 end, 'daily_stored', daily_stored,
    'daily_usable', case when ads_on then daily_stored else 0 end, 'subscription', subscription_balance,
    'permanent', permanent_balance, 'reserved', reserved_total, 'passive_ads_enabled', ads_on,
    'usable_now', usable_now, 'usable_total', usable_now, 'has_active_plan', has_plan,
    'resolved_plan', case when has_plan then plan.slug else null end, 'daily_yk_target', target,
    'auth_context_present', (u is not null),
    'current_cycle_claim_exists_for_auth_user', claim_exists,
    'current_cycle_claim_amount_for_auth_user', claim_amount,
    'valid_daily_credit_total_for_auth_user', valid_daily_credit_total,
    'daily_debit_total_for_auth_user', daily_debit_total,
    'active_daily_reserved_total_for_auth_user', active_daily_reserved_total,
    'daily_net_stored_recomputed_for_auth_user', recomputed_daily_net,
    'wallet_daily_stored_returned', daily_stored,
    'daily_reconciliation_match', (recomputed_daily_net = daily_stored));
end $$;
revoke all on function public.wallet_summary() from public, anon;
grant execute on function public.wallet_summary() to authenticated, service_role, postgres;
