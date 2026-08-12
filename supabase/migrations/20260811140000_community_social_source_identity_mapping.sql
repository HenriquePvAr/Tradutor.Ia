-- Canonical social source identity mapping for History publication.
--
-- public.works/public.chapters keep presentation and social state.  This private
-- layer binds server-derived source identities to those public rows, so retries,
-- response loss and concurrent attempts resolve the same canonical ids before
-- publication metadata reservation or Drive upload.

begin;

create table if not exists private.community_source_work_mappings (
    id uuid primary key default gen_random_uuid(),
    owner_id uuid not null references public.profiles (id) on delete cascade,
    source_provider text not null,
    source_work_key text not null,
    source_identity_hash text,
    source_identity_version int not null default 1,
    work_id uuid not null references public.works (id) on delete restrict,
    first_source_job_id text,
    first_source_run_id text,
    first_reconstruction_job_id text,
    first_reconstruction_run_id text,
    created_at timestamptz not null default now(),
    constraint community_source_work_provider_check
        check (source_provider = lower(trim(source_provider)) and length(source_provider) between 1 and 80),
    constraint community_source_work_key_check
        check (length(trim(source_work_key)) between 1 and 300),
    constraint community_source_work_hash_check
        check (source_identity_hash is null or source_identity_hash ~ '^[0-9a-f]{64}$'),
    constraint community_source_work_version_check
        check (source_identity_version > 0),
    constraint community_source_work_identity_unique
        unique (owner_id, source_provider, source_work_key),
    constraint community_source_work_id_unique
        unique (work_id),
    constraint community_source_work_relation_unique
        unique (work_id, owner_id)
);

comment on table private.community_source_work_mappings is
    'Server-owned immutable mapping from canonical source work identity to public.works.id. No raw anon/authenticated access.';
comment on column private.community_source_work_mappings.source_work_key is
    'Provider-specific canonical work identity such as a provider native series id. Never a title, slug, filename or fuzzy display string.';
comment on column private.community_source_work_mappings.first_source_job_id is
    'First-binding provenance only. A later reconstruction of the same source must reuse the mapping.';

create table if not exists private.community_source_chapter_mappings (
    id uuid primary key default gen_random_uuid(),
    work_mapping_id uuid not null
        references private.community_source_work_mappings (id) on delete cascade,
    source_chapter_key text not null,
    source_identity_hash text,
    source_identity_version int not null default 1,
    chapter_id uuid not null references public.chapters (id) on delete restrict,
    first_source_job_id text,
    first_source_run_id text,
    first_reconstruction_job_id text,
    first_reconstruction_run_id text,
    created_at timestamptz not null default now(),
    constraint community_source_chapter_key_check
        check (length(trim(source_chapter_key)) between 1 and 400),
    constraint community_source_chapter_hash_check
        check (source_identity_hash is null or source_identity_hash ~ '^[0-9a-f]{64}$'),
    constraint community_source_chapter_version_check
        check (source_identity_version > 0),
    constraint community_source_chapter_identity_unique
        unique (work_mapping_id, source_chapter_key),
    constraint community_source_chapter_id_unique
        unique (chapter_id)
);

comment on table private.community_source_chapter_mappings is
    'Server-owned immutable mapping from canonical source chapter identity to public.chapters.id under a source work mapping.';
comment on column private.community_source_chapter_mappings.source_chapter_key is
    'Provider-specific canonical chapter identity such as a provider native series id plus episode id. Never a title, filename or fuzzy display string.';

create index if not exists community_source_work_owner_idx
    on private.community_source_work_mappings (owner_id);
create index if not exists community_source_work_work_idx
    on private.community_source_work_mappings (work_id);
create index if not exists community_source_chapter_work_mapping_idx
    on private.community_source_chapter_mappings (work_mapping_id);
create index if not exists community_source_chapter_chapter_idx
    on private.community_source_chapter_mappings (chapter_id);

alter table private.community_source_work_mappings enable row level security;
alter table private.community_source_chapter_mappings enable row level security;

