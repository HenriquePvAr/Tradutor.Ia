-- Server-authoritative translation pricing.  The client-supplied amount is
-- intentionally ignored; the active license plan is the only price source.
create or replace function public.reserve_translation_yk(
  p_job_id text,
  p_device_id uuid
) returns jsonb
language plpgsql security definer
set search_path=pg_catalog,public as $$
declare
  u uuid := auth.uid();
  l uuid;
  plan_cost integer;
  rid uuid;
  d integer;
  s integer;
  p integer;
  rd integer;
  rs integer;
  rp integer;
  ad integer;
  asub integer;
  ap integer;
  rem integer;
begin
  if u is null then raise exception 'unauthorized'; end if;
  perform pg_advisory_xact_lock(hashtextextended(u::text,0));
  if nullif(trim(p_job_id),'') is null then raise exception 'invalid_reservation'; end if;

  select id into l
    from public.licenses
   where user_id=u and status='active'
     and (expires_at is null or expires_at>now())
   order by expires_at desc nulls last limit 1;
  if l is null then raise exception 'license_not_found'; end if;
  if not exists (
    select 1 from public.license_devices
     where id=p_device_id and user_id=u and license_id=l and status='active'
  ) then raise exception 'device_not_found'; end if;

  select p.yk_cost_per_translation into plan_cost
    from public.licenses lic
    join public.plans p on p.id=lic.plan_id
   where lic.id=l and p.active=true;
  if plan_cost is null or plan_cost < 1 then raise exception 'pricing_unavailable'; end if;

  select id into rid from public.yk_reservations
   where user_id=u and job_id=trim(p_job_id);
  if rid is not null then
    return (select jsonb_build_object(
      'reservation_id',id,'required_yk',amount,'amount',amount,
      'status',status,'already_reserved',true)
      from public.yk_reservations where id=rid);
  end if;

  select coalesce(sum(amount),0) into d from public.yk_ledger
   where user_id=u and bucket='daily' and (expires_at is null or expires_at>now());
  select coalesce(sum(amount),0) into s from public.yk_ledger
   where user_id=u and bucket='subscription' and (expires_at is null or expires_at>now());
  select coalesce(sum(amount),0) into p from public.yk_ledger
   where user_id=u and bucket='permanent';
  select coalesce(sum(daily_amount),0),coalesce(sum(subscription_amount),0),coalesce(sum(permanent_amount),0)
    into rd,rs,rp from public.yk_reservations where user_id=u and status='reserved';
  d:=greatest(0,d-rd); s:=greatest(0,s-rs); p:=greatest(0,p-rp);
  if d+s+p < plan_cost then raise exception 'insufficient_yk'; end if;
  ad:=least(d,plan_cost); rem:=plan_cost-ad;
  asub:=least(s,rem); rem:=rem-asub; ap:=rem;
  insert into public.yk_reservations(user_id,job_id,amount,status,daily_amount,subscription_amount,permanent_amount)
    values(u,trim(p_job_id),plan_cost,'reserved',ad,asub,ap) returning id into rid;
  return jsonb_build_object('reservation_id',rid,'required_yk',plan_cost,'amount',plan_cost,
    'daily_amount',ad,'subscription_amount',asub,'permanent_amount',ap,'status','reserved');
end $$;

-- Compatibility for already-shipped callers: p_amount is deliberately ignored.
create or replace function public.reserve_translation_yk(
  p_job_id text, p_device_id uuid, p_amount integer default null
) returns jsonb language plpgsql security definer
set search_path=pg_catalog,public as $$
begin
  return public.reserve_translation_yk(p_job_id, p_device_id);
end $$;

revoke all on function public.reserve_translation_yk(text,uuid), public.reserve_translation_yk(text,uuid,integer) from public,anon;
grant execute on function public.reserve_translation_yk(text,uuid), public.reserve_translation_yk(text,uuid,integer) to authenticated;

;
