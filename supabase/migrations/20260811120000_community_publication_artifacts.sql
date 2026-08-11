-- Community publication artifact metadata (Supabase social completion).
--
-- The structured publication record lives in Supabase; PDF bytes remain in the
-- configured StorageProvider. This table is deliberately private because
-- storage_reference is an opaque backend-only provider reference (for example a
-- Google Drive file id). Browsers route by publication_id/chapter_id only.

begin;

create table if not exists private.community_publication_artifacts (
    publication_id uuid primary key references public.chapters (id) on delete cascade,
    owner_user_id uuid not null references public.profiles (id) on delete cascade,
    job_id text,
    run_id text,
    artifact_sha256 text not null,
    artifact_size_bytes bigint not null,
    mime_type text not null default 'application/pdf',
    storage_provider text not null,
    storage_reference text,
    publication_status text not null default 'reserved',
    title text,
    last_error_code text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint community_pub_artifacts_sha_hex
        check (artifact_sha256 ~ '^[0-9a-f]{64}$'),
    constraint community_pub_artifacts_size_positive
        check (artifact_size_bytes > 0),
    constraint community_pub_artifacts_mime_pdf
        check (mime_type = 'application/pdf'),
    constraint community_pub_artifacts_provider_check
        check (storage_provider in ('google_drive')),
    constraint community_pub_artifacts_status_check
        check (publication_status in (
            'reserved', 'uploading', 'uploaded', 'finalizing',
            'published', 'failed_retryable', 'failed_terminal'
        )),
    constraint community_pub_artifacts_published_has_storage
        check (publication_status <> 'published' or storage_reference is not null)
);

comment on table private.community_publication_artifacts is
    'Server-side metadata for published Community PDF artifacts. The browser uses publication_id/chapter_id; backend resolves storage_reference for authorized streaming.';
comment on column private.community_publication_artifacts.storage_reference is
    'Opaque provider reference such as a Drive file id. Never exposed to anon/authenticated.';

create unique index if not exists community_pub_artifacts_owner_job_run_idx
    on private.community_publication_artifacts (owner_user_id, job_id, run_id)
    where job_id is not null and run_id is not null;

create unique index if not exists community_pub_artifacts_owner_hash_idx
    on private.community_publication_artifacts (owner_user_id, artifact_sha256)
    where publication_status in ('reserved', 'uploading', 'uploaded', 'finalizing', 'published');

create index if not exists community_pub_artifacts_owner_status_idx
    on private.community_publication_artifacts (owner_user_id, publication_status);
create index if not exists community_pub_artifacts_storage_ref_idx
    on private.community_publication_artifacts (storage_provider, storage_reference)
    where storage_reference is not null;

alter table private.community_publication_artifacts enable row level security;
revoke all on private.community_publication_artifacts from anon, authenticated;

create trigger trg_community_pub_artifacts_updated_at
    before update on private.community_publication_artifacts
    for each row execute function public.set_updated_at();

commit;
