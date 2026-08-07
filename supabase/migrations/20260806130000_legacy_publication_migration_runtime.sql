-- Legacy publication provenance — Fase 2A2 (operational runtime on top of Fase 2A1).
--
-- Adds exactly the objects Fase 2A2 owns: two NOLOGIN/NOINHERIT roles, eight migrator
-- SECURITY DEFINER functions, one separate administrative authorization function (+ its
-- local current_admin_actor_id() helper), and an unconditional append-only trigger on the
-- event table. No table is altered; the three private tables and their constraints stay
-- exactly as Fase 2A1 fixed them (supabase/migrations/20260806120000_legacy_publication_provenance.sql).
--
-- Every SECURITY DEFINER function below: set search_path = '' with fully schema-qualified
-- references, EXECUTE revoked from PUBLIC, EXECUTE granted only to the one role that should
-- call it, categorical RAISE EXCEPTION ... USING ERRCODE, no dynamic SQL, no SELECT *,
-- ownership/state validated before any write, actor_id derived from session context
-- (auth.uid() / current_admin_actor_id()) never from a client-supplied field.
--
-- draft_recovery can never reach `community` through this migration: complete_legacy_migration
-- unconditionally rejects migration_mode = 'draft_recovery' with requested_target_status =
-- 'community', and no function here writes publication_authorization_state = 'published'. The
-- later promotion function stays conceptual (migration_2a_scope.md) — out of scope.

begin;

-- ===========================================================================
-- 1. Roles — NOLOGIN/NOINHERIT, zero table grants, EXECUTE only.
-- ===========================================================================
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'migration_operator') then
        create role migration_operator noinherit nologin;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'migration_publication_admin') then
        create role migration_publication_admin noinherit nologin;
    end if;
end
$$;

comment on role migration_operator is
    'Fase 2A2 operational role. NOINHERIT/NOLOGIN — assumed via SET ROLE / a backend service '
    'claim, never a direct client credential. No membership in anon/authenticated. No DML on '
    'any private.* or public.works/chapters/private.chapter_assets table — EXECUTE only, on '
    'the eight migrator functions below. Never authorizes draft_recovery publication.';
comment on role migration_publication_admin is
    'Fase 2A2 administrative role, distinct from migration_operator. EXECUTE only on '
    'authorize_draft_recovery_publication. Never granted the migrator function set.';

-- The admin/migration connection (postgres) is granted membership so it can SET ROLE into
-- either operational role — the same pattern Supabase already uses to let postgres assume
-- anon/authenticated. This grants nothing to any client-facing role: anon/authenticated never
-- receive membership in migration_operator or migration_publication_admin. A backend service
-- process assumes migration_operator via SET ROLE over a connection already authenticated as
-- (or granted through) postgres — never as a new standalone login credential.
grant migration_operator to postgres;
grant migration_publication_admin to postgres;

-- Calling a function requires USAGE on its schema; this alone grants no table/row access.
grant usage on schema private to migration_operator, migration_publication_admin;
revoke all on schema public from migration_operator, migration_publication_admin;

-- Explicit, defense-in-depth: neither role has ever been granted DML on the three tables
-- (they never received any GRANT), but REVOKE ALL makes the guarantee unconditional and
-- future-proof against an accidental GRANT being added elsewhere.
revoke all on private.legacy_migration_sources,
    private.legacy_publication_migrations,
    private.legacy_publication_migration_events
    from migration_operator, migration_publication_admin;
revoke all on public.works, public.chapters, private.chapter_assets
    from migration_operator, migration_publication_admin;

-- ===========================================================================
-- 2. Append-only enforcement beyond REVOKE — a trigger that rejects UPDATE/DELETE
--    unconditionally, even for a role that might later be granted table privilege by mistake.
-- ===========================================================================
create or replace function private.legacy_pub_migration_events_block_mutation()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
    -- DELETE: NEW is not assigned at all in a DELETE trigger, so this must be checked first
    -- and unconditionally, before any NEW.* reference below.
    if tg_op = 'DELETE' then
        raise exception 'legacy_publication_migration_events is append-only; DELETE is not permitted'
            using errcode = 'insufficient_privilege';
    end if;

    -- UPDATE: the one legitimate mutation is the Fase 2A1 retention contract's
    -- actor_id -> NULL via ON DELETE SET NULL (updated_constraints.md Decisao 9) when the
    -- acting auth.users row is deleted. Every other column must be byte-identical for this
    -- narrow exception to apply; anything else falls through to the unconditional reject.
    if new.actor_id is null and old.actor_id is not null
       and new.id = old.id and new.migration_id = old.migration_id
       and new.event_type = old.event_type
       and new.previous_state is not distinct from old.previous_state
       and new.next_state is not distinct from old.next_state
       and new.error_code is not distinct from old.error_code
       and new.metadata = old.metadata
       and new.created_at = old.created_at
    then
        return new;
    end if;

    raise exception 'legacy_publication_migration_events is append-only; UPDATE is not permitted'
        using errcode = 'insufficient_privilege';
end;
$$;

comment on function private.legacy_pub_migration_events_block_mutation() is
    'Fase 2A2: append-only guard. UPDATE/DELETE on legacy_publication_migration_events is '
    'rejected structurally, independent of any GRANT (defense in depth beyond the Fase 2A1 '
    'REVOKE) — except the single Fase 2A1 retention case of actor_id being nulled by '
    'ON DELETE SET NULL when the acting auth.users row is deleted, with every other column '
    'unchanged.';

drop trigger if exists legacy_pub_migration_events_no_update on private.legacy_publication_migration_events;
drop trigger if exists legacy_pub_migration_events_no_delete on private.legacy_publication_migration_events;
create trigger legacy_pub_migration_events_no_update
    before update on private.legacy_publication_migration_events
    for each row execute function private.legacy_pub_migration_events_block_mutation();
create trigger legacy_pub_migration_events_no_delete
    before delete on private.legacy_publication_migration_events
    for each row execute function private.legacy_pub_migration_events_block_mutation();

