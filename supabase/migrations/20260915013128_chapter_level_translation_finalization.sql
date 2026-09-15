-- Chapter-level translation accounting.
-- A logical chapter reserves and consumes exactly one YK, even when the
-- provider transport splits it into several translation requests.

create or replace function public.commit_translation_batch_success(
  p_request_id text,
  p_billed_characters integer,
  p_detected_source_language text,
  p_model_type_used text,
  p_result jsonb
) returns jsonb
language plpgsql security definer
set search_path = pg_catalog, public
as $$
declare q public.translation_requests%rowtype; v_result jsonb;
begin
  select * into q from public.translation_requests
   where request_id = trim(p_request_id) for update;
  if q.id is null then raise exception 'translation_not_found'; end if;
  if q.status = 'completed' then
    return jsonb_build_object('request_id',q.request_id,'status','completed','idempotent',true,'result',q.result);
  end if;
  if q.status <> 'processing' then raise exception 'translation_not_processing'; end if;
  if q.provider <> 'deepl' then raise exception 'translation_provider_mismatch'; end if;
  if q.reservation_id is null then raise exception 'reservation_not_reserved'; end if;
  v_result := coalesce(p_result, q.result);
  if v_result is null then raise exception 'translation_result_missing'; end if;
  update public.translation_requests
     set status='completed', completed_at=now(),
         billed_characters=greatest(coalesce(p_billed_characters,0),0),
         detected_source_language=left(nullif(trim(p_detected_source_language),''),32),
         model_type_used=left(nullif(trim(p_model_type_used),''),64),
         result=v_result
   where id=q.id;
  return jsonb_build_object('request_id',q.request_id,'status','completed','idempotent',false,'result',v_result);
end $$;

create or replace function public.finalize_translation_job(
  p_job_id text,
  p_reservation_id uuid
) returns jsonb
language plpgsql security definer
set search_path = pg_catalog, public
as $$
declare
  q public.translation_requests%rowtype;
  r public.yk_reservations%rowtype;
  total_requests integer := 0;
  completed_requests integer := 0;
  v_user uuid;
  progression_inserted integer := 0;
begin
  if nullif(trim(p_job_id),'') is null or p_reservation_id is null then
    raise exception 'invalid_translation_job';
  end if;

  -- Lock every request for this logical chapter before checking completeness.
  for q in select * from public.translation_requests
            where job_id=trim(p_job_id) order by request_id for update loop
    total_requests := total_requests + 1;
    if q.status = 'completed' then completed_requests := completed_requests + 1; end if;
    if q.reservation_id is distinct from p_reservation_id then
      raise exception 'reservation_job_mismatch';
    end if;
    if q.provider <> 'deepl' then raise exception 'translation_provider_mismatch'; end if;
    v_user := q.user_id;
  end loop;
  if total_requests = 0 then raise exception 'translation_job_not_found'; end if;
  if completed_requests <> total_requests then raise exception 'translation_job_not_complete'; end if;

  select * into r from public.yk_reservations
   where id=p_reservation_id and job_id=trim(p_job_id) and user_id=v_user for update;
  if r.id is null then raise exception 'reservation_not_found'; end if;
  if r.status = 'consumed' then
    return jsonb_build_object('job_id',trim(p_job_id),'reservation_id',r.id,'status','consumed','idempotent',true,'yk_debited',0,'xp_credited',0);
  end if;
  if r.status <> 'reserved' then raise exception 'reservation_not_reserved'; end if;

  update public.yk_reservations set status='consumed',updated_at=now() where id=r.id;
  if r.daily_amount > 0 then
    insert into public.yk_ledger(user_id,amount,bucket,event_type,reference_id)
      values(v_user,-r.daily_amount,'daily','translation_debit',trim(p_job_id));
  end if;
  if r.subscription_amount > 0 then
    insert into public.yk_ledger(user_id,amount,bucket,event_type,reference_id)
      values(v_user,-r.subscription_amount,'subscription','translation_debit',trim(p_job_id));
  end if;
  if r.permanent_amount > 0 then
    insert into public.yk_ledger(user_id,amount,bucket,event_type,reference_id)
      values(v_user,-r.permanent_amount,'permanent','translation_debit',trim(p_job_id));
  end if;
  insert into public.progression_ledger(user_id,amount,event_type,reference_id)
    values(v_user,10,'translation',trim(p_job_id)) on conflict do nothing;
  get diagnostics progression_inserted = row_count;
  return jsonb_build_object('job_id',trim(p_job_id),'reservation_id',r.id,'status','consumed',
    'idempotent',false,'yk_debited',r.amount,'xp_credited',case when progression_inserted=1 then 10 else 0 end);
end $$;

revoke all on function public.commit_translation_batch_success(text,integer,text,text,jsonb) from public,anon,authenticated;
revoke all on function public.finalize_translation_job(text,uuid) from public,anon,authenticated;
grant execute on function public.commit_translation_batch_success(text,integer,text,text,jsonb) to service_role,postgres;
grant execute on function public.finalize_translation_job(text,uuid) to service_role,postgres;
