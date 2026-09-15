-- ayeT rewarded-video session boundary. Apply remotely only after provider
-- account/domain configuration and owner review.
create table if not exists public.rewarded_ad_sessions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  ads_external_id text not null unique,
  provider text not null check (provider = 'ayet'),
  adslot_id text not null,
  status text not null default 'created' check (status in ('created','rewarded','expired','cancelled')),
  expires_at timestamptz not null,
  created_at timestamptz not null default now(),
  rewarded_at timestamptz
);
create index if not exists rewarded_ad_sessions_user_status_idx
  on public.rewarded_ad_sessions(user_id, status, expires_at);
alter table public.rewarded_ad_sessions enable row level security;
revoke all on public.rewarded_ad_sessions from public, anon, authenticated;
grant select, insert, update on public.rewarded_ad_sessions to service_role, postgres;

create or replace function public.create_rewarded_ad_session(p_adslot_id text default 'rewarded_video')
returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare u uuid := auth.uid(); sid uuid; external_id text; begin
  if u is null then raise exception 'unauthorized'; end if;
  if not coalesce((select enabled from public.beta_feature_flags where name='rewarded_ads_enabled'), false)
     then raise exception 'rewarded_ads_disabled'; end if;
  if not exists (select 1 from public.licenses l join public.plans p on p.id=l.plan_id
                 where l.user_id=u and l.status='active' and (l.expires_at is null or l.expires_at > now())
                   and p.active=true and p.rewarded_ads_enabled=true)
     then raise exception 'rewarded_ads_disabled'; end if;
  if (select count(*) from public.ad_reward_events where user_id=u and verified_at >= date_trunc('day', now()))
     >= coalesce((select p.rewarded_daily_cap from public.licenses l join public.plans p on p.id=l.plan_id
                  where l.user_id=u and l.status='active' order by l.expires_at desc nulls last limit 1), 5)
     then raise exception 'rewarded_daily_cap_reached'; end if;
  sid := gen_random_uuid(); external_id := 'yk-' || replace(sid::text, '-', '');
  insert into public.rewarded_ad_sessions(id,user_id,ads_external_id,provider,adslot_id,expires_at)
    values(sid,u,external_id,'ayet',trim(p_adslot_id),now()+interval '15 minutes');
  return jsonb_build_object('session_id',sid,'external_identifier',external_id,'adslot_id',trim(p_adslot_id), 'expires_at',now()+interval '15 minutes');
end $$;
revoke all on function public.create_rewarded_ad_session(text) from public, anon;
grant execute on function public.create_rewarded_ad_session(text) to authenticated, service_role, postgres;

create or replace function public.credit_ayet_rewarded_ad(
  p_external_identifier text, p_transaction_id text, p_amount integer default 1,
  p_adslot_id text default ''
) returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare s public.rewarded_ad_sessions%rowtype; result jsonb;
begin
  if nullif(trim(p_external_identifier), '') is null or nullif(trim(p_transaction_id), '') is null
     or p_amount is distinct from 1 then raise exception 'invalid_reward_event'; end if;
  select * into s from public.rewarded_ad_sessions
   where ads_external_id=trim(p_external_identifier) and provider='ayet'
     and (p_adslot_id = '' or adslot_id=trim(p_adslot_id)) for update;
  if s.id is null then raise exception 'reward_session_not_found'; end if;
  if s.expires_at <= now() then raise exception 'reward_session_expired'; end if;
  if s.status = 'rewarded' then
    return jsonb_build_object('credited', false, 'replay', true, 'amount', 0);
  end if;
  result := public.credit_rewarded_ad(s.user_id, 'ayet', trim(p_transaction_id), p_amount,
    jsonb_build_object('session_id', s.id, 'adslot_id', s.adslot_id));
  if coalesce((result->>'credited')::boolean, false) then
    update public.rewarded_ad_sessions set status='rewarded', rewarded_at=now() where id=s.id;
  end if;
  return result;
end $$;
revoke all on function public.credit_ayet_rewarded_ad(text,text,integer,text) from public, anon, authenticated;
grant execute on function public.credit_ayet_rewarded_ad(text,text,integer,text) to service_role, postgres;
