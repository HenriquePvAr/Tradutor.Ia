alter table public.translation_requests
  add column if not exists billed_characters integer,
  add column if not exists detected_source_language text,
  add column if not exists model_type_used text;
create or replace function public.commit_translation_success(
  p_request_id text,
  p_billed_characters integer default null,
  p_detected_source_language text default null,
  p_model_type_used text default null
) returns jsonb language plpgsql security definer
set search_path = pg_catalog, public as $$
declare q public.translation_requests%rowtype; r public.yk_reservations%rowtype;
begin
  select * into q from public.translation_requests where request_id=trim(p_request_id) for update;
  if q.id is null then raise exception 'translation_not_found'; end if;
  if q.status='completed' then return jsonb_build_object('request_id',q.request_id,'status','completed','idempotent',true); end if;
  if q.status<>'processing' or q.provider<>'deepl' then raise exception 'translation_not_processing'; end if;
  select * into r from public.yk_reservations where id=q.reservation_id and user_id=q.user_id for update;
  if r.id is null or r.status<>'reserved' then raise exception 'reservation_not_reserved'; end if;
  update public.yk_reservations set status='consumed',updated_at=now() where id=r.id;
  if r.daily_amount>0 then insert into public.yk_ledger(user_id,amount,bucket,event_type,reference_id) values(q.user_id,-r.daily_amount,'daily','translation_debit',q.job_id); end if;
  if r.subscription_amount>0 then insert into public.yk_ledger(user_id,amount,bucket,event_type,reference_id) values(q.user_id,-r.subscription_amount,'subscription','translation_debit',q.job_id); end if;
  if r.permanent_amount>0 then insert into public.yk_ledger(user_id,amount,bucket,event_type,reference_id) values(q.user_id,-r.permanent_amount,'permanent','translation_debit',q.job_id); end if;
  insert into public.progression_ledger(user_id,amount,event_type,reference_id) values(q.user_id,10,'translation',q.request_id) on conflict do nothing;
  update public.translation_requests set status='completed',completed_at=now(),billed_characters=greatest(p_billed_characters,0),detected_source_language=left(nullif(trim(p_detected_source_language),''),32),model_type_used=left(nullif(trim(p_model_type_used),''),64) where id=q.id;
  return jsonb_build_object('request_id',q.request_id,'status','completed','idempotent',false,'xp_credited',10);
end $$;
create or replace function public.record_translation_failure(p_request_id text,p_error_code text default 'provider_failed',p_release_reservation boolean default false)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare q public.translation_requests%rowtype;
begin
  select * into q from public.translation_requests where request_id=trim(p_request_id) for update;
  if q.id is null then raise exception 'translation_not_found'; end if;
  if q.status in ('failed','completed') then return jsonb_build_object('request_id',q.request_id,'status',q.status,'idempotent',true); end if;
  update public.translation_requests set status='failed',failed_at=now(),error_code=left(coalesce(p_error_code,'provider_failed'),80) where id=q.id;
  if p_release_reservation and q.reservation_id is not null then update public.yk_reservations set status='released',updated_at=now() where id=q.reservation_id and status='reserved'; end if;
  return jsonb_build_object('request_id',q.request_id,'status','failed','idempotent',false);
end $$;
revoke all on function public.commit_translation_success(text,integer,text,text), public.record_translation_failure(text,text,boolean) from public,anon,authenticated;
grant execute on function public.commit_translation_success(text,integer,text,text), public.record_translation_failure(text,text,boolean) to service_role,postgres;
