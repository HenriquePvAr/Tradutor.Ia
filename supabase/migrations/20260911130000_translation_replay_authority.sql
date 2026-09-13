-- Local/test iteration: make replay and idempotency server-authoritative.
alter table public.translation_requests
  add column if not exists request_hash text,
  add column if not exists result jsonb;

create or replace function public.begin_translation_request(
  p_request_id text,
  p_device_id uuid,
  p_reservation_id uuid,
  p_job_id text,
  p_character_count integer,
  p_request_hash text
) returns jsonb language plpgsql security definer
set search_path=pg_catalog,public as $$
declare
  u uuid := auth.uid();
  l uuid;
  r public.yk_reservations%rowtype;
  q public.translation_requests%rowtype;
  tid uuid;
begin
  if u is null then raise exception 'unauthorized'; end if;
  if nullif(trim(p_request_id),'') is null or p_character_count < 0 or nullif(trim(p_request_hash),'') is null then
    raise exception 'invalid_translation_request';
  end if;
  if coalesce((select enabled from public.beta_feature_flags where name='translation_enabled'),false)=false then raise exception 'translation_disabled'; end if;
  if coalesce((select enabled from public.beta_feature_flags where name='maintenance_mode'),false)=true then raise exception 'maintenance_mode'; end if;

  -- Replay is checked before reservation state: a completed request must be
  -- recoverable even after its reservation has been consumed.
  select * into q from public.translation_requests where request_id=trim(p_request_id) and user_id=u for update;
  if q.id is not null then
    if q.request_hash is distinct from trim(p_request_hash) or q.job_id is distinct from p_job_id then
      raise exception 'idempotency_conflict';
    end if;
    return jsonb_build_object('request_id',q.request_id,'status',case when q.result is not null and q.status='processing' then 'provider_succeeded' else q.status end,'reservation_id',q.reservation_id,'idempotent',true,'result',q.result);
  end if;

  select * into r from public.yk_reservations where id=p_reservation_id and user_id=u for update;
  if r.id is null or r.status<>'reserved' then raise exception 'reservation_invalid'; end if;
  if p_job_id is not null and trim(p_job_id)<>r.job_id then raise exception 'reservation_job_mismatch'; end if;
  select id into l from public.licenses where user_id=u and status='active' and (expires_at is null or expires_at>now()) order by expires_at desc nulls last limit 1;
  if l is null then raise exception 'license_not_found'; end if;
  if not exists(select 1 from public.license_devices where id=p_device_id and user_id=u and license_id=l and status='active') then raise exception 'device_not_found'; end if;
  insert into public.translation_requests(request_id,user_id,license_id,device_id,job_id,reservation_id,provider,status,character_count,started_at,request_hash)
    values(trim(p_request_id),u,l,p_device_id,r.job_id,p_reservation_id,'deepl','processing',p_character_count,now(),trim(p_request_hash)) returning id into tid;
  return jsonb_build_object('id',tid,'request_id',trim(p_request_id),'status','processing','reservation_id',p_reservation_id,'provider','deepl');
end $$;

revoke all on function public.begin_translation_request(text,uuid,uuid,text,integer,text) from public,anon,authenticated;
grant execute on function public.begin_translation_request(text,uuid,uuid,text,integer,text) to authenticated;

create or replace function public.persist_translation_result(p_request_id text,p_result jsonb)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare q public.translation_requests%rowtype;
begin
  select * into q from public.translation_requests where request_id=trim(p_request_id) for update;
  if q.id is null then raise exception 'translation_not_found'; end if;
  if q.status='completed' then return jsonb_build_object('request_id',q.request_id,'status','completed','idempotent',true,'result',q.result); end if;
  update public.translation_requests set result=p_result where id=q.id;
  return jsonb_build_object('request_id',q.request_id,'status','provider_succeeded','idempotent',false,'result',p_result);
end $$;
revoke all on function public.persist_translation_result(text,jsonb) from public,anon,authenticated;
grant execute on function public.persist_translation_result(text,jsonb) to service_role,postgres;

create or replace function public.commit_translation_success(
  p_request_id text,
  p_billed_characters integer default null,
  p_detected_source_language text default null,
  p_model_type_used text default null,
  p_result jsonb default null
) returns jsonb language plpgsql security definer
set search_path = pg_catalog, public as $$
declare q public.translation_requests%rowtype; r public.yk_reservations%rowtype;
begin
  select * into q from public.translation_requests where request_id=trim(p_request_id) for update;
  if q.id is null then raise exception 'translation_not_found'; end if;
  if q.status='completed' then return jsonb_build_object('request_id',q.request_id,'status','completed','idempotent',true,'result',q.result); end if;
  if q.status<>'processing' or q.provider<>'deepl' then raise exception 'translation_not_processing'; end if;
  select * into r from public.yk_reservations where id=q.reservation_id and user_id=q.user_id for update;
  if r.id is null or r.status<>'reserved' then raise exception 'reservation_not_reserved'; end if;
  update public.yk_reservations set status='consumed',updated_at=now() where id=r.id;
  if r.daily_amount>0 then insert into public.yk_ledger(user_id,amount,bucket,event_type,reference_id) values(q.user_id,-r.daily_amount,'daily','translation_debit',q.job_id); end if;
  if r.subscription_amount>0 then insert into public.yk_ledger(user_id,amount,bucket,event_type,reference_id) values(q.user_id,-r.subscription_amount,'subscription','translation_debit',q.job_id); end if;
  if r.permanent_amount>0 then insert into public.yk_ledger(user_id,amount,bucket,event_type,reference_id) values(q.user_id,-r.permanent_amount,'permanent','translation_debit',q.job_id); end if;
  insert into public.progression_ledger(user_id,amount,event_type,reference_id) values(q.user_id,10,'translation',q.request_id) on conflict do nothing;
  update public.translation_requests set status='completed',completed_at=now(),billed_characters=greatest(p_billed_characters,0),detected_source_language=left(nullif(trim(p_detected_source_language),''),32),model_type_used=left(nullif(trim(p_model_type_used),''),64),result=p_result where id=q.id;
  return jsonb_build_object('request_id',q.request_id,'status','completed','idempotent',false,'xp_credited',10,'result',p_result);
end $$;

revoke all on function public.commit_translation_success(text,integer,text,text,jsonb) from public,anon,authenticated;
grant execute on function public.commit_translation_success(text,integer,text,text,jsonb) to service_role,postgres;
