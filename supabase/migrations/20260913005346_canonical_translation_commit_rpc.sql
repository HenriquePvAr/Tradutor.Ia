drop function if exists public.commit_translation_success(text, integer, text, text);
drop function if exists public.commit_translation_success(text, integer, text, text, jsonb);

create function public.commit_translation_success(
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

  v_result := coalesce(p_result, q.result);
  if v_result is null then
    raise exception 'translation_result_missing';
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
  values(q.user_id, 10, 'translation', q.request_id)
  on conflict do nothing;

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
    'xp_credited', 10,
    'result', v_result
  );
end
$function$;
