create or replace function public.commit_translation_batch_success(
  p_request_id text,
  p_billed_characters integer,
  p_detected_source_language text,
  p_model_type_used text,
  p_result jsonb
)
returns jsonb
language plpgsql
security definer
set search_path to 'pg_catalog', 'public'
as $function$
declare
  q public.translation_requests%rowtype;
  r public.yk_reservations%rowtype;
  v_result jsonb;
begin
  select * into q
  from public.translation_requests
  where request_id = trim(p_request_id)
  for update;

  if q.id is null then
    raise exception 'translation_not_found';
  end if;

  if q.status = 'completed' then
    return jsonb_build_object(
      'request_id', q.request_id,
      'status', 'completed',
      'idempotent', true,
      'result', q.result
    );
  end if;

  if q.status <> 'processing' then
    raise exception 'translation_not_processing';
  end if;

  if q.provider <> 'deepl' then
    raise exception 'translation_provider_mismatch';
  end if;

  select * into r
  from public.yk_reservations
  where id = q.reservation_id
    and user_id = q.user_id
  for update;

  if r.id is null or r.status <> 'reserved' then
    raise exception 'reservation_not_reserved';
  end if;

  if q.job_id is null or r.job_id is distinct from q.job_id then
    raise exception 'reservation_job_mismatch';
  end if;

  v_result := coalesce(p_result, q.result);
  if v_result is null then
    raise exception 'translation_result_missing';
  end if;

  update public.translation_requests
  set status = 'completed',
      completed_at = now(),
      billed_characters = greatest(coalesce(p_billed_characters, 0), 0),
      detected_source_language = left(nullif(trim(p_detected_source_language), ''), 32),
      model_type_used = left(nullif(trim(p_model_type_used), ''), 64),
      result = v_result
  where id = q.id;

  return jsonb_build_object(
    'request_id', q.request_id,
    'status', 'completed',
    'idempotent', false,
    'job_id', q.job_id,
    'reservation_id', q.reservation_id,
    'result', v_result
  );
end
$function$;

create or replace function public.finalize_translation_job(
  p_request_id text
)
returns jsonb
language plpgsql
security definer
set search_path to 'pg_catalog', 'public'
as $function$
declare
  q public.translation_requests%rowtype;
  r public.yk_reservations%rowtype;
  v_xp_inserted integer := 0;
begin
  select * into q
  from public.translation_requests
  where request_id = trim(p_request_id)
  for update;

  if q.id is null then
    raise exception 'translation_not_found';
  end if;

  if q.status <> 'completed' then
    raise exception 'translation_not_completed';
  end if;

  if q.job_id is null or q.reservation_id is null then
    raise exception 'translation_job_missing';
  end if;

  select * into r
  from public.yk_reservations
  where id = q.reservation_id
    and user_id = q.user_id
  for update;

  if r.id is null then
    raise exception 'reservation_not_found';
  end if;

  if r.job_id is distinct from q.job_id then
    raise exception 'reservation_job_mismatch';
  end if;

  if r.status = 'consumed' then
    return jsonb_build_object(
      'request_id', q.request_id,
      'job_id', q.job_id,
      'reservation_id', r.id,
      'status', 'completed',
      'reservation_status', 'consumed',
      'idempotent', true,
      'xp_credited', 0
    );
  end if;

  if r.status <> 'reserved' then
    raise exception 'reservation_not_reserved';
  end if;

  update public.yk_reservations
  set status = 'consumed', updated_at = now()
  where id = r.id;

  if r.daily_amount > 0 then
    insert into public.yk_ledger(user_id, amount, bucket, event_type, reference_id)
    values(q.user_id, -r.daily_amount, 'daily', 'translation_debit', q.job_id);
  end if;

  if r.subscription_amount > 0 then
    insert into public.yk_ledger(user_id, amount, bucket, event_type, reference_id)
    values(q.user_id, -r.subscription_amount, 'subscription', 'translation_debit', q.job_id);
  end if;

  if r.permanent_amount > 0 then
    insert into public.yk_ledger(user_id, amount, bucket, event_type, reference_id)
    values(q.user_id, -r.permanent_amount, 'permanent', 'translation_debit', q.job_id);
  end if;

  insert into public.progression_ledger(user_id, amount, event_type, reference_id)
  values(q.user_id, 10, 'translation', q.job_id)
  on conflict (user_id, event_type, reference_id) do nothing;
  get diagnostics v_xp_inserted = row_count;

  return jsonb_build_object(
    'request_id', q.request_id,
    'job_id', q.job_id,
    'reservation_id', r.id,
    'status', 'completed',
    'reservation_status', 'consumed',
    'idempotent', false,
    'xp_credited', case when v_xp_inserted > 0 then 10 else 0 end
  );
end
$function$;

create or replace function public.commit_translation_success(
  p_request_id text,
  p_billed_characters integer,
  p_detected_source_language text,
  p_model_type_used text,
  p_result jsonb
)
returns jsonb
language plpgsql
security definer
set search_path to 'pg_catalog', 'public'
as $function$
declare
  v_batch jsonb;
  v_final jsonb;
begin
  v_batch := public.commit_translation_batch_success(
    p_request_id,
    p_billed_characters,
    p_detected_source_language,
    p_model_type_used,
    p_result
  );

  v_final := public.finalize_translation_job(p_request_id);

  return v_batch || jsonb_build_object(
    'job_finalized', true,
    'finalization', v_final
  );
end
$function$;

revoke all on function public.commit_translation_batch_success(text, integer, text, text, jsonb) from public, anon, authenticated;
revoke all on function public.finalize_translation_job(text) from public, anon, authenticated;
grant execute on function public.commit_translation_batch_success(text, integer, text, text, jsonb) to service_role;
grant execute on function public.finalize_translation_job(text) to service_role;

comment on function public.commit_translation_batch_success(text, integer, text, text, jsonb)
is 'Commits one translation provider batch without consuming the chapter-level YK reservation.';
comment on function public.finalize_translation_job(text)
is 'Idempotently consumes the chapter-level YK reservation and credits one progression event for the job.';
