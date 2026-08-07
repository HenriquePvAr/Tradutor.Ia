-- Legacy publication provenance — storage upload reservation/lease (additive follow-up to
-- Fase 2A1/2A2 and the Drive storage adapter's bounded create-deadline contract).
--
-- Closes the cross-process race documented in
-- test_concurrent_narrow_lookup_race_can_duplicate_without_external_lock
-- (test_legacy_publication_drive_adapter.py): two workers can each independently observe zero
-- matches from Drive's own idempotency lookup before either create is visible to the other,
-- because nothing previously held a lock across that network round trip. This migration adds a
-- persistent, cross-process reservation/lease so only one worker at a time is *permitted* to
-- attempt an external create for a given (storage_provider, storage_scope_key, idempotency_key)
-- identity. It does not and cannot cancel an HTTP request already in flight to the storage
-- provider — see the fencing/TTL comments below and .runtime/legacy-storage-reservation-design/
-- external_side_effect_race.md in the sibling Drive-adapter worktree for the honest limitation.
--
-- No existing table is altered. private.legacy_publication_migrations,
-- private.legacy_migration_sources, and private.legacy_publication_migration_events (Fase 2A1)
-- and the migration_operator/migration_publication_admin roles plus the eight Fase 2A2 functions
-- (supabase/migrations/20260806130000_legacy_publication_migration_runtime.sql) are untouched.
--
-- Every SECURITY DEFINER function below follows the exact same discipline as the existing 2A2
-- functions: set search_path = '' with fully schema-qualified references, EXECUTE revoked from
-- PUBLIC, EXECUTE granted only to migration_operator (reused, no new role — role_security.md:
-- this reservation surface is pure automated-pipeline plumbing with no differentiated trust
-- boundary from the functions migration_operator already runs), categorical
-- RAISE EXCEPTION ... USING ERRCODE, no dynamic SQL, no SELECT *, fencing (lease_generation +
-- lease_holder_token) validated under `select ... for update` before any write.

begin;

-- ===========================================================================
-- 1. private.legacy_storage_upload_reservations
-- ===========================================================================
create table if not exists private.legacy_storage_upload_reservations (
    id uuid primary key default gen_random_uuid(),

    migration_id uuid not null
        references private.legacy_publication_migrations (id) on delete restrict,
        -- RESTRICT, not CASCADE: a reservation is an audit-relevant record of an external
        -- side-effect attempt; a migration row must never be deletable while a reservation still
        -- references it. Matches the existing table's own FK style (events -> migrations).

    -- Canonical identity (immutable after insert) — migration_id above is reference/audit only
    -- and is deliberately NOT part of the UNIQUE tuple.
    storage_provider text not null,
    storage_scope_key text not null,
    storage_scope_raw text,
    idempotency_key text not null,

    -- Integrity bound at claim time; never overwritten after insert (only actual_* records the
    -- provider-reported outcome for comparison).
    expected_sha256 text not null,
    expected_size bigint not null,

    -- Lease / fencing.
    lease_holder_token uuid not null default gen_random_uuid(),
    lease_generation bigint not null default 1,
    lease_expires_at timestamptz not null,
    acquired_at timestamptz not null default now(),
    attempt_count int not null default 0,

    -- State machine.
    reservation_state text not null default 'reserved',
    external_create_started_at timestamptz,

    -- Outcome, once known.
    storage_file_id text,
    actual_sha256 text,
    actual_size bigint,
    last_error_code text,

    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    completed_at timestamptz,

    constraint legacy_storage_upload_reservations_identity_unique
        unique (storage_provider, storage_scope_key, idempotency_key),

    constraint legacy_storage_upload_reservations_state_check
        check (reservation_state in (
            'reserved', 'create_in_flight', 'completed', 'failed_retryable', 'failed_terminal'
        )),
    constraint legacy_storage_upload_reservations_sha_hex
        check (expected_sha256 ~ '^[0-9a-f]{64}$'
               and (actual_sha256 is null or actual_sha256 ~ '^[0-9a-f]{64}$')),
    constraint legacy_storage_upload_reservations_size_positive
        check (expected_size > 0 and (actual_size is null or actual_size > 0)),
    constraint legacy_storage_upload_reservations_completed_requires_file_id
        check (reservation_state <> 'completed' or (storage_file_id is not null and completed_at is not null)),
    constraint legacy_storage_upload_reservations_create_in_flight_requires_started_at
        check (reservation_state not in ('create_in_flight', 'completed') or external_create_started_at is not null),
    constraint legacy_storage_upload_reservations_attempt_count_non_negative
        check (attempt_count >= 0)
);

comment on table private.legacy_storage_upload_reservations is
    'Persistent cross-process reservation/lease for one external storage-provider create attempt '
    'per (storage_provider, storage_scope_key, idempotency_key). Closes the storage_concurrency_gap '
    'documented against the Drive adapter (two workers each observing zero matches before either '
    'create is visible). Never exposed to anon/authenticated; zero client policies.';
comment on column private.legacy_storage_upload_reservations.migration_id is
    'Audit/reference only. Never part of the UNIQUE identity tuple — the same migration could in '
    'principle spawn more than one reservation attempt across retries with a changed artifact.';
comment on column private.legacy_storage_upload_reservations.storage_scope_key is
    'Provider-agnostic fingerprint of the upload destination (e.g. sha256(provider || folder_id) '
    'for Drive). The raw, human-diagnosable value lives in storage_scope_raw, which is NOT part of '
    'the UNIQUE tuple — same pattern as legacy_migration_sources.initial_snapshot_sha256.';
comment on column private.legacy_storage_upload_reservations.lease_generation is
    'Monotonic per identity, incremented on every takeover. Never reused, so a stale holder can '
    'never accidentally match a later generation by chance (fencing_model).';
comment on column private.legacy_storage_upload_reservations.lease_expires_at is
    'Server-side only; the TTL constants (RESERVE_PHASE=300s, CREATE_PHASE=900s) are fixed inside '
    'the functions below, never a client-supplied parameter.';

create index legacy_storage_upload_reservations_migration_idx
    on private.legacy_storage_upload_reservations (migration_id);
create index legacy_storage_upload_reservations_state_expiry_idx
    on private.legacy_storage_upload_reservations (reservation_state, lease_expires_at)
    where reservation_state in ('reserved', 'create_in_flight');

alter table private.legacy_storage_upload_reservations enable row level security;
revoke all on private.legacy_storage_upload_reservations from public, anon, authenticated;

-- ===========================================================================
-- 2. claim_legacy_storage_upload(...)
--    returns acquired | busy | recovery_required | already_completed | conflict
-- ===========================================================================
create or replace function private.claim_legacy_storage_upload(
    p_migration_id uuid,
    p_storage_provider text,
    p_storage_scope_key text,
    p_idempotency_key text,
    p_expected_sha256 text,
    p_expected_size bigint,
    p_storage_scope_raw text default null
)
returns table(
    reservation_id uuid,
    lease_holder_token uuid,
    lease_generation bigint,
    lease_expires_at timestamptz,
    status text,
    storage_file_id text,
    reservation_state text
)
language plpgsql
security definer
set search_path = ''
as $$
#variable_conflict use_column
declare
    v_row private.legacy_storage_upload_reservations%rowtype;
    v_new_id uuid;
    v_token uuid;
    v_expires timestamptz;
begin
    if p_migration_id is null then
        raise exception 'migration_id is required' using errcode = 'not_null_violation';
    end if;
    if p_storage_provider is null or length(trim(p_storage_provider)) = 0 then
        raise exception 'storage_provider is required' using errcode = 'not_null_violation';
    end if;
    if p_storage_scope_key is null or length(trim(p_storage_scope_key)) = 0 then
        raise exception 'storage_scope_key is required' using errcode = 'not_null_violation';
    end if;
    if p_idempotency_key is null or length(trim(p_idempotency_key)) = 0 then
        raise exception 'idempotency_key is required' using errcode = 'not_null_violation';
    end if;
    if p_expected_sha256 is null or p_expected_sha256 !~ '^[0-9a-f]{64}$' then
        raise exception 'expected_sha256 must be 64 lowercase hex characters' using errcode = 'invalid_parameter_value';
    end if;
    if p_expected_size is null or p_expected_size <= 0 then
        raise exception 'expected_size must be positive' using errcode = 'invalid_parameter_value';
    end if;

    v_token := gen_random_uuid();
    v_expires := now() + interval '300 seconds';

    insert into private.legacy_storage_upload_reservations (
        migration_id, storage_provider, storage_scope_key, storage_scope_raw, idempotency_key,
        expected_sha256, expected_size, lease_holder_token, lease_generation, lease_expires_at,
        acquired_at, attempt_count, reservation_state
    ) values (
        p_migration_id, p_storage_provider, p_storage_scope_key, p_storage_scope_raw, p_idempotency_key,
        p_expected_sha256, p_expected_size, v_token, 1, v_expires, now(), 1, 'reserved'
    )
    on conflict (storage_provider, storage_scope_key, idempotency_key) do nothing
    returning id into v_new_id;

    if v_new_id is not null then
        return query select v_new_id, v_token, 1::bigint, v_expires, 'acquired'::text, null::text, 'reserved'::text;
        return;
    end if;

    -- Someone else won the insert race (or this identity was already claimed earlier): lock and
    -- classify the existing row. This `for update` is what turns the check-then-insert race that
    -- plagued the old Drive-only path into a serialized decision.
    select * into v_row
    from private.legacy_storage_upload_reservations
    where storage_provider = p_storage_provider
      and storage_scope_key = p_storage_scope_key
      and idempotency_key = p_idempotency_key
    for update;

    if v_row.expected_sha256 <> p_expected_sha256 or v_row.expected_size <> p_expected_size then
        -- Same logical upload identity, different bytes -- never silently reused.
        return query select v_row.id, null::uuid, v_row.lease_generation, v_row.lease_expires_at, 'conflict'::text, v_row.storage_file_id, v_row.reservation_state;
        return;
    end if;

    if v_row.reservation_state = 'completed' then
        return query select v_row.id, null::uuid, v_row.lease_generation, v_row.lease_expires_at, 'already_completed'::text, v_row.storage_file_id, v_row.reservation_state;
        return;
    end if;

    if v_row.reservation_state = 'failed_terminal' then
        -- No further attempts without a genuinely new idempotency key (new plan/artifact).
        return query select v_row.id, null::uuid, v_row.lease_generation, v_row.lease_expires_at, 'conflict'::text, v_row.storage_file_id, v_row.reservation_state;
        return;
    end if;

    if v_row.reservation_state = 'failed_retryable' then
        v_token := gen_random_uuid();
        v_expires := now() + interval '300 seconds';
        update private.legacy_storage_upload_reservations
           set lease_holder_token = v_token,
               lease_generation = lease_generation + 1,
               lease_expires_at = v_expires,
               acquired_at = now(),
               attempt_count = attempt_count + 1,
               reservation_state = 'reserved',
               external_create_started_at = null,
               last_error_code = null,
               updated_at = now()
         where id = v_row.id
         returning * into v_row;
        return query select v_row.id, v_token, v_row.lease_generation, v_row.lease_expires_at, 'acquired'::text, null::text, v_row.reservation_state;
        return;
    end if;

    -- reservation_state in ('reserved', 'create_in_flight') -- either a live lease (busy) or an
    -- expired one (takeover_semantics.md).
    if v_row.lease_expires_at < now() then
        v_token := gen_random_uuid();
        v_expires := now() + (case when v_row.reservation_state = 'create_in_flight' then interval '900 seconds' else interval '300 seconds' end);
        update private.legacy_storage_upload_reservations
           set lease_holder_token = v_token,
               lease_generation = lease_generation + 1,
               lease_expires_at = v_expires,
               acquired_at = now(),
               attempt_count = attempt_count + 1,
               updated_at = now()
         where id = v_row.id
         returning * into v_row;
        if v_row.reservation_state = 'create_in_flight' then
            -- window C/D/E/F/G (crash_windows.md): outcome unknown, mandatory narrow lookup
            -- before the caller may attempt another create.
            return query select v_row.id, v_token, v_row.lease_generation, v_row.lease_expires_at, 'recovery_required'::text, null::text, v_row.reservation_state;
        else
            -- window B: provably before_create, no lookup required (harmless if done anyway).
            return query select v_row.id, v_token, v_row.lease_generation, v_row.lease_expires_at, 'acquired'::text, null::text, v_row.reservation_state;
        end if;
        return;
    end if;

    -- Live lease held by someone else. Never blocks/waits -- caller's own retry/backoff decides.
    return query select v_row.id, null::uuid, v_row.lease_generation, v_row.lease_expires_at, 'busy'::text, null::text, v_row.reservation_state;
end;
$$;

comment on function private.claim_legacy_storage_upload(uuid, text, text, text, text, bigint, text) is
    'Reserve-or-classify a storage upload attempt. insert ... on conflict do nothing plus a row '
    'lock on the loser''s path serializes the exact check-then-insert race that let two workers '
    'each observe zero Drive matches before either create landed. Never blocks/waits on a live lease.';

revoke execute on function private.claim_legacy_storage_upload(uuid, text, text, text, text, bigint, text) from public;
grant execute on function private.claim_legacy_storage_upload(uuid, text, text, text, text, bigint, text) to migration_operator;

-- ===========================================================================
-- 3. mark_storage_create_started(...)
--    returns started | already_in_flight_same_lease | already_completed | stale_lease
-- ===========================================================================
create or replace function private.mark_storage_create_started(
    p_reservation_id uuid,
    p_lease_holder_token uuid,
    p_lease_generation bigint
)
returns table(status text, lease_expires_at timestamptz)
language plpgsql
security definer
set search_path = ''
as $$
#variable_conflict use_column
declare
    v_row private.legacy_storage_upload_reservations%rowtype;
    v_expires timestamptz;
begin
    if p_reservation_id is null or p_lease_holder_token is null or p_lease_generation is null then
        raise exception 'reservation_id, lease_holder_token and lease_generation are required' using errcode = 'not_null_violation';
    end if;

    select * into v_row from private.legacy_storage_upload_reservations where id = p_reservation_id for update;
    if not found then
        raise exception 'reservation % not found', p_reservation_id using errcode = 'no_data_found';
    end if;

    if v_row.reservation_state = 'completed' then
        return query select 'already_completed'::text, v_row.lease_expires_at;
        return;
    end if;

    -- Fencing: generation is authoritative, token is defense-in-depth (fencing_model.md).
    if v_row.lease_generation <> p_lease_generation or v_row.lease_holder_token <> p_lease_holder_token then
        return query select 'stale_lease'::text, v_row.lease_expires_at;
        return;
    end if;

    if v_row.reservation_state = 'create_in_flight' then
        -- Idempotent in-process retry under the same still-valid lease.
        return query select 'already_in_flight_same_lease'::text, v_row.lease_expires_at;
        return;
    end if;

    if v_row.reservation_state <> 'reserved' then
        -- Fencing matched but the state machine doesn't allow this transition (e.g. failed_*):
        -- fail closed rather than silently accept.
        return query select 'stale_lease'::text, v_row.lease_expires_at;
        return;
    end if;

    v_expires := now() + interval '900 seconds';
    update private.legacy_storage_upload_reservations
       set reservation_state = 'create_in_flight',
           external_create_started_at = now(),
           lease_expires_at = v_expires,
           updated_at = now()
     where id = p_reservation_id;

    return query select 'started'::text, v_expires;
end;
$$;

comment on function private.mark_storage_create_started(uuid, uuid, bigint) is
    'Commits create_in_flight BEFORE the caller issues the external create/upload request. This '
    'is what closes the window between "committed intent" and "external attempt": any takeover '
    'that observes create_in_flight is forced through the mandatory recovery lookup.';

revoke execute on function private.mark_storage_create_started(uuid, uuid, bigint) from public;
grant execute on function private.mark_storage_create_started(uuid, uuid, bigint) to migration_operator;

-- ===========================================================================
-- 4. complete_legacy_storage_upload(...)
--    returns completed | already_completed | stale_lease | integrity_mismatch
-- ===========================================================================
create or replace function private.complete_legacy_storage_upload(
    p_reservation_id uuid,
    p_lease_holder_token uuid,
    p_lease_generation bigint,
    p_storage_file_id text,
    p_actual_sha256 text,
    p_actual_size bigint
)
returns table(status text, storage_file_id text)
language plpgsql
security definer
set search_path = ''
as $$
#variable_conflict use_column
declare
    v_row private.legacy_storage_upload_reservations%rowtype;
begin
    if p_reservation_id is null or p_lease_holder_token is null or p_lease_generation is null then
        raise exception 'reservation_id, lease_holder_token and lease_generation are required' using errcode = 'not_null_violation';
    end if;
    if p_storage_file_id is null or length(trim(p_storage_file_id)) = 0 then
        raise exception 'storage_file_id is required' using errcode = 'not_null_violation';
    end if;
    if p_actual_sha256 is null or p_actual_sha256 !~ '^[0-9a-f]{64}$' then
        raise exception 'actual_sha256 must be 64 lowercase hex characters' using errcode = 'invalid_parameter_value';
    end if;
    if p_actual_size is null or p_actual_size <= 0 then
        raise exception 'actual_size must be positive' using errcode = 'invalid_parameter_value';
    end if;

    select * into v_row from private.legacy_storage_upload_reservations where id = p_reservation_id for update;
    if not found then
        raise exception 'reservation % not found', p_reservation_id using errcode = 'no_data_found';
    end if;

    if v_row.reservation_state = 'completed' then
        -- Idempotent replay (response_lost_db.md). The FIRST completion is authoritative and is
        -- never re-validated against a later call's params.
        if v_row.lease_generation = p_lease_generation and v_row.lease_holder_token = p_lease_holder_token then
            return query select 'already_completed'::text, v_row.storage_file_id;
        else
            return query select 'stale_lease'::text, v_row.storage_file_id;
        end if;
        return;
    end if;

    if v_row.lease_generation <> p_lease_generation or v_row.lease_holder_token <> p_lease_holder_token then
        -- Old worker's late completion after a takeover: fenced out harmlessly. The winning
        -- generation's own completion (if it already landed) is untouched by this call.
        return query select 'stale_lease'::text, null::text;
        return;
    end if;

    if v_row.reservation_state <> 'create_in_flight' then
        return query select 'stale_lease'::text, null::text;
        return;
    end if;

    if p_actual_sha256 <> v_row.expected_sha256 or p_actual_size <> v_row.expected_size then
        -- No partial completion: an integrity mismatch is always terminal, never left ambiguous.
        update private.legacy_storage_upload_reservations
           set reservation_state = 'failed_terminal',
               last_error_code = 'integrity_mismatch',
               actual_sha256 = p_actual_sha256,
               actual_size = p_actual_size,
               updated_at = now()
         where id = p_reservation_id;
        return query select 'integrity_mismatch'::text, null::text;
        return;
    end if;

    update private.legacy_storage_upload_reservations
       set reservation_state = 'completed',
           storage_file_id = p_storage_file_id,
           actual_sha256 = p_actual_sha256,
           actual_size = p_actual_size,
           completed_at = now(),
           updated_at = now()
     where id = p_reservation_id;

    return query select 'completed'::text, p_storage_file_id;
end;
$$;

comment on function private.complete_legacy_storage_upload(uuid, uuid, bigint, text, text, bigint) is
    'Fencing check first (generation primary, token secondary), then requires create_in_flight and '
    'an exact expected_sha256/expected_size match before recording completion. Never raises for a '
    'stale/fenced caller -- returns stale_lease so the caller can branch instead of crashing.';

revoke execute on function private.complete_legacy_storage_upload(uuid, uuid, bigint, text, text, bigint) from public;
grant execute on function private.complete_legacy_storage_upload(uuid, uuid, bigint, text, text, bigint) to migration_operator;

-- ===========================================================================
-- 5. mark_legacy_storage_upload_failure(...)
--    returns failed_retryable | failed_terminal | stale_lease | conflict
-- ===========================================================================
create or replace function private.mark_legacy_storage_upload_failure(
    p_reservation_id uuid,
    p_lease_holder_token uuid,
    p_lease_generation bigint,
    p_error_code text,
    p_outcome text
)
returns table(status text)
language plpgsql
security definer
set search_path = ''
as $$
#variable_conflict use_column
declare
    v_row private.legacy_storage_upload_reservations%rowtype;
begin
    if p_reservation_id is null or p_lease_holder_token is null or p_lease_generation is null then
        raise exception 'reservation_id, lease_holder_token and lease_generation are required' using errcode = 'not_null_violation';
    end if;
    if p_error_code is null or p_error_code !~ '^[a-z0-9_]+$' then
        raise exception 'error_code must be a non-empty lowercase snake_case category' using errcode = 'invalid_parameter_value';
    end if;
    if p_outcome not in ('known_not_created', 'unknown_after_create_attempt') then
        raise exception 'invalid outcome %', p_outcome using errcode = 'invalid_parameter_value';
    end if;

    select * into v_row from private.legacy_storage_upload_reservations where id = p_reservation_id for update;
    if not found then
        raise exception 'reservation % not found', p_reservation_id using errcode = 'no_data_found';
    end if;

    if v_row.lease_generation <> p_lease_generation or v_row.lease_holder_token <> p_lease_holder_token then
        return query select 'stale_lease'::text;
        return;
    end if;

    if v_row.reservation_state not in ('reserved', 'create_in_flight') then
        -- Already completed/failed_*: nothing to mark, and never trust a stale-outcome claim.
        return query select 'conflict'::text;
        return;
    end if;

    if p_outcome = 'known_not_created' then
        -- Only trustworthy from 'reserved' (windows A/B): nothing external could have started.
        -- A caller claiming known_not_created from create_in_flight is rejected, never trusted.
        if v_row.reservation_state <> 'reserved' then
            return query select 'conflict'::text;
            return;
        end if;
        update private.legacy_storage_upload_reservations
           set reservation_state = 'failed_retryable',
               last_error_code = p_error_code,
               attempt_count = attempt_count + 1,
               updated_at = now()
         where id = p_reservation_id;
        return query select 'failed_retryable'::text;
        return;
    end if;

    -- p_outcome = 'unknown_after_create_attempt': accepted only from create_in_flight. This is a
    -- no-op on reservation_state -- it never "releases" the lease early. The row stays
    -- create_in_flight so a future claimer (this worker's own retry or a takeover after TTL) is
    -- still forced through the mandatory recovery lookup instead of blindly retrying a create.
    if v_row.reservation_state <> 'create_in_flight' then
        return query select 'conflict'::text;
        return;
    end if;
    update private.legacy_storage_upload_reservations
       set last_error_code = p_error_code,
           updated_at = now()
     where id = p_reservation_id;
    return query select 'failed_retryable'::text;
end;
$$;

comment on function private.mark_legacy_storage_upload_failure(uuid, uuid, bigint, text, text) is
    'known_not_created (only trustworthy from reserved) frees the identity immediately as '
    'failed_retryable. unknown_after_create_attempt (accepted from create_in_flight) records the '
    'error but leaves reservation_state untouched -- the caller never blindly retries a create '
    'whose outcome is genuinely unknown; recovery goes through takeover + narrow lookup instead.';

revoke execute on function private.mark_legacy_storage_upload_failure(uuid, uuid, bigint, text, text) from public;
grant execute on function private.mark_legacy_storage_upload_failure(uuid, uuid, bigint, text, text) to migration_operator;

commit;
