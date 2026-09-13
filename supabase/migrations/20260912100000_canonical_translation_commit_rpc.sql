-- Canonical server-side translation commit contract.
-- Remove the legacy four-argument overload and make all five arguments explicit
-- so PostgREST cannot resolve an incomplete payload ambiguously.
drop function if exists public.commit_translation_success(text, integer, text, text);

create or replace function public.commit_translation_success(
  p_request_id text,
  p_billed_characters integer,
  p_detected_source_language text,
  p_model_type_used text,
  p_result jsonb
) returns jsonb language plpgsql security definer
set search_path = pg_catalog, public as $$
declare q public.translation_requests%rowtype; r public.yk_reservations%rowtype;
begin
  select * into q from public.translation_requests where request_id=p_request_id for update;
  if q.id is null then raise exception 'translation_not_found'; end if;
  if q.status='completed' then return jsonb_build_object('request_id',q.request_id,'status','completed','idempotent',true,'result',q.result); end if;
  if q.status <> 'processing' then raise exception 'translation_not_processing'; end if;
  if lower(trim(q.provider)) <> 'deepl' then raise exception 'translation_provider_mismatch'; end if;
  if q.result is null and p_result is null then raise exception 'translation_result_missing'; end if;
  select * into r from public.yk_reservations where id=q.reservation_id and user_id=q.user_id for update;
  if r.id is null then raise exception 'reservation_not_found'; end if;
  if r.status <> 'reserved' then raise exception 'reservation_not_reserved'; end if;
  if r.subscription_amount>0 then insert into public.yk_ledger(user_id,amount,bucket,event_type,reference_id) values(q.user_id,-r.subscription_amount,'subscription','translation_debit',q.job_id); end if;
  if r.permanent_amount>0 then insert into public.yk_ledger(user_id,amount,bucket,event_type,reference_id) values(q.user_id,-r.permanent_amount,'permanent','translation_debit',q.job_id); end if;
  update public.yk_reservations set status='consumed',updated_at=now() where id=r.id and status='reserved';
  insert into public.progression_ledger(user_id,amount,event_type,reference_id) values(q.user_id,10,'translation',q.request_id) on conflict do nothing;
  update public.translation_requests set status='completed',completed_at=now(),billed_characters=greatest(coalesce(p_billed_characters,0),0),detected_source_language=left(nullif(trim(p_detected_source_language),''),32),model_type_used=left(nullif(trim(p_model_type_used),''),64),result=coalesce(p_result,q.result) where id=q.id and status='processing';
  return jsonb_build_object('request_id',q.request_id,'status','completed','idempotent',false,'xp_credited',10,'result',coalesce(p_result,q.result));
end $$;

revoke all on function public.commit_translation_success(text,integer,text,text,jsonb) from public,anon,authenticated;
grant execute on function public.commit_translation_success(text,integer,text,text,jsonb) to service_role,postgres;
