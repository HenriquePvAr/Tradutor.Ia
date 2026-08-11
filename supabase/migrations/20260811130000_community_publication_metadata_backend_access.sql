-- Backend-only RPC surface for private Community publication artifact metadata.
--
-- PDF bytes remain in the configured StorageProvider.  The private table remains
-- closed to anon/authenticated/service_role raw table access; privileged backend
-- writes happen only through these narrow SECURITY DEFINER functions.

begin;

create or replace function public.reserve_community_publication_artifact(
    p_publication_id uuid,
    p_owner_user_id uuid,
    p_job_id text,
    p_run_id text,
    p_artifact_sha256 text,
    p_artifact_size_bytes bigint,
    p_mime_type text default 'application/pdf',
    p_storage_provider text default 'google_drive',
    p_title text default ''
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_owner_user_id uuid;
    v_existing private.community_publication_artifacts%rowtype;
begin
    select w.owner_id
      into v_owner_user_id
      from public.chapters c
      join public.works w on w.id = c.work_id
     where c.id = p_publication_id
       and c.deleted_at is null
       and w.deleted_at is null;

    if v_owner_user_id is null then
        raise exception 'publication_missing' using errcode = 'P0002';
    end if;
    if v_owner_user_id <> p_owner_user_id then
        raise exception 'publication_owner_mismatch' using errcode = '42501';
    end if;
    if p_artifact_sha256 is null or p_artifact_sha256 !~ '^[0-9a-f]{64}$' then
        raise exception 'invalid_artifact_sha256' using errcode = '22023';
    end if;
    if p_artifact_size_bytes is null or p_artifact_size_bytes <= 0 then
        raise exception 'invalid_artifact_size' using errcode = '22023';
    end if;
    if coalesce(p_mime_type, 'application/pdf') <> 'application/pdf' then
        raise exception 'invalid_mime_type' using errcode = '22023';
    end if;
    if coalesce(p_storage_provider, '') <> 'google_drive' then
        raise exception 'invalid_storage_provider' using errcode = '22023';
    end if;

    select *
      into v_existing
      from private.community_publication_artifacts
     where publication_id = p_publication_id
     for update;

    if found then
        if v_existing.owner_user_id <> p_owner_user_id
           or v_existing.artifact_sha256 <> p_artifact_sha256
           or v_existing.artifact_size_bytes <> p_artifact_size_bytes
           or v_existing.mime_type <> coalesce(p_mime_type, 'application/pdf')
           or v_existing.storage_provider <> p_storage_provider
           or coalesce(v_existing.job_id, '') <> coalesce(p_job_id, '')
           or coalesce(v_existing.run_id, '') <> coalesce(p_run_id, '') then
            raise exception 'publication_reservation_conflict' using errcode = '23505';
        end if;
        return jsonb_build_object(
            'publication_id', v_existing.publication_id,
            'publication_status', v_existing.publication_status
        );
    end if;

    if exists (
        select 1
          from private.community_publication_artifacts a
         where a.owner_user_id = p_owner_user_id
           and a.publication_id <> p_publication_id
           and a.publication_status in ('reserved', 'uploading', 'uploaded', 'finalizing', 'published')
           and a.artifact_sha256 = p_artifact_sha256
         for update
    ) then
        raise exception 'duplicate_artifact_publication' using errcode = '23505';
    end if;

    if coalesce(p_job_id, '') <> '' and coalesce(p_run_id, '') <> '' and exists (
        select 1
          from private.community_publication_artifacts a
         where a.owner_user_id = p_owner_user_id
           and a.publication_id <> p_publication_id
           and a.job_id = p_job_id
           and a.run_id = p_run_id
         for update
    ) then
        raise exception 'duplicate_publication_run' using errcode = '23505';
    end if;

    insert into private.community_publication_artifacts (
        publication_id,
        owner_user_id,
        job_id,
        run_id,
        artifact_sha256,
        artifact_size_bytes,
        mime_type,
        storage_provider,
        publication_status,
        title
    )
    values (
        p_publication_id,
        p_owner_user_id,
        nullif(p_job_id, ''),
        nullif(p_run_id, ''),
        p_artifact_sha256,
        p_artifact_size_bytes,
        coalesce(p_mime_type, 'application/pdf'),
        p_storage_provider,
        'reserved',
        nullif(p_title, '')
    );

    return jsonb_build_object(
        'publication_id', p_publication_id,
        'publication_status', 'reserved'
    );
end;
$$;

create or replace function public.record_community_publication_storage(
    p_publication_id uuid,
    p_owner_user_id uuid,
    p_artifact_sha256 text,
    p_artifact_size_bytes bigint,
    p_storage_provider text,
    p_storage_reference text
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_owner_user_id uuid;
    v_existing private.community_publication_artifacts%rowtype;
begin
    if coalesce(p_storage_reference, '') = '' then
        raise exception 'missing_storage_reference' using errcode = '22023';
    end if;

    select w.owner_id
      into v_owner_user_id
      from public.chapters c
      join public.works w on w.id = c.work_id
     where c.id = p_publication_id
       and c.deleted_at is null
       and w.deleted_at is null;

    if v_owner_user_id is null then
        raise exception 'publication_missing' using errcode = 'P0002';
    end if;
    if v_owner_user_id <> p_owner_user_id then
        raise exception 'publication_owner_mismatch' using errcode = '42501';
    end if;

    select *
      into v_existing
      from private.community_publication_artifacts
     where publication_id = p_publication_id
     for update;

    if not found then
        raise exception 'publication_metadata_not_reserved' using errcode = 'P0002';
    end if;
    if v_existing.owner_user_id <> p_owner_user_id
       or v_existing.artifact_sha256 <> p_artifact_sha256
       or v_existing.artifact_size_bytes <> p_artifact_size_bytes
       or v_existing.storage_provider <> p_storage_provider then
        raise exception 'publication_artifact_mismatch' using errcode = '22023';
    end if;
    if v_existing.publication_status = 'failed_terminal' then
        raise exception 'invalid_publication_transition' using errcode = '22023';
    end if;
    if v_existing.storage_reference is not null
       and v_existing.storage_reference <> p_storage_reference then
        raise exception 'storage_reference_conflict' using errcode = '23505';
    end if;

    if v_existing.publication_status = 'published' then
        return jsonb_build_object(
            'publication_id', v_existing.publication_id,
            'publication_status', 'published'
        );
    end if;

    update private.community_publication_artifacts
       set storage_reference = p_storage_reference,
           publication_status = 'uploaded',
           last_error_code = null
     where publication_id = p_publication_id;

    return jsonb_build_object(
        'publication_id', p_publication_id,
        'publication_status', 'uploaded'
    );
end;
$$;

create or replace function public.finalize_community_publication_artifact(
    p_publication_id uuid,
    p_owner_user_id uuid,
    p_artifact_sha256 text,
    p_artifact_size_bytes bigint
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_owner_user_id uuid;
    v_existing private.community_publication_artifacts%rowtype;
begin
    select w.owner_id
      into v_owner_user_id
      from public.chapters c
      join public.works w on w.id = c.work_id
     where c.id = p_publication_id
       and c.deleted_at is null
       and w.deleted_at is null;

    if v_owner_user_id is null then
        raise exception 'publication_missing' using errcode = 'P0002';
    end if;
    if v_owner_user_id <> p_owner_user_id then
        raise exception 'publication_owner_mismatch' using errcode = '42501';
    end if;

    select *
      into v_existing
      from private.community_publication_artifacts
     where publication_id = p_publication_id
     for update;

    if not found then
        raise exception 'publication_metadata_not_reserved' using errcode = 'P0002';
    end if;
    if v_existing.owner_user_id <> p_owner_user_id
       or v_existing.artifact_sha256 <> p_artifact_sha256
       or v_existing.artifact_size_bytes <> p_artifact_size_bytes then
        raise exception 'publication_artifact_mismatch' using errcode = '22023';
    end if;
    if v_existing.storage_reference is null then
        raise exception 'missing_storage_reference' using errcode = '22023';
    end if;
    if v_existing.publication_status = 'published' then
        return jsonb_build_object(
            'publication_id', v_existing.publication_id,
            'publication_status', 'published'
        );
    end if;
    if v_existing.publication_status not in ('uploaded', 'finalizing') then
        raise exception 'invalid_publication_transition' using errcode = '22023';
    end if;

    update private.community_publication_artifacts
       set publication_status = 'published',
           last_error_code = null
     where publication_id = p_publication_id;

    return jsonb_build_object(
        'publication_id', p_publication_id,
        'publication_status', 'published'
    );
end;
$$;

create or replace function public.mark_community_publication_artifact_failure(
    p_publication_id uuid,
    p_failure_status text,
    p_last_error_code text
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_existing private.community_publication_artifacts%rowtype;
begin
    if p_failure_status not in ('failed_retryable', 'failed_terminal') then
        raise exception 'invalid_failure_status' using errcode = '22023';
    end if;

    select *
      into v_existing
      from private.community_publication_artifacts
     where publication_id = p_publication_id
     for update;

    if not found then
        raise exception 'publication_metadata_not_reserved' using errcode = 'P0002';
    end if;
    if v_existing.publication_status = 'published' then
        raise exception 'published_artifact_immutable' using errcode = '22023';
    end if;
    if v_existing.publication_status = 'failed_terminal' then
        return jsonb_build_object(
            'publication_id', v_existing.publication_id,
            'publication_status', 'failed_terminal'
        );
    end if;

    update private.community_publication_artifacts
       set publication_status = p_failure_status,
           last_error_code = left(coalesce(p_last_error_code, 'failed'), 120)
     where publication_id = p_publication_id;

    return jsonb_build_object(
        'publication_id', p_publication_id,
        'publication_status', p_failure_status
    );
end;
$$;

revoke all on function public.reserve_community_publication_artifact(
    uuid, uuid, text, text, text, bigint, text, text, text
) from public;
revoke execute on function public.reserve_community_publication_artifact(
    uuid, uuid, text, text, text, bigint, text, text, text
) from anon;
revoke execute on function public.reserve_community_publication_artifact(
    uuid, uuid, text, text, text, bigint, text, text, text
) from authenticated;
grant execute on function public.reserve_community_publication_artifact(
    uuid, uuid, text, text, text, bigint, text, text, text
) to service_role;

revoke all on function public.record_community_publication_storage(
    uuid, uuid, text, bigint, text, text
) from public;
revoke execute on function public.record_community_publication_storage(
    uuid, uuid, text, bigint, text, text
) from anon;
revoke execute on function public.record_community_publication_storage(
    uuid, uuid, text, bigint, text, text
) from authenticated;
grant execute on function public.record_community_publication_storage(
    uuid, uuid, text, bigint, text, text
) to service_role;

revoke all on function public.finalize_community_publication_artifact(
    uuid, uuid, text, bigint
) from public;
revoke execute on function public.finalize_community_publication_artifact(
    uuid, uuid, text, bigint
) from anon;
revoke execute on function public.finalize_community_publication_artifact(
    uuid, uuid, text, bigint
) from authenticated;
grant execute on function public.finalize_community_publication_artifact(
    uuid, uuid, text, bigint
) to service_role;

revoke all on function public.mark_community_publication_artifact_failure(
    uuid, text, text
) from public;
revoke execute on function public.mark_community_publication_artifact_failure(
    uuid, text, text
) from anon;
revoke execute on function public.mark_community_publication_artifact_failure(
    uuid, text, text
) from authenticated;
grant execute on function public.mark_community_publication_artifact_failure(
    uuid, text, text
) to service_role;

commit;
