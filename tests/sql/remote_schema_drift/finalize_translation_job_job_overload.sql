-- Out-of-history remote schema snapshot for tests only.
-- This is NOT a deployable migration. Captured from pg_get_functiondef on
-- project mimrsxnhqbqkffsekxuw, 2026-09-27; keep the active migration history separate.
CREATE OR REPLACE FUNCTION public.finalize_translation_job(p_job_id text, p_reservation_id uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
AS $function$
declare
  q public.translation_requests%rowtype;
  r public.yk_reservations%rowtype;
  total_requests integer := 0;
  completed_requests integer := 0;
  v_user uuid;
  progression_inserted integer := 0;
begin
  if nullif(trim(p_job_id), '') is null or p_reservation_id is null then
    raise exception 'invalid_translation_job';
  end if;

  for q in select * from public.translation_requests
            where job_id = trim(p_job_id) order by request_id for update loop
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
   where id = p_reservation_id and job_id = trim(p_job_id) and user_id = v_user for update;
  if r.id is null then raise exception 'reservation_not_found'; end if;
  if r.status = 'consumed' then
    return jsonb_build_object('job_id', trim(p_job_id), 'reservation_id', r.id,
      'status', 'consumed', 'idempotent', true, 'yk_debited', 0, 'xp_credited', 0);
  end if;
  if r.status <> 'reserved' then raise exception 'reservation_not_reserved'; end if;

  update public.yk_reservations set status = 'consumed', updated_at = now() where id = r.id;

  -- The daily debit inherits the expiry captured at reservation time so it
  -- leaves the active balance together with the credit that funded it.
  if r.daily_amount > 0 then
    insert into public.yk_ledger(user_id, amount, bucket, event_type, reference_id, expires_at)
      values (v_user, -r.daily_amount, 'daily', 'translation_debit', trim(p_job_id), r.daily_expires_at);
  end if;
  if r.subscription_amount > 0 then
    insert into public.yk_ledger(user_id, amount, bucket, event_type, reference_id)
      values (v_user, -r.subscription_amount, 'subscription', 'translation_debit', trim(p_job_id));
  end if;
  if r.permanent_amount > 0 then
    insert into public.yk_ledger(user_id, amount, bucket, event_type, reference_id)
      values (v_user, -r.permanent_amount, 'permanent', 'translation_debit', trim(p_job_id));
  end if;

  insert into public.progression_ledger(user_id, amount, event_type, reference_id)
    values (v_user, 10, 'translation', trim(p_job_id)) on conflict do nothing;
  get diagnostics progression_inserted = row_count;

  return jsonb_build_object('job_id', trim(p_job_id), 'reservation_id', r.id,
    'status', 'consumed', 'idempotent', false, 'yk_debited', r.amount,
    'xp_credited', case when progression_inserted = 1 then 10 else 0 end);
end $function$;
REVOKE ALL ON FUNCTION public.finalize_translation_job(text, uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.finalize_translation_job(text, uuid) TO service_role, postgres;
