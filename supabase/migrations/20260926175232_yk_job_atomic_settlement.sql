-- Local source migration only. Do not apply remotely without separate approval.
-- Wallet transition is bound to the authenticated owner, exact job/reservation,
-- server-derived reservation amounts, and an idempotency key.
alter table public.yk_reservations
  add column if not exists settlement_action text,
  add column if not exists settlement_idempotency_key text,
  add column if not exists settled_at timestamptz;

create unique index if not exists yk_reservations_user_settlement_key_uq
  on public.yk_reservations(user_id, settlement_idempotency_key)
  where settlement_idempotency_key is not null;

create or replace function public.settle_translation_job(
  p_job_id text,
  p_reservation_id uuid,
  p_action text,
  p_idempotency_key text
) returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_user uuid := auth.uid();
  r public.yk_reservations%rowtype;
  q record;
  v_requests integer := 0;
  v_processing integer := 0;
  v_completed integer := 0;
  v_result_persisted integer := 0;
  v_expected_status text;
  v_result jsonb;
begin
  if v_user is null then raise exception 'unauthorized'; end if;
  if nullif(trim(p_job_id), '') is null or p_reservation_id is null
     or p_action is null or p_action not in ('consume', 'release')
     or p_idempotency_key is null
     or p_idempotency_key !~ '^[A-Za-z0-9][A-Za-z0-9:_.-]{15,199}$' then
    raise exception 'invalid_settlement_request';
  end if;

  perform pg_advisory_xact_lock(hashtextextended(v_user::text, 0));
  if not exists (select 1 from public.yk_reservations
                  where id = p_reservation_id and job_id = trim(p_job_id)
                    and user_id = v_user) then
    raise exception 'reservation_not_found';
  end if;
  -- Match the existing begin/finalize lock order (request rows, then reservation)
  -- to avoid a settlement-vs-provider deadlock.
  for q in
    select user_id, reservation_id, status, provider, result
      from public.translation_requests
     where job_id = trim(p_job_id)
     for update
  loop
    v_requests := v_requests + 1;
    if q.user_id <> v_user or q.reservation_id is distinct from p_reservation_id then
      raise exception 'reservation_job_mismatch';
    end if;
    if q.status = 'processing' then v_processing := v_processing + 1; end if;
    if q.status = 'completed' then v_completed := v_completed + 1; end if;
    if q.result is not null then v_result_persisted := v_result_persisted + 1; end if;
    if p_action = 'consume' and (q.status <> 'completed' or q.provider <> 'deepl') then
      raise exception 'translation_job_not_complete';
    end if;
  end loop;

  select * into r from public.yk_reservations
   where id = p_reservation_id and job_id = trim(p_job_id) and user_id = v_user
   for update;
  if r.id is null then raise exception 'reservation_not_found'; end if;

  if r.status in ('consumed', 'released') then
    v_expected_status := case when p_action = 'consume' then 'consumed' else 'released' end;
    if r.status <> v_expected_status then raise exception 'reservation_terminal_conflict'; end if;
    return jsonb_build_object('job_id', trim(p_job_id), 'reservation_id', r.id,
      'status', r.status, 'idempotent', true, 'yk_debited', 0, 'xp_credited', 0);
  end if;
  if r.status <> 'reserved' then raise exception 'reservation_state_invalid'; end if;
  if exists (select 1 from public.yk_reservations
              where user_id = v_user and settlement_idempotency_key = p_idempotency_key
                and id <> r.id) then
    raise exception 'idempotency_key_conflict';
  end if;

  if p_action = 'consume' then
    if v_requests = 0 then raise exception 'translation_job_not_found'; end if;
    if v_result_persisted <> v_requests then raise exception 'translation_result_not_persisted'; end if;
    -- Reuse the canonical server-side ledger/XP transaction. This function is
    -- transactionally nested; any failure rolls back the settlement metadata too.
    v_result := public.finalize_translation_job(trim(p_job_id), r.id);
    update public.yk_reservations
       set settlement_action = 'consume',
           settlement_idempotency_key = p_idempotency_key,
           settled_at = now(), updated_at = now()
     where id = r.id;
    return v_result || jsonb_build_object('idempotent', false);
  end if;

  -- A client may only abandon before any provider output exists. Even one
  -- completed request/result makes the reservation non-refundable: partial
  -- provider success is recoverable work, not grounds for a client refund.
  if v_processing > 0 then raise exception 'translation_request_in_flight'; end if;
  if v_completed > 0 or v_result_persisted > 0 then
    raise exception 'provider_success_cannot_be_released';
  end if;
  update public.yk_reservations
     set status = 'released', settlement_action = 'release',
         settlement_idempotency_key = p_idempotency_key,
         settled_at = now(), updated_at = now()
   where id = r.id;
  return jsonb_build_object('job_id', trim(p_job_id), 'reservation_id', r.id,
    'status', 'released', 'idempotent', false, 'yk_debited', 0, 'xp_credited', 0);
end;
$$;

revoke all on function public.settle_translation_job(text, uuid, text, text)
  from public, anon;
grant execute on function public.settle_translation_job(text, uuid, text, text)
  to authenticated;
