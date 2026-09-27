-- LOCAL ONLY: do not apply without explicit production authorization.
-- DAILY debits without a funding expiry are legacy rows with no safe cycle
-- attribution. They remain in the ledger for audit, but cannot consume a
-- later expiring grant. New reservations/debits retain the captured expiry.

create or replace function public.wallet_summary() returns jsonb
language plpgsql security definer set search_path=pg_catalog,public as $$
declare
  u uuid:=auth.uid(); d date:=current_date; ads_on boolean; daily_stored integer;
  subscription_balance integer; permanent_balance integer; reserved_total integer;
  usable_now integer; plan public.plans%rowtype; has_plan boolean; target integer;
  claim_exists boolean; claim_amount integer; valid_daily_credit_total integer;
  daily_debit_total integer; active_daily_reserved_total integer; recomputed_daily_net integer;
begin
  if u is null then raise exception 'unauthorized'; end if;
  ads_on:=public.passive_ads_enabled(u);
  select * into plan from public.active_plan_row(u);
  has_plan:=plan.id is not null; target:=greatest(coalesce(plan.daily_yk_target,0),0);
  select exists(select 1 from public.yk_daily_claims where user_id=u and claim_day=d),
    coalesce((select amount from public.yk_daily_claims where user_id=u and claim_day=d order by created_at desc limit 1),0)
    into claim_exists,claim_amount;
  select coalesce(sum(amount) filter(where amount>0),0),coalesce(sum(-amount) filter(where amount<0),0),coalesce(sum(amount),0)
    into valid_daily_credit_total,daily_debit_total,recomputed_daily_net
    from public.yk_ledger where user_id=u and bucket='daily' and expires_at>now();
  select coalesce(sum(daily_amount),0) into active_daily_reserved_total from public.yk_reservations where user_id=u and status='reserved';
  select coalesce(sum(amount),0) into daily_stored from public.yk_ledger where user_id=u and bucket='daily' and expires_at>now();
  select coalesce(sum(amount),0) into subscription_balance from public.yk_ledger where user_id=u and bucket='subscription' and (expires_at is null or expires_at>now());
  select coalesce(sum(amount),0) into permanent_balance from public.yk_ledger where user_id=u and bucket='permanent';
  select coalesce(sum(amount),0) into reserved_total from public.yk_reservations where user_id=u and status='reserved';
  daily_stored:=greatest(daily_stored,0); subscription_balance:=greatest(subscription_balance,0); permanent_balance:=greatest(permanent_balance,0); recomputed_daily_net:=greatest(recomputed_daily_net,0);
  usable_now:=greatest(0,(case when ads_on then daily_stored else 0 end)+subscription_balance+permanent_balance-greatest(reserved_total,0));
  return jsonb_build_object('daily',case when ads_on then daily_stored else 0 end,'daily_stored',daily_stored,'daily_usable',case when ads_on then daily_stored else 0 end,'subscription',subscription_balance,'permanent',permanent_balance,'reserved',reserved_total,'passive_ads_enabled',ads_on,'usable_now',usable_now,'usable_total',usable_now,'has_active_plan',has_plan,'resolved_plan',case when has_plan then plan.slug else null end,'daily_yk_target',target,'auth_context_present',(u is not null),'current_cycle_claim_exists_for_auth_user',claim_exists,'current_cycle_claim_amount_for_auth_user',claim_amount,'valid_daily_credit_total_for_auth_user',valid_daily_credit_total,'daily_debit_total_for_auth_user',daily_debit_total,'active_daily_reserved_total_for_auth_user',active_daily_reserved_total,'daily_net_stored_recomputed_for_auth_user',recomputed_daily_net,'wallet_daily_stored_returned',daily_stored,'daily_reconciliation_match',(recomputed_daily_net=daily_stored));
end $$;
revoke all on function public.wallet_summary() from public,anon;
grant execute on function public.wallet_summary() to authenticated,service_role,postgres;