revoke all on private.community_source_work_mappings from public, anon, authenticated;
revoke all on private.community_source_chapter_mappings from public, anon, authenticated;

create or replace function public.resolve_or_materialize_community_source_identity(
    p_owner_id uuid,
    p_source_provider text,
    p_source_work_key text,
    p_source_chapter_key text,
    p_source_identity_hash text default null,
    p_source_identity_version int default 1,
    p_work_title text default '',
    p_work_slug text default '',
    p_chapter_number numeric default null,
    p_chapter_title text default '',
    p_first_source_job_id text default null,
    p_first_source_run_id text default null,
    p_first_reconstruction_job_id text default null,
    p_first_reconstruction_run_id text default null
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_provider text := lower(trim(coalesce(p_source_provider, '')));
    v_work_key text := trim(coalesce(p_source_work_key, ''));
    v_chapter_key text := trim(coalesce(p_source_chapter_key, ''));
    v_hash text := nullif(lower(trim(coalesce(p_source_identity_hash, ''))), '');
    v_version int := coalesce(p_source_identity_version, 1);
    v_work_title text := nullif(trim(coalesce(p_work_title, '')), '');
    v_work_slug text := lower(trim(coalesce(p_work_slug, '')));
    v_chapter_title text := nullif(trim(coalesce(p_chapter_title, '')), '');
    v_work_mapping private.community_source_work_mappings%rowtype;
    v_chapter_mapping private.community_source_chapter_mappings%rowtype;
    v_work_id uuid;
    v_chapter_id uuid;
    v_work_created boolean := false;
    v_chapter_created boolean := false;
    v_owner uuid;
    v_chapter_work uuid;
begin
    if p_owner_id is null then
        raise exception 'owner_id is required' using errcode = 'not_null_violation';
    end if;
    if v_provider = '' or length(v_provider) > 80 then
        raise exception 'invalid_source_provider' using errcode = 'invalid_parameter_value';
    end if;
    if v_work_key = '' or length(v_work_key) > 300 then
        raise exception 'invalid_source_work_key' using errcode = 'invalid_parameter_value';
    end if;
    if v_chapter_key = '' or length(v_chapter_key) > 400 then
        raise exception 'invalid_source_chapter_key' using errcode = 'invalid_parameter_value';
    end if;
    if v_hash is not null and v_hash !~ '^[0-9a-f]{64}$' then
        raise exception 'invalid_source_identity_hash' using errcode = 'invalid_parameter_value';
    end if;
    if v_version <= 0 then
        raise exception 'invalid_source_identity_version' using errcode = 'invalid_parameter_value';
    end if;
    if v_work_title is null or char_length(v_work_title) > 200 then
        raise exception 'invalid_work_title' using errcode = 'invalid_parameter_value';
    end if;
    if v_work_slug = '' or char_length(v_work_slug) > 120 or v_work_slug !~ '^[a-z0-9-]+$' then
        raise exception 'invalid_work_slug' using errcode = 'invalid_parameter_value';
    end if;
    if p_chapter_number is null or p_chapter_number <= 0 then
        raise exception 'invalid_chapter_number' using errcode = 'invalid_parameter_value';
    end if;
    if v_chapter_title is not null and char_length(v_chapter_title) > 200 then
        raise exception 'invalid_chapter_title' using errcode = 'invalid_parameter_value';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(p_owner_id::text || '|' || v_provider || '|' || v_work_key || '|' || v_chapter_key, 0));

    select * into v_work_mapping
      from private.community_source_work_mappings
     where owner_id = p_owner_id
       and source_provider = v_provider
       and source_work_key = v_work_key
     for update;

    if found then
        if v_work_mapping.source_identity_version <> v_version then
            raise exception 'canonical_social_identity_conflict' using errcode = 'PT001';
        end if;
        select owner_id into v_owner from public.works where id = v_work_mapping.work_id and deleted_at is null;
        if not found or v_owner is distinct from p_owner_id then
            raise exception 'canonical_social_identity_conflict' using errcode = 'PT001';
        end if;
        v_work_id := v_work_mapping.work_id;
    else
        v_work_id := gen_random_uuid();
        insert into public.works (id, owner_id, title, slug, status)
        values (v_work_id, p_owner_id, v_work_title, v_work_slug, 'draft');
        insert into private.community_source_work_mappings (
            owner_id, source_provider, source_work_key, source_identity_hash,
            source_identity_version, work_id, first_source_job_id, first_source_run_id,
            first_reconstruction_job_id, first_reconstruction_run_id
        ) values (
            p_owner_id, v_provider, v_work_key, v_hash, v_version, v_work_id,
            nullif(p_first_source_job_id, ''), nullif(p_first_source_run_id, ''),
            nullif(p_first_reconstruction_job_id, ''), nullif(p_first_reconstruction_run_id, '')
        )
        returning * into v_work_mapping;
        v_work_created := true;
    end if;

    select * into v_chapter_mapping
      from private.community_source_chapter_mappings
     where work_mapping_id = v_work_mapping.id
       and source_chapter_key = v_chapter_key
     for update;

    if found then
        if v_chapter_mapping.source_identity_version <> v_version
           or (v_hash is not null and v_chapter_mapping.source_identity_hash is not null and v_chapter_mapping.source_identity_hash <> v_hash)
        then
            raise exception 'canonical_social_identity_conflict' using errcode = 'PT001';
        end if;
        select work_id into v_chapter_work from public.chapters where id = v_chapter_mapping.chapter_id and deleted_at is null;
        if not found or v_chapter_work is distinct from v_work_id then
            raise exception 'canonical_social_identity_conflict' using errcode = 'PT001';
        end if;
        v_chapter_id := v_chapter_mapping.chapter_id;
    else
        v_chapter_id := gen_random_uuid();
        insert into public.chapters (id, work_id, chapter_number, title, status)
        values (v_chapter_id, v_work_id, p_chapter_number, v_chapter_title, 'draft');
        insert into private.community_source_chapter_mappings (
            work_mapping_id, source_chapter_key, source_identity_hash,
            source_identity_version, chapter_id, first_source_job_id, first_source_run_id,
            first_reconstruction_job_id, first_reconstruction_run_id
        ) values (
            v_work_mapping.id, v_chapter_key, v_hash, v_version, v_chapter_id,
            nullif(p_first_source_job_id, ''), nullif(p_first_source_run_id, ''),
            nullif(p_first_reconstruction_job_id, ''), nullif(p_first_reconstruction_run_id, '')
        )
        returning * into v_chapter_mapping;
        v_chapter_created := true;
    end if;

    return jsonb_build_object(
        'work_id', v_work_id,
        'chapter_id', v_chapter_id,
        'work_created', v_work_created,
        'chapter_created', v_chapter_created
    );
end;
$$;

comment on function public.resolve_or_materialize_community_source_identity(
    uuid, text, text, text, text, int, text, text, numeric, text, text, text, text, text
) is
    'Backend-only canonical source identity materialization. Server-derived provider/work/chapter keys resolve to public work/chapter ids before metadata reservation or Drive.';

revoke all on function public.resolve_or_materialize_community_source_identity(
    uuid, text, text, text, text, int, text, text, numeric, text, text, text, text, text
) from public;
revoke execute on function public.resolve_or_materialize_community_source_identity(
    uuid, text, text, text, text, int, text, text, numeric, text, text, text, text, text
) from public;
revoke execute on function public.resolve_or_materialize_community_source_identity(
    uuid, text, text, text, text, int, text, text, numeric, text, text, text, text, text
) from anon;
revoke execute on function public.resolve_or_materialize_community_source_identity(
    uuid, text, text, text, text, int, text, text, numeric, text, text, text, text, text
) from authenticated;
grant execute on function public.resolve_or_materialize_community_source_identity(
    uuid, text, text, text, text, int, text, text, numeric, text, text, text, text, text
) to service_role;

commit;
