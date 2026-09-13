-- Security hardening: keep provider outcome and XP server-authoritative.
alter table public.yk_reservations
  add column if not exists daily_amount integer not null default 0,
  add column if not exists subscription_amount integer not null default 0,
  add column if not exists permanent_amount integer not null default 0;
-- Existing client-shaped begin signature is deliberately fail-closed.
create or replace function public.begin_translation_request(p_request_id text,p_device_id uuid,p_job_id text default null,p_character_count integer default 0)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
begin perform p_request_id; perform p_device_id; perform p_job_id; perform p_character_count; raise exception 'reservation_required'; end $$;
create or replace function public.begin_translation_request(p_request_id text,p_device_id uuid,p_reservation_id uuid,p_job_id text default null,p_character_count integer default 0)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare u uuid:=auth.uid(); l uuid; r public.yk_reservations%rowtype; tid uuid;
begin
  if u is null then raise exception 'unauthorized'; end if;
  if nullif(trim(p_request_id),'') is null or p_character_count<0 then raise exception 'invalid_translation_request'; end if;
  if coalesce((select enabled from public.beta_feature_flags where name='translation_enabled'),false)=false then raise exception 'translation_disabled'; end if;
  if coalesce((select enabled from public.beta_feature_flags where name='maintenance_mode'),false)=true then raise exception 'maintenance_mode'; end if;
  select * into r from public.yk_reservations where id=p_reservation_id and user_id=u for update;
  if r.id is null or r.status<>'reserved' then raise exception 'reservation_invalid'; end if;
  if p_job_id is not null and trim(p_job_id)<>r.job_id then raise exception 'reservation_job_mismatch'; end if;
  select id into l from public.licenses where user_id=u and status='active' and (expires_at is null or expires_at>now()) order by expires_at desc nulls last limit 1;
  if l is null then raise exception 'license_not_found'; end if;
  if not exists(select 1 from public.license_devices where id=p_device_id and user_id=u and license_id=l and status='active') then raise exception 'device_not_found'; end if;
  if exists(select 1 from public.translation_requests where request_id=trim(p_request_id) and user_id=u) then return (select jsonb_build_object('request_id',request_id,'status',status,'reservation_id',reservation_id,'idempotent',true) from public.translation_requests where request_id=trim(p_request_id) and user_id=u); end if;
  insert into public.translation_requests(request_id,user_id,license_id,device_id,job_id,reservation_id,provider,status,character_count,started_at)
    values(trim(p_request_id),u,l,p_device_id,r.job_id,p_reservation_id,'deepl','processing',p_character_count,now()) returning id into tid;
  return jsonb_build_object('id',tid,'request_id',trim(p_request_id),'status','processing','reservation_id',p_reservation_id,'provider','deepl');
end $$;
-- Provider completion/failure and XP are server-only; service_role is used by the provider worker path.
revoke execute on function public.award_translation_xp(text,integer),public.complete_translation_request(text),public.fail_translation_request(text,text) from authenticated,anon,public;
grant execute on function public.award_translation_xp(text,integer),public.complete_translation_request(text),public.fail_translation_request(text,text) to service_role;
-- Safe defaults: feature flags are authoritative and fail closed.
update public.beta_feature_flags set enabled=false where name in ('translation_enabled','billing_enabled','rewarded_ads_enabled','maintenance_mode');
insert into public.beta_feature_flags(name,enabled) values
 ('translation_enabled',false),('billing_enabled',false),('rewarded_ads_enabled',false),('maintenance_mode',false)
on conflict (name) do update set enabled=excluded.enabled;
create or replace function public.reserve_translation_yk(p_job_id text,p_device_id uuid,p_amount integer default 1) returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare u uuid:=auth.uid(); l uuid; rid uuid; d integer; s integer; p integer; rd integer; rs integer; rp integer; rem integer; ad integer; asub integer; ap integer;
begin
 if u is null then raise exception 'unauthorized'; end if; perform pg_advisory_xact_lock(hashtextextended(u::text,0));
 if nullif(trim(p_job_id),'') is null or p_amount<1 then raise exception 'invalid_reservation'; end if;
 select id into l from public.licenses where user_id=u and status='active' and (expires_at is null or expires_at>now()) order by expires_at desc nulls last limit 1;
 if l is null then raise exception 'license_not_found'; end if;
 if not exists(select 1 from public.license_devices where id=p_device_id and user_id=u and license_id=l and status='active') then raise exception 'device_not_found'; end if;
 select id into rid from public.yk_reservations where user_id=u and job_id=trim(p_job_id); if rid is not null then return jsonb_build_object('reservation_id',rid,'already_reserved',true); end if;
 select coalesce(sum(amount),0) into d from public.yk_ledger where user_id=u and bucket='daily' and (expires_at is null or expires_at>now());
 select coalesce(sum(amount),0) into s from public.yk_ledger where user_id=u and bucket='subscription' and (expires_at is null or expires_at>now());
 select coalesce(sum(amount),0) into p from public.yk_ledger where user_id=u and bucket='permanent';
 select coalesce(sum(daily_amount),0),coalesce(sum(subscription_amount),0),coalesce(sum(permanent_amount),0) into rd,rs,rp from public.yk_reservations where user_id=u and status='reserved';
 d:=greatest(0,d-rd); s:=greatest(0,s-rs); p:=greatest(0,p-rp); if d+s+p<p_amount then raise exception 'insufficient_yk'; end if;
 ad:=least(d,p_amount); rem:=p_amount-ad; asub:=least(s,rem); rem:=rem-asub; ap:=rem;
 insert into public.yk_reservations(user_id,job_id,amount,status,daily_amount,subscription_amount,permanent_amount) values(u,trim(p_job_id),p_amount,'reserved',ad,asub,ap) returning id into rid;
 return jsonb_build_object('reservation_id',rid,'amount',p_amount,'daily_amount',ad,'subscription_amount',asub,'permanent_amount',ap,'status','reserved');
end $$;
create or replace function public.finalize_yk_reservation(p_reservation_id uuid,p_release boolean default false) returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare u uuid:=auth.uid(); r public.yk_reservations%rowtype;
begin
 if u is null then raise exception 'unauthorized'; end if; select * into r from public.yk_reservations where id=p_reservation_id and user_id=u for update;
 if r.id is null then raise exception 'reservation_not_found'; end if; if r.status<>'reserved' then return jsonb_build_object('reservation_id',r.id,'status',r.status,'idempotent',true); end if;
 if p_release then update public.yk_reservations set status='released',updated_at=now() where id=r.id;
 else
   update public.yk_reservations set status='consumed',updated_at=now() where id=r.id;
   if r.daily_amount>0 then insert into public.yk_ledger(user_id,amount,bucket,event_type,reference_id) values(u,-r.daily_amount,'daily','translation_debit',r.job_id); end if;
   if r.subscription_amount>0 then insert into public.yk_ledger(user_id,amount,bucket,event_type,reference_id) values(u,-r.subscription_amount,'subscription','translation_debit',r.job_id); end if;
   if r.permanent_amount>0 then insert into public.yk_ledger(user_id,amount,bucket,event_type,reference_id) values(u,-r.permanent_amount,'permanent','translation_debit',r.job_id); end if;
 end if;
 return jsonb_build_object('reservation_id',r.id,'status',case when p_release then 'released' else 'consumed' end);
end $$;