create or replace function public.reserve_translation_yk(p_job_id text,p_device_id uuid) returns jsonb
language plpgsql security definer set search_path=pg_catalog,public as $$
declare u uuid:=auth.uid(); l uuid; plan_cost integer; rid uuid; ads_on boolean; d integer; s integer; p integer; rd integer; rs integer; rp integer; ad integer; asub integer; ap integer; rem integer; daily_expiry timestamptz;
begin
  if u is null then raise exception 'unauthorized'; end if;
  perform pg_advisory_xact_lock(hashtextextended(u::text,0));
  if nullif(trim(p_job_id),'') is null then raise exception 'invalid_reservation'; end if;
  select id into l from public.licenses where user_id=u and status='active' and (expires_at is null or expires_at>now()) order by expires_at desc nulls last limit 1;
  if l is null then raise exception 'license_not_found'; end if;
  if not exists(select 1 from public.license_devices where id=p_device_id and user_id=u and license_id=l and status='active') then raise exception 'device_not_found'; end if;
  select pl.yk_cost_per_translation into plan_cost from public.licenses lic join public.plans pl on pl.id=lic.plan_id where lic.id=l and pl.active=true;
  if plan_cost is null or plan_cost<1 then raise exception 'pricing_unavailable'; end if;
  select id into rid from public.yk_reservations where user_id=u and job_id=trim(p_job_id);
  if rid is not null then return(select jsonb_build_object('reservation_id',id,'required_yk',amount,'amount',amount,'daily_amount',daily_amount,'subscription_amount',subscription_amount,'permanent_amount',permanent_amount,'daily_expires_at',daily_expires_at,'status',status,'already_reserved',true) from public.yk_reservations where id=rid); end if;
  ads_on:=public.passive_ads_enabled(u);
  if ads_on then
    select coalesce(sum(amount),0),max(expires_at) into d,daily_expiry from public.yk_ledger where user_id=u and bucket='daily' and expires_at>now();
  else d:=0; daily_expiry:=null; end if;
  select coalesce(sum(amount),0) into s from public.yk_ledger where user_id=u and bucket='subscription' and (expires_at is null or expires_at>now());
  select coalesce(sum(amount),0) into p from public.yk_ledger where user_id=u and bucket='permanent';
  select coalesce(sum(daily_amount),0),coalesce(sum(subscription_amount),0),coalesce(sum(permanent_amount),0) into rd,rs,rp from public.yk_reservations where user_id=u and status='reserved';
  d:=greatest(0,d-rd); s:=greatest(0,s-rs); p:=greatest(0,p-rp);
  if d+s+p<plan_cost then raise exception 'insufficient_yk'; end if;
  ad:=least(d,plan_cost); rem:=plan_cost-ad; asub:=least(s,rem); rem:=rem-asub; ap:=rem; if ad=0 then daily_expiry:=null; end if;
  insert into public.yk_reservations(user_id,job_id,amount,status,daily_amount,subscription_amount,permanent_amount,daily_expires_at) values(u,trim(p_job_id),plan_cost,'reserved',ad,asub,ap,daily_expiry) returning id into rid;
  return jsonb_build_object('reservation_id',rid,'required_yk',plan_cost,'amount',plan_cost,'daily_amount',ad,'subscription_amount',asub,'permanent_amount',ap,'daily_expires_at',daily_expiry,'status','reserved');
end $$;

-- Preserve legacy finalization while ensuring new daily debits inherit funding expiry.
create or replace function public.finalize_yk_reservation(p_reservation_id uuid,p_release boolean default false) returns jsonb
language plpgsql security definer set search_path=pg_catalog,public as $$
declare r public.yk_reservations%rowtype;
begin
  select * into r from public.yk_reservations where id=p_reservation_id for update;
  if r.id is null then raise exception 'reservation_not_found'; end if;
  if r.status<>'reserved' then return jsonb_build_object('reservation_id',r.id,'status',r.status,'idempotent',true); end if;
  if p_release then update public.yk_reservations set status='released',updated_at=now() where id=r.id;
  else
    update public.yk_reservations set status='consumed',updated_at=now() where id=r.id;
    if r.daily_amount>0 then insert into public.yk_ledger(user_id,amount,bucket,event_type,reference_id,expires_at) values(r.user_id,-r.daily_amount,'daily','translation_debit',r.job_id,r.daily_expires_at); end if;
    if r.subscription_amount>0 then insert into public.yk_ledger(user_id,amount,bucket,event_type,reference_id) values(r.user_id,-r.subscription_amount,'subscription','translation_debit',r.job_id); end if;
    if r.permanent_amount>0 then insert into public.yk_ledger(user_id,amount,bucket,event_type,reference_id) values(r.user_id,-r.permanent_amount,'permanent','translation_debit',r.job_id); end if;
  end if;
  return jsonb_build_object('reservation_id',r.id,'status',case when p_release then 'released' else 'consumed' end);
end $$;