-- ===========================================================================
-- 3. current_admin_actor_id() — local placeholder for external admin-claim infrastructure.
--    Not defined by the contract (final_summary.md: "dependência externa... a resolver antes
--    da Fase 2A2"); no admin infrastructure exists elsewhere in this repo. Implemented as the
--    combination of a real authenticated session (auth.uid()) and a fresh, uncached role
--    membership check (pg_has_role) — never inferred from a stored flag or client payload.
-- ===========================================================================
create or replace function private.current_admin_actor_id()
returns uuid
language sql
stable
security invoker
set search_path = ''
as $$
    select case
        when pg_has_role(current_user, 'migration_publication_admin', 'member')
             and auth.uid() is not null
        then auth.uid()
        else null
    end;
$$;

comment on function private.current_admin_actor_id() is
    'Fase 2A2 local placeholder (see contract_audit.md #8): resolves to auth.uid() only when '
    'the calling session is both authenticated (auth.uid() is not null) and a fresh member of '
    'migration_publication_admin at call time. Returns NULL otherwise. Not a stored/cached flag.';

-- ===========================================================================
-- 4. register_legacy_source(manifest jsonb) returns uuid
-- ===========================================================================
create or replace function private.register_legacy_source(p_manifest jsonb)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_manifest_id uuid;
    v_proposed_id uuid;
    v_source_system text;
    v_schema_version int;
    v_snapshot_sha256 text;
    v_logical_fingerprint text;
    v_source_reference text;
    v_publication_count int;
    v_digest_count int;
    v_registered_by text;
    v_existing private.legacy_migration_sources%rowtype;
begin
    if p_manifest is null or jsonb_typeof(p_manifest) <> 'object' then
        raise exception 'manifest must be a JSON object' using errcode = 'invalid_parameter_value';
    end if;

    if coalesce((p_manifest->>'approval_state'), '') <> 'approved' then
        raise exception 'manifest is not approved' using errcode = 'insufficient_privilege';
    end if;

    v_manifest_id := nullif(p_manifest->>'manifest_id', '')::uuid;
    v_proposed_id := nullif(p_manifest->>'proposed_source_instance_id', '')::uuid;
    v_source_system := p_manifest->>'source_system';
    v_schema_version := nullif(p_manifest->>'source_schema_version', '')::int;
    v_snapshot_sha256 := p_manifest->>'snapshot_sha256';
    v_logical_fingerprint := p_manifest->>'logical_fingerprint';
    v_source_reference := p_manifest->>'source_reference';
    v_publication_count := nullif(p_manifest->>'publication_count', '')::int;
    v_registered_by := coalesce(p_manifest->>'approved_by', '');

    if v_manifest_id is null then
        raise exception 'manifest_id is required' using errcode = 'not_null_violation';
    end if;
    if v_proposed_id is null then
        raise exception 'proposed_source_instance_id is required' using errcode = 'not_null_violation';
    end if;
    if v_source_system is null or length(trim(v_source_system)) = 0 then
        raise exception 'source_system is required' using errcode = 'not_null_violation';
    end if;
    if v_schema_version is null then
        raise exception 'source_schema_version is required' using errcode = 'not_null_violation';
    end if;
    if v_snapshot_sha256 is null or v_snapshot_sha256 !~ '^[0-9a-f]{64}$' then
        raise exception 'snapshot_sha256 must be 64 lowercase hex characters' using errcode = 'invalid_parameter_value';
    end if;
    if v_logical_fingerprint is null or length(trim(v_logical_fingerprint)) = 0 then
        raise exception 'logical_fingerprint is required' using errcode = 'not_null_violation';
    end if;
    if length(trim(v_registered_by)) = 0 then
        raise exception 'approved_by is required' using errcode = 'not_null_violation';
    end if;
    if jsonb_typeof(coalesce(p_manifest->'source_record_digests', '[]'::jsonb)) <> 'array' then
        raise exception 'source_record_digests must be an array' using errcode = 'invalid_parameter_value';
    end if;

    select count(*) into v_digest_count
    from jsonb_array_elements(coalesce(p_manifest->'source_record_digests', '[]'::jsonb));
    if v_publication_count is null or v_publication_count <> v_digest_count then
        raise exception 'publication_count (%) does not match source_record_digests length (%)',
            v_publication_count, v_digest_count using errcode = 'invalid_parameter_value';
    end if;

    -- Advisory lock scoped to the exact (manifest_id, proposed_source_instance_id) pair: closes
    -- the check-then-insert race between two concurrent register_legacy_source calls for the
    -- same manifest (the lookups below would otherwise both see "not found" and both attempt
    -- the INSERT; the source_instance_id PRIMARY KEY still guarantees only one row is ever
    -- persisted, but without this lock the loser gets a raw duplicate-key error instead of the
    -- same clean idempotent result the winner gets).
    perform pg_advisory_xact_lock(hashtextextended(v_manifest_id::text || '|' || v_proposed_id::text, 1));

    -- Idempotent replay: the same approved manifest, already registered for the same proposed
    -- source, is a safe no-op returning the existing identity. Never silently create a second
    -- source_instance_id for a manifest already used (source_manifest_contract.md).
    select * into v_existing
    from private.legacy_migration_sources
    where manifest_id = v_manifest_id
    for update;

    if found then
        if v_existing.source_instance_id <> v_proposed_id
           or v_existing.source_system <> v_source_system
           or v_existing.initial_snapshot_sha256 <> v_snapshot_sha256
        then
            raise exception 'manifest_id % is already bound to a different source registration', v_manifest_id
                using errcode = 'unique_violation';
        end if;
        return v_existing.source_instance_id;
    end if;

    -- Same physical source (same proposed_source_instance_id), a genuinely new manifest_id (a
    -- later re-capture): idempotency_algorithm_final.md step 2 — never mint a new
    -- source_instance_id here. Reconcile by logical_fingerprint instead of the physical hash
    -- (which is expected to differ across WAL checkpoints/VACUUM/unrelated writes).
    select * into v_existing
    from private.legacy_migration_sources
    where source_instance_id = v_proposed_id
    for update;

    if found then
        if v_existing.source_system <> v_source_system then
            raise exception 'proposed_source_instance_id % is already registered under a different source_system', v_proposed_id
                using errcode = 'unique_violation';
        end if;
        if v_existing.initial_logical_fingerprint = v_logical_fingerprint then
            -- Legitimate evolution of the same source: reuse the existing identity as-is.
            -- The original approved manifest/snapshot hash is never edited in place.
            return v_existing.source_instance_id;
        end if;
        raise exception 'snapshot for source_instance_id % diverges incompatibly from the registered manifest (source_snapshot_changed)', v_proposed_id
            using errcode = 'PT001';
    end if;

    insert into private.legacy_migration_sources (
        source_instance_id, source_system, source_schema_version, initial_snapshot_sha256,
        initial_logical_fingerprint, registered_by, source_reference, manifest_id
    ) values (
        v_proposed_id, v_source_system, v_schema_version, v_snapshot_sha256,
        v_logical_fingerprint, v_registered_by, v_source_reference, v_manifest_id
    );

    return v_proposed_id;
end;
$$;

comment on function private.register_legacy_source(jsonb) is
    'Fase 2A2. Only mints source_instance_id from an approved manifest; never derives it from a '
    'hash/path. Idempotent on a manifest already registered compatibly; categorical rejection '
    'on manifest_id/proposed_source_instance_id conflict.';

revoke execute on function private.register_legacy_source(jsonb) from public;
grant execute on function private.register_legacy_source(jsonb) to migration_operator;

-- ===========================================================================
-- 5. claim_legacy_publication(...) returns table(migration_id uuid, result text)
-- ===========================================================================
create or replace function private.claim_legacy_publication(
    p_source_instance_id uuid,
    p_legacy_publication_id text,
    p_source_schema_version int,
    p_source_record_digest text,
    p_migration_mode text,
    p_requested_target_status text,
    p_owner_id uuid default null,
    p_owner_resolution_state text default 'unresolved',
    p_legacy_file_id text default null,
    p_source_job_id text default null,
    p_source_run_id text default null,
    p_pdf_sha256 text default null,
    p_pdf_size_bytes bigint default null,
    p_source_storage_provider text default null,
    p_source_storage_file_id text default null,
    p_source_storage_folder_id text default null,
    p_source_snapshot_digest text default null,
    p_private_metadata jsonb default '{}'::jsonb
)
returns table(migration_id uuid, result text)
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_source private.legacy_migration_sources%rowtype;
    v_existing private.legacy_publication_migrations%rowtype;
    v_new_id uuid;
    v_result text;
begin
    if p_source_instance_id is null then
        raise exception 'source_instance_id is required' using errcode = 'not_null_violation';
    end if;
    if p_legacy_publication_id is null or length(trim(p_legacy_publication_id)) = 0 then
        raise exception 'legacy_publication_id is required' using errcode = 'not_null_violation';
    end if;
    if p_source_record_digest is null or length(trim(p_source_record_digest)) = 0 then
        raise exception 'source_record_digest is required' using errcode = 'not_null_violation';
    end if;
    if p_migration_mode not in ('published_copy', 'draft_recovery') then
        raise exception 'invalid migration_mode %', p_migration_mode using errcode = 'invalid_parameter_value';
    end if;
    if p_requested_target_status not in ('draft', 'private', 'community') then
        raise exception 'invalid requested_target_status %', p_requested_target_status using errcode = 'invalid_parameter_value';
    end if;
    -- Same structural, unconditional block as complete_legacy_migration (intentional
    -- redundancy, draft_authorization_policy.md): a draft_recovery migration can never even be
    -- claimed with requested_target_status = community, since publication_authorization_state
    -- cannot be 'published' before the row exists — the Fase 2A1 CHECK constraint
    -- legacy_pub_migrations_draft_never_community_via_operator would reject the raw INSERT
    -- anyway; this turns that into a clean categorical error instead of a bare constraint
    -- violation reaching the caller.
    if p_migration_mode = 'draft_recovery' and p_requested_target_status = 'community' then
        raise exception 'draft_recovery cannot request community status via migrator claim; '
            'requires authorize_draft_recovery_publication and a separate promotion operation'
            using errcode = 'insufficient_privilege';
    end if;
    if p_pdf_sha256 is not null and p_pdf_sha256 !~ '^[0-9a-f]{64}$' then
        raise exception 'pdf_sha256 must be 64 lowercase hex characters' using errcode = 'invalid_parameter_value';
    end if;

    -- Advisory lock scoped to the exact canonical identity: serializes concurrent claims for
    -- the same (source_instance_id, legacy_publication_id) without holding a table-wide lock.
    -- Released automatically at transaction end (xact-scoped).
    perform pg_advisory_xact_lock(hashtextextended(p_source_instance_id::text || '|' || p_legacy_publication_id, 0));

    select * into v_source from private.legacy_migration_sources where source_instance_id = p_source_instance_id;
    if not found then
        raise exception 'source_instance_id % is not registered', p_source_instance_id using errcode = 'no_data_found';
    end if;
    if v_source.source_state in ('blocked', 'invalidated') then
        raise exception 'source_instance_id % is % and cannot accept new claims', p_source_instance_id, v_source.source_state
            using errcode = 'insufficient_privilege';
    end if;

    insert into private.legacy_publication_migrations (
        source_system, source_instance_id, source_schema_version, legacy_publication_id,
        legacy_file_id, source_job_id, source_run_id, source_record_digest,
        owner_id, owner_resolution_state,
        pdf_sha256, pdf_size_bytes, source_storage_provider, source_storage_file_id, source_storage_folder_id,
        migration_mode, requested_target_status, migration_state, source_snapshot_digest, private_metadata
    ) values (
        v_source.source_system, p_source_instance_id, p_source_schema_version, p_legacy_publication_id,
        p_legacy_file_id, p_source_job_id, p_source_run_id, p_source_record_digest,
        p_owner_id, p_owner_resolution_state,
        p_pdf_sha256, p_pdf_size_bytes, p_source_storage_provider, p_source_storage_file_id, p_source_storage_folder_id,
        p_migration_mode, p_requested_target_status, 'claimed', p_source_snapshot_digest, coalesce(p_private_metadata, '{}'::jsonb)
    )
    on conflict (source_system, source_instance_id, legacy_publication_id) do nothing
    returning id into v_new_id;

    if v_new_id is not null then
        insert into private.legacy_publication_migration_events
            (migration_id, event_type, previous_state, next_state, metadata)
        values (v_new_id, 'migration_claimed', null, 'claimed', jsonb_build_object('reason', 'created'));
        return query select v_new_id, 'created'::text;
        return;
    end if;

    select * into v_existing
    from private.legacy_publication_migrations
    where source_system = v_source.source_system
      and source_instance_id = p_source_instance_id
      and legacy_publication_id = p_legacy_publication_id
    for update;

    -- Artifact-identity/integrity mismatch (the record itself, or its PDF) is always the most
    -- severe category, independent of target state. Ownership mismatch is evaluated separately
    -- below, scoped by whether a partial target already exists — that is what distinguishes
    -- partial_target_conflicting from a blanket conflicting_identity (idempotency_algorithm_final.md
    -- step 7).
    if v_existing.source_record_digest <> p_source_record_digest
       or (v_existing.pdf_sha256 is not null and p_pdf_sha256 is not null and v_existing.pdf_sha256 <> p_pdf_sha256)
    then
        v_result := 'conflicting_identity';
    elsif v_existing.migration_state = 'completed' then
        v_result := 'already_completed';
    elsif v_existing.migration_state = 'recovery_completed' then
        v_result := 'recovery_completed';
    elsif v_existing.migration_state in ('failed_terminal', 'cancelled', 'rolled_back', 'invalidated') then
        v_result := 'conflicting_identity';
    elsif v_existing.target_chapter_id is not null
          and v_existing.migration_state in ('chapter_created', 'asset_pending', 'asset_linked', 'publication_authorized')
    then
        if p_owner_id is not null and v_existing.owner_id is not null and v_existing.owner_id <> p_owner_id then
            v_result := 'partial_target_conflicting';
        else
            v_result := 'partial_target_consistent';
        end if;
    elsif p_owner_id is not null and v_existing.owner_id is not null and v_existing.owner_id <> p_owner_id then
        -- No partial target yet, but a different owner is claiming the same canonical identity —
        -- still a genuine conflict, just not a target-scoped one.
        v_result := 'conflicting_identity';
    else
        v_result := 'resumable';
    end if;

    return query select v_existing.id, v_result;
end;
$$;

comment on function private.claim_legacy_publication(
    uuid, text, int, text, text, text, uuid, text, text, text, text, text, bigint, text, text, text, text, jsonb
) is
    'Fase 2A2. Reserve-or-classify per idempotency_algorithm_final.md step 4-8. Advisory-lock '
    'scoped to the exact canonical identity guards concurrent claims. Never mutates an existing '
    'row''s identity fields; classification is read-only beyond the first insert.';

revoke execute on function private.claim_legacy_publication(
    uuid, text, int, text, text, text, uuid, text, text, text, text, text, bigint, text, text, text, text, jsonb
) from public;
grant execute on function private.claim_legacy_publication(
    uuid, text, int, text, text, text, uuid, text, text, text, text, text, bigint, text, text, text, text, jsonb
) to migration_operator;

-- ===========================================================================
-- 6. attach_migration_work(migration_id uuid, work_id uuid) returns void
-- ===========================================================================
create or replace function private.attach_migration_work(p_migration_id uuid, p_work_id uuid)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_migration private.legacy_publication_migrations%rowtype;
    v_work_owner uuid;
begin
    if p_migration_id is null or p_work_id is null then
        raise exception 'migration_id and work_id are required' using errcode = 'not_null_violation';
    end if;

    select * into v_migration from private.legacy_publication_migrations where id = p_migration_id for update;
    if not found then
        raise exception 'migration % not found' , p_migration_id using errcode = 'no_data_found';
    end if;

    if v_migration.target_work_id is not null then
        if v_migration.target_work_id = p_work_id then
            return; -- idempotent no-op: identity already attached, retry does not change it
        end if;
        raise exception 'migration % already has a different target_work_id', p_migration_id using errcode = 'object_not_in_prerequisite_state';
    end if;

    if v_migration.migration_state <> 'claimed' then
        raise exception 'migration % is in state % (expected claimed)', p_migration_id, v_migration.migration_state
            using errcode = 'object_not_in_prerequisite_state';
    end if;

    select owner_id into v_work_owner from public.works where id = p_work_id;
    if not found then
        raise exception 'work % not found', p_work_id using errcode = 'no_data_found';
    end if;
    if v_migration.owner_id is null then
        raise exception 'migration % has no resolved owner_id; cannot attach a public target', p_migration_id
            using errcode = 'insufficient_privilege';
    end if;
    if v_work_owner is distinct from v_migration.owner_id then
        raise exception 'work % owner does not match migration owner', p_work_id using errcode = 'insufficient_privilege';
    end if;

    update private.legacy_publication_migrations
       set target_work_id = p_work_id, migration_state = 'work_created', updated_at = now()
     where id = p_migration_id;

    insert into private.legacy_publication_migration_events
        (migration_id, event_type, previous_state, next_state, metadata)
    values (p_migration_id, 'work_attached', v_migration.migration_state, 'work_created',
            jsonb_build_object('work_id', p_work_id));
end;
$$;

comment on function private.attach_migration_work(uuid, uuid) is
    'Fase 2A2. Attaches an existing public.works row (never creates one). owner_id always '
    'comes from the already-validated migration row, never trusted from a caller field.';

revoke execute on function private.attach_migration_work(uuid, uuid) from public;
grant execute on function private.attach_migration_work(uuid, uuid) to migration_operator;

-- ===========================================================================
-- 7. attach_migration_chapter(migration_id uuid, chapter_id uuid) returns void
-- ===========================================================================
create or replace function private.attach_migration_chapter(p_migration_id uuid, p_chapter_id uuid)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_migration private.legacy_publication_migrations%rowtype;
    v_chapter_work_id uuid;
begin
    if p_migration_id is null or p_chapter_id is null then
        raise exception 'migration_id and chapter_id are required' using errcode = 'not_null_violation';
    end if;

    select * into v_migration from private.legacy_publication_migrations where id = p_migration_id for update;
    if not found then
        raise exception 'migration % not found', p_migration_id using errcode = 'no_data_found';
    end if;

    if v_migration.target_chapter_id is not null then
        if v_migration.target_chapter_id = p_chapter_id then
            return; -- idempotent no-op
        end if;
        raise exception 'migration % already has a different target_chapter_id', p_migration_id using errcode = 'object_not_in_prerequisite_state';
    end if;

    if v_migration.migration_state <> 'work_created' then
        raise exception 'migration % is in state % (expected work_created)', p_migration_id, v_migration.migration_state
            using errcode = 'object_not_in_prerequisite_state';
    end if;
    if v_migration.target_work_id is null then
        raise exception 'migration % has no attached work', p_migration_id using errcode = 'object_not_in_prerequisite_state';
    end if;

    select work_id into v_chapter_work_id from public.chapters where id = p_chapter_id;
    if not found then
        raise exception 'chapter % not found', p_chapter_id using errcode = 'no_data_found';
    end if;
    if v_chapter_work_id is distinct from v_migration.target_work_id then
        raise exception 'chapter % does not belong to migration target work', p_chapter_id using errcode = 'insufficient_privilege';
    end if;

    if exists (
        select 1 from private.legacy_publication_migrations
        where target_chapter_id = p_chapter_id
          and migration_state not in ('rolled_back', 'invalidated')
          and id <> p_migration_id
    ) then
        raise exception 'chapter % already carries provenance from another migration', p_chapter_id using errcode = 'unique_violation';
    end if;

    update private.legacy_publication_migrations
       set target_chapter_id = p_chapter_id, migration_state = 'chapter_created', updated_at = now()
     where id = p_migration_id;

    insert into private.legacy_publication_migration_events
        (migration_id, event_type, previous_state, next_state, metadata)
    values (p_migration_id, 'chapter_attached', v_migration.migration_state, 'chapter_created',
            jsonb_build_object('chapter_id', p_chapter_id));
end;
$$;

comment on function private.attach_migration_chapter(uuid, uuid) is
    'Fase 2A2. Validates chapter->work membership and rejects a chapter already carrying live '
    'provenance from another migration before writing.';

revoke execute on function private.attach_migration_chapter(uuid, uuid) from public;
grant execute on function private.attach_migration_chapter(uuid, uuid) to migration_operator;

-- ===========================================================================
-- 8. attach_migration_asset(migration_id, chapter_id, expected_pdf_sha256, expected_pdf_size,
--                            expected_storage_provider, expected_storage_file_id) returns void
--    target_chapter_asset_id references private.chapter_assets(chapter_id) (1 asset/chapter).
-- ===========================================================================
create or replace function private.attach_migration_asset(
    p_migration_id uuid,
    p_chapter_id uuid,
    p_expected_pdf_sha256 text,
    p_expected_pdf_size_bytes bigint,
    p_expected_storage_provider text,
    p_expected_storage_file_id text
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_migration private.legacy_publication_migrations%rowtype;
    v_asset private.chapter_assets%rowtype;
    v_auth_meta jsonb;
begin
    if p_migration_id is null or p_chapter_id is null then
        raise exception 'migration_id and chapter_id are required' using errcode = 'not_null_violation';
    end if;
    if p_expected_pdf_sha256 is null or p_expected_pdf_sha256 !~ '^[0-9a-f]{64}$' then
        raise exception 'expected_pdf_sha256 must be 64 lowercase hex characters' using errcode = 'invalid_parameter_value';
    end if;

    select * into v_migration from private.legacy_publication_migrations where id = p_migration_id for update;
    if not found then
        raise exception 'migration % not found', p_migration_id using errcode = 'no_data_found';
    end if;

    -- recovery_completed/publication_authorized are included so a draft_recovery asset can be
    -- attached or replaced after recovery/authorization (draft_authorization_policy.md's
    -- invalidation rule only makes sense if attach_migration_asset can still run at that
    -- point); migration_mode/migration_state DB constraints already make those two states
    -- unreachable for published_copy, so this is never a loophole for that mode.
    if v_migration.migration_state not in (
        'chapter_created', 'asset_pending', 'asset_linked', 'recovery_completed', 'publication_authorized'
    ) then
        raise exception 'migration % is in state % (expected chapter_created, asset_pending, asset_linked, recovery_completed or publication_authorized)',
            p_migration_id, v_migration.migration_state
            using errcode = 'object_not_in_prerequisite_state';
    end if;
    if v_migration.target_chapter_id is distinct from p_chapter_id then
        raise exception 'chapter % does not match migration target_chapter_id', p_chapter_id using errcode = 'insufficient_privilege';
    end if;

    -- A genuinely different chapter is already rejected above (target_chapter_id must match),
    -- so target_chapter_asset_id can only ever be NULL (first attach) or already = p_chapter_id
    -- (a re-verify/hash-change call, e.g. the asset was re-uploaded — that path is exactly what
    -- feeds the authorization-invalidation check below). A call that changes nothing at all is
    -- a safe idempotent no-op.
    if v_migration.target_chapter_asset_id is not null
       and v_migration.target_chapter_asset_id = p_chapter_id
       and v_migration.pdf_sha256 = p_expected_pdf_sha256
    then
        return;
    end if;

    select * into v_asset from private.chapter_assets where chapter_id = p_chapter_id;
    if not found or v_asset.deleted_at is not null then
        raise exception 'no live asset found for chapter %', p_chapter_id using errcode = 'no_data_found';
    end if;
    if v_asset.checksum_sha256 is distinct from p_expected_pdf_sha256 then
        raise exception 'asset checksum for chapter % does not match expected_pdf_sha256', p_chapter_id using errcode = 'invalid_parameter_value';
    end if;
    if p_expected_pdf_size_bytes is not null and v_asset.byte_size is distinct from p_expected_pdf_size_bytes then
        raise exception 'asset byte_size for chapter % does not match expected_pdf_size_bytes', p_chapter_id using errcode = 'invalid_parameter_value';
    end if;
    if p_expected_storage_provider is not null and v_asset.storage_provider is distinct from p_expected_storage_provider then
        raise exception 'asset storage_provider for chapter % does not match expected', p_chapter_id using errcode = 'invalid_parameter_value';
    end if;
    if p_expected_storage_file_id is not null and v_asset.storage_file_id is distinct from p_expected_storage_file_id then
        raise exception 'asset storage_file_id for chapter % does not match expected', p_chapter_id using errcode = 'invalid_parameter_value';
    end if;

    -- Invalidate a standing draft_recovery authorization if this asset diverges from the one
    -- an admin actually authorized (draft_authorization_policy.md: "Invalidada se asset/hash
    -- mudar"). The authorized expectation is read from the latest draft_publication_authorized
    -- event — there is no dedicated column on the row (see contract_audit.md #4).
    if v_migration.publication_authorization_state = 'authorized' then
        select metadata into v_auth_meta
        from private.legacy_publication_migration_events
        where migration_id = p_migration_id and event_type = 'draft_publication_authorized'
        order by created_at desc
        limit 1;

        if v_auth_meta is null
           or (v_auth_meta->>'expected_pdf_sha256') is distinct from p_expected_pdf_sha256
           or (nullif(v_auth_meta->>'expected_asset_id', '')::uuid) is distinct from p_chapter_id
        then
            -- legacy_pub_migrations_publication_authorized_requires_recovery (Fase 2A1) demands
            -- migration_state <> 'publication_authorized' whenever publication_authorization_state
            -- isn't 'authorized' — so invalidating the authorization must also step migration_state
            -- back to recovery_completed in the same UPDATE, undoing exactly the transition that
            -- authorize_draft_recovery_publication made.
            update private.legacy_publication_migrations
               set publication_authorization_state = 'invalidated',
                   migration_state = case when migration_state = 'publication_authorized' then 'recovery_completed' else migration_state end
             where id = p_migration_id;
            insert into private.legacy_publication_migration_events
                (migration_id, event_type, previous_state, next_state, metadata)
            values (p_migration_id, 'draft_publication_authorization_invalidated', 'authorized', 'invalidated',
                    jsonb_build_object('reason', 'asset_or_hash_changed', 'new_pdf_sha256', p_expected_pdf_sha256,
                                        'new_asset_chapter_id', p_chapter_id));
        end if;
    end if;

    -- Only the forward chapter_created/asset_pending -> asset_linked transition advances
    -- migration_state; a re-verify/replace call arriving after recovery_completed/
    -- publication_authorized updates the asset fields (and runs the invalidation check above)
    -- without regressing migration_state — there is no such backward transition in
    -- state_machine_final.md.
    update private.legacy_publication_migrations
       set target_chapter_asset_id = p_chapter_id,
           pdf_sha256 = p_expected_pdf_sha256,
           pdf_size_bytes = coalesce(p_expected_pdf_size_bytes, pdf_size_bytes),
           migration_state = case
               when migration_state in ('chapter_created', 'asset_pending') then 'asset_linked'
               else migration_state
           end,
           updated_at = now()
     where id = p_migration_id;

    insert into private.legacy_publication_migration_events
        (migration_id, event_type, previous_state, next_state, metadata)
    values (p_migration_id, 'asset_attached', v_migration.migration_state,
            case when v_migration.migration_state in ('chapter_created', 'asset_pending') then 'asset_linked' else v_migration.migration_state end,
            jsonb_build_object('chapter_id', p_chapter_id, 'pdf_sha256', p_expected_pdf_sha256));
end;
$$;

comment on function private.attach_migration_asset(uuid, uuid, text, bigint, text, text) is
    'Fase 2A2. Never touches Google Drive; validates only the already-persisted '
    'private.chapter_assets row against expected checksum/size/provider/file id. Invalidates a '
    'standing draft_recovery authorization whose expected asset/hash no longer matches.';

revoke execute on function private.attach_migration_asset(uuid, uuid, text, bigint, text, text) from public;
grant execute on function private.attach_migration_asset(uuid, uuid, text, bigint, text, text) to migration_operator;

-- ===========================================================================
-- 9. mark_migration_failure(migration_id uuid, error_code text, retryable boolean) returns void
-- ===========================================================================
create or replace function private.mark_migration_failure(p_migration_id uuid, p_error_code text, p_retryable boolean)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_migration private.legacy_publication_migrations%rowtype;
    v_next_state text;
    v_event_type text;
begin
    if p_migration_id is null then
        raise exception 'migration_id is required' using errcode = 'not_null_violation';
    end if;
    if p_error_code is null or p_error_code !~ '^[a-z0-9_]+$' then
        raise exception 'error_code must be a non-empty lowercase snake_case category' using errcode = 'invalid_parameter_value';
    end if;
    if p_retryable is null then
        raise exception 'retryable is required' using errcode = 'not_null_violation';
    end if;

    select * into v_migration from private.legacy_publication_migrations where id = p_migration_id for update;
    if not found then
        raise exception 'migration % not found', p_migration_id using errcode = 'no_data_found';
    end if;

    if v_migration.migration_state in (
        'completed', 'recovery_completed', 'failed_terminal', 'cancelled', 'rolled_back', 'invalidated'
    ) then
        raise exception 'migration % is in terminal state % and cannot be marked failed', p_migration_id, v_migration.migration_state
            using errcode = 'object_not_in_prerequisite_state';
    end if;

    v_next_state := case when p_retryable then 'failed_retryable' else 'failed_terminal' end;
    v_event_type := case when p_retryable then 'migration_retryable_failure' else 'migration_terminal_failure' end;

    update private.legacy_publication_migrations
       set migration_state = v_next_state,
           last_error_code = p_error_code,
           attempt_count = attempt_count + 1,
           updated_at = now()
     where id = p_migration_id;

    insert into private.legacy_publication_migration_events
        (migration_id, event_type, previous_state, next_state, error_code, metadata)
    values (p_migration_id, v_event_type, v_migration.migration_state, v_next_state, p_error_code, '{}'::jsonb);
end;
$$;

comment on function private.mark_migration_failure(uuid, text, boolean) is
    'Fase 2A2. Only a categorical error_code is ever stored (no stack trace, token, URL, or raw '
    'payload). failed_terminal requires an explicit later action; failed_retryable can be '
    'resumed via the matching attach_*/claim function.';

revoke execute on function private.mark_migration_failure(uuid, text, boolean) from public;
grant execute on function private.mark_migration_failure(uuid, text, boolean) to migration_operator;

-- ===========================================================================
-- 10. complete_legacy_migration(migration_id uuid, completed_by text) returns text
-- ===========================================================================
create or replace function private.complete_legacy_migration(p_migration_id uuid, p_completed_by text)
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_migration private.legacy_publication_migrations%rowtype;
begin
    if p_migration_id is null then
        raise exception 'migration_id is required' using errcode = 'not_null_violation';
    end if;

    select * into v_migration from private.legacy_publication_migrations where id = p_migration_id for update;
    if not found then
        raise exception 'migration % not found', p_migration_id using errcode = 'no_data_found';
    end if;

    -- Structural, unconditional block: the migrator's normal completion path can never turn a
    -- draft_recovery into community — that requires authorize_draft_recovery_publication (a
    -- separate administrative role) plus a promotion function that is out of scope for 2A2.
    if v_migration.migration_mode = 'draft_recovery' and v_migration.requested_target_status = 'community' then
        raise exception 'draft_recovery cannot request community status via migrator completion; '
            'requires authorize_draft_recovery_publication and a separate promotion operation'
            using errcode = 'insufficient_privilege';
    end if;

    if v_migration.migration_mode = 'published_copy' then
        if v_migration.migration_state = 'completed' then
            return 'already_completed';
        end if;
        if v_migration.migration_state <> 'asset_linked' then
            raise exception 'migration % is in state % (expected asset_linked)', p_migration_id, v_migration.migration_state
                using errcode = 'object_not_in_prerequisite_state';
        end if;
        if v_migration.target_work_id is null or v_migration.target_chapter_id is null
           or v_migration.target_chapter_asset_id is null or v_migration.pdf_sha256 is null
        then
            raise exception 'migration % is missing work/chapter/asset/pdf_sha256 required for completion', p_migration_id
                using errcode = 'object_not_in_prerequisite_state';
        end if;
        if exists (
            select 1 from private.legacy_publication_migrations
            where target_chapter_id = v_migration.target_chapter_id
              and migration_state not in ('rolled_back', 'invalidated')
              and id <> p_migration_id
        ) then
            raise exception 'target chapter % has a colliding live migration', v_migration.target_chapter_id
                using errcode = 'unique_violation';
        end if;

        update private.legacy_publication_migrations
           set migration_state = 'completed', completed_at = now(), completed_by = p_completed_by, updated_at = now()
         where id = p_migration_id;

        insert into private.legacy_publication_migration_events
            (migration_id, event_type, previous_state, next_state, metadata)
        values (p_migration_id, 'migration_completed', v_migration.migration_state, 'completed', '{}'::jsonb);

        return 'completed';
    end if;

    -- draft_recovery, requested_target_status in ('draft', 'private') — technical recovery only.
    if v_migration.migration_state = 'recovery_completed' then
        return 'recovery_completed';
    end if;
    if v_migration.migration_state not in ('chapter_created', 'asset_linked') then
        raise exception 'migration % is in state % (expected chapter_created or asset_linked)', p_migration_id, v_migration.migration_state
            using errcode = 'object_not_in_prerequisite_state';
    end if;
    if v_migration.target_work_id is null or v_migration.target_chapter_id is null then
        raise exception 'migration % is missing work/chapter required for recovery completion', p_migration_id
            using errcode = 'object_not_in_prerequisite_state';
    end if;

    update private.legacy_publication_migrations
       set migration_state = 'recovery_completed', completed_at = now(), completed_by = p_completed_by, updated_at = now()
     where id = p_migration_id;

    insert into private.legacy_publication_migration_events
        (migration_id, event_type, previous_state, next_state, metadata)
    values (p_migration_id, 'migration_completed', v_migration.migration_state, 'recovery_completed', '{}'::jsonb);

    return 'recovery_completed';
end;
$$;

comment on function private.complete_legacy_migration(uuid, text) is
    'Fase 2A2. published_copy -> completed requires a verified work+chapter+asset+pdf_sha256. '
    'draft_recovery -> recovery_completed never requires an asset. draft_recovery + '
    'requested_target_status=community is unconditionally rejected regardless of authorization '
    'state (the real promotion function is out of scope). Idempotent: a second call returns '
    'already_completed/recovery_completed instead of erroring.';

revoke execute on function private.complete_legacy_migration(uuid, text) from public;
grant execute on function private.complete_legacy_migration(uuid, text) to migration_operator;

-- ===========================================================================
-- 11. append_legacy_migration_event(migration_id, event_type, metadata, error_code) returns uuid
--     Pure append-only annotation — never mutates migration_state itself.
-- ===========================================================================
create or replace function private.append_legacy_migration_event(
    p_migration_id uuid,
    p_event_type text,
    p_metadata jsonb default '{}'::jsonb,
    p_error_code text default null
)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_current_state text;
    v_event_id uuid;
begin
    if p_migration_id is null then
        raise exception 'migration_id is required' using errcode = 'not_null_violation';
    end if;
    if p_event_type is null or p_event_type not in (
        'source_registered', 'migration_planned', 'migration_claimed', 'work_attached',
        'chapter_attached', 'asset_pending', 'asset_attached', 'migration_retryable_failure',
        'migration_terminal_failure', 'migration_completed', 'migration_cancelled',
        'migration_rolled_back', 'draft_publication_authorized',
        'draft_publication_authorization_invalidated', 'draft_published'
    ) then
        raise exception 'invalid event_type %', p_event_type using errcode = 'invalid_parameter_value';
    end if;
    if p_metadata is not null and jsonb_typeof(p_metadata) <> 'object' then
        raise exception 'metadata must be a JSON object' using errcode = 'invalid_parameter_value';
    end if;

    select migration_state into v_current_state
    from private.legacy_publication_migrations
    where id = p_migration_id;
    if not found then
        raise exception 'migration % not found', p_migration_id using errcode = 'no_data_found';
    end if;

    insert into private.legacy_publication_migration_events
        (migration_id, event_type, previous_state, next_state, error_code, metadata)
    values (p_migration_id, p_event_type, v_current_state, v_current_state, p_error_code, coalesce(p_metadata, '{}'::jsonb))
    returning id into v_event_id;

    return v_event_id;
end;
$$;

comment on function private.append_legacy_migration_event(uuid, text, jsonb, text) is
    'Fase 2A2. Standalone append-only annotation for the migrator; never updates or deletes an '
    'existing event, never changes migration_state (the dedicated transition functions do that).';

revoke execute on function private.append_legacy_migration_event(uuid, text, jsonb, text) from public;
grant execute on function private.append_legacy_migration_event(uuid, text, jsonb, text) to migration_operator;

-- ===========================================================================
-- 12. authorize_draft_recovery_publication(...) returns uuid — administrative, separate role.
-- ===========================================================================
create or replace function private.authorize_draft_recovery_publication(
    p_migration_id uuid,
    p_target_chapter_id uuid,
    p_expected_pdf_sha256 text,
    p_expected_asset_id uuid,
    p_expected_state text,
    p_reason text
)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_actor_id uuid := private.current_admin_actor_id();
    v_migration private.legacy_publication_migrations%rowtype;
    v_authorization_id uuid;
begin
    if v_actor_id is null then
        raise exception 'no authenticated administrative actor' using errcode = 'insufficient_privilege';
    end if;
    if p_reason is null or length(trim(p_reason)) = 0 then
        raise exception 'reason is required' using errcode = 'not_null_violation';
    end if;
    if p_migration_id is null or p_target_chapter_id is null or p_expected_state is null then
        raise exception 'migration_id, target_chapter_id and expected_state are required' using errcode = 'not_null_violation';
    end if;
    if p_expected_pdf_sha256 is not null and p_expected_pdf_sha256 !~ '^[0-9a-f]{64}$' then
        raise exception 'expected_pdf_sha256 must be 64 lowercase hex characters' using errcode = 'invalid_parameter_value';
    end if;

    select * into v_migration from private.legacy_publication_migrations where id = p_migration_id for update;
    if not found then
        raise exception 'migration % not found', p_migration_id using errcode = 'no_data_found';
    end if;
    if v_migration.migration_mode <> 'draft_recovery' then
        raise exception 'migration % is not draft_recovery', p_migration_id using errcode = 'invalid_parameter_value';
    end if;
    if v_migration.migration_state <> p_expected_state or v_migration.migration_state <> 'recovery_completed' then
        raise exception 'migration % state is % (expected % = recovery_completed)',
            p_migration_id, v_migration.migration_state, p_expected_state
            using errcode = 'object_not_in_prerequisite_state';
    end if;
    if v_migration.target_chapter_id is distinct from p_target_chapter_id then
        raise exception 'target_chapter_id % does not match migration', p_target_chapter_id using errcode = 'invalid_parameter_value';
    end if;
    if p_expected_asset_id is not null and v_migration.target_chapter_asset_id is distinct from p_expected_asset_id then
        raise exception 'expected_asset_id % does not match migration target_chapter_asset_id', p_expected_asset_id
            using errcode = 'invalid_parameter_value';
    end if;
    if p_expected_pdf_sha256 is not null and v_migration.pdf_sha256 is distinct from p_expected_pdf_sha256 then
        raise exception 'expected_pdf_sha256 does not match migration pdf_sha256' using errcode = 'invalid_parameter_value';
    end if;

    insert into private.legacy_publication_migration_events
        (migration_id, event_type, previous_state, next_state, actor_id, metadata)
    values (p_migration_id, 'draft_publication_authorized', p_expected_state, p_expected_state, v_actor_id,
            jsonb_build_object(
                'target_chapter_id', p_target_chapter_id,
                'expected_pdf_sha256', p_expected_pdf_sha256,
                'expected_asset_id', p_expected_asset_id,
                'reason', p_reason))
    returning id into v_authorization_id;

    update private.legacy_publication_migrations
       set publication_authorization_state = 'authorized',
           migration_state = 'publication_authorized'
     where id = p_migration_id and migration_state = 'recovery_completed';

    return v_authorization_id;
end;
$$;

comment on function private.authorize_draft_recovery_publication(uuid, uuid, text, uuid, text, text) is
    'Fase 2A2 administrative authorization, separate from the migrator role. Does not publish '
    'anything by itself: only records the draft_publication_authorized event and moves '
    'publication_authorization_state to authorized / migration_state to publication_authorized. '
    'actor_id always comes from current_admin_actor_id(), never from a payload field.';

revoke execute on function private.authorize_draft_recovery_publication(uuid, uuid, text, uuid, text, text) from public;
grant execute on function private.authorize_draft_recovery_publication(uuid, uuid, text, uuid, text, text) to migration_publication_admin;
-- migration_operator is never granted EXECUTE here — enforced by omission, verified by test.

commit;
