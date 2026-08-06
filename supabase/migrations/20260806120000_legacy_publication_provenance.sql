-- Legacy publication provenance schema — Fase 2A1 (structural, additive only).
--
-- Creates three private tables that anchor migration of legacy (SQLite-era) publications
-- into the social schema, without introducing any operational behavior: no SECURITY
-- DEFINER function, no operational role, no data. Fase 2A2 builds the migrator functions
-- and the migration_operator/migration_publication_admin roles on top of this foundation.
--
-- Canonical identity: (source_system, source_instance_id, legacy_publication_id).
-- source_instance_id (private.legacy_migration_sources) is a UUID minted once at
-- controlled source registration — never derived from a file hash, path, worktree, branch
-- or title, and never recalculated. A physical SQLite capture's file hash changes across
-- WAL checkpoints/VACUUM/unrelated writes even when nothing migration-relevant changed;
-- treating that hash as identity would let the same legacy publication register twice
-- under what the system thinks are two different sources. Hashes/fingerprints stay in the
-- schema as auditable evidence columns, never as components of a uniqueness constraint.
--
-- draft_recovery (PUB-0002) can never reach `community` through this schema alone: the
-- state-consistency checks below are the first of two independent structural layers ("a
-- primeira camada estrutural implementada na 2A1"); the second layer — a separate
-- SECURITY DEFINER authorization function plus a dedicated administrative role — is 2A2.
--
-- No PII beyond auth.users UUID references. No token/cookie/password/connection string/
-- PDF content/translated text is ever stored — only hashes, sizes and categorical codes.
--
-- RLS is enabled on all three tables with zero policies (same fail-closed pattern as
-- private.chapter_assets), and PUBLIC/anon/authenticated hold no grant whatsoever.

begin;

-- ===========================================================================
-- 1. private.legacy_migration_sources — one row per physically registered source.
-- ===========================================================================
create table if not exists private.legacy_migration_sources (
    source_instance_id uuid primary key default gen_random_uuid(),
    source_system text not null,
    source_schema_version int not null,
    initial_snapshot_sha256 text not null,
    initial_logical_fingerprint text not null,
    registered_at timestamptz not null default now(),
    registered_by text not null,
    source_state text not null default 'registered',
    verified_at timestamptz,
    retired_at timestamptz,
    blocked_at timestamptz,
    blocked_reason text,
    invalidated_at timestamptz,
    invalidated_reason text,
    source_reference text,
    private_metadata jsonb not null default '{}'::jsonb,
    manifest_id uuid not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint legacy_migration_sources_snapshot_hex
        check (initial_snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    constraint legacy_migration_sources_state_check
        check (source_state in ('registered', 'verified', 'retired', 'blocked', 'invalidated')),
    constraint legacy_migration_sources_verified_at_consistent
        check (source_state <> 'verified' or verified_at is not null),
    constraint legacy_migration_sources_blocked_reason_required
        check (source_state <> 'blocked' or blocked_reason is not null),
    constraint legacy_migration_sources_invalidated_reason_required
        check (source_state <> 'invalidated' or invalidated_reason is not null),
    constraint legacy_migration_sources_metadata_is_object
        check (jsonb_typeof(private_metadata) = 'object')
);

comment on table private.legacy_migration_sources is
    'Registry of physically distinct legacy (SQLite-era) sources. source_instance_id is the '
    'stable identity, minted once at controlled registration; never exposed to anon/authenticated. '
    'No policy exists for any client role. Fase 2A1: structural only, no register_legacy_source function.';
comment on column private.legacy_migration_sources.source_instance_id is
    'Stable source identity. Never derived from file hash/path/worktree/branch/title; never '
    'recalculated after registration; only register_legacy_source (Fase 2A2) may mint one.';
comment on column private.legacy_migration_sources.initial_snapshot_sha256 is
    'SHA-256 of the physical capture used at registration. Integrity evidence only — never a '
    'component of identity (see identity_correction.md); no unique constraint on this column.';
comment on column private.legacy_migration_sources.initial_logical_fingerprint is
    'Logical-content fingerprint of the initial capture, used to distinguish legitimate source '
    'evolution from a genuinely different source when a later snapshot hash diverges. Evidence only.';
comment on column private.legacy_migration_sources.private_metadata is
    'Sanitized metadata only: never a token, cookie, password, connection string, local filesystem '
    'path, or database content. Must be a JSON object.';

create index legacy_migration_sources_state_idx
    on private.legacy_migration_sources (source_state);
create index legacy_migration_sources_system_idx
    on private.legacy_migration_sources (source_system);
create unique index legacy_migration_sources_manifest_unique
    on private.legacy_migration_sources (manifest_id);

alter table private.legacy_migration_sources enable row level security;
revoke all on private.legacy_migration_sources from public, anon, authenticated;

-- ===========================================================================
-- 2. private.legacy_publication_migrations — one row per legacy publication being migrated.
-- ===========================================================================
create table if not exists private.legacy_publication_migrations (
    id uuid primary key default gen_random_uuid(),

    -- Canonical identity (source_system is redundant with the FK, kept for index/audit
    -- readability; a trigger below guarantees it never diverges from the registered source).
    source_system text not null,
    source_instance_id uuid not null
        references private.legacy_migration_sources (source_instance_id) on delete restrict,
    source_schema_version int not null,
    legacy_publication_id text not null,
    legacy_file_id text,
    source_job_id text,
    source_run_id text,
    source_record_digest text not null,

    -- Ownership — never trusted from a raw caller payload; nulled (never cascaded) on user delete.
    owner_id uuid references auth.users (id) on delete set null,
    owner_resolution_state text not null,
    owner_verified_at timestamptz,

    -- Artifact integrity (evidence only; the PDF content itself is never stored).
    pdf_sha256 text,
    pdf_size_bytes bigint,
    source_storage_provider text,
    source_storage_file_id text,
    source_storage_folder_id text,

    -- Migration intent.
    migration_mode text not null,
    requested_target_status text not null,
    preserve_legacy_publication_state boolean not null default true,

    -- Technical + authorization state (two independent dimensions, see state_machine_final.md).
    migration_state text not null,
    publication_authorization_state text not null default 'none',
    attempt_count int not null default 0,
    last_error_code text,
    started_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),

    -- Targets — never cascaded from; removal preserves the row (tombstone below).
    target_work_id uuid references public.works (id) on delete set null,
    target_chapter_id uuid references public.chapters (id) on delete set null,
    target_chapter_asset_id uuid references private.chapter_assets (chapter_id) on delete set null,
    source_snapshot_digest text,

    -- Audit (process/operator identifiers — not client UUIDs).
    created_by text,
    completed_by text,
    private_metadata jsonb not null default '{}'::jsonb,

    -- Retention tombstones (Decisão 9) — exclusion never deletes provenance/events.
    owner_deleted_at timestamptz,
    source_retired_at timestamptz,
    target_removed_at timestamptz,
    invalidated_at timestamptz,
    invalidated_reason text,

    constraint legacy_pub_migrations_identity_unique
        unique (source_system, source_instance_id, legacy_publication_id),

    constraint legacy_pub_migrations_pdf_sha_hex
        check (pdf_sha256 is null or pdf_sha256 ~ '^[0-9a-f]{64}$'),
    constraint legacy_pub_migrations_size_positive
        check (pdf_size_bytes is null or pdf_size_bytes > 0),
    constraint legacy_pub_migrations_record_digest_not_empty
        check (length(trim(source_record_digest)) > 0),
    constraint legacy_pub_migrations_attempt_count_non_negative
        check (attempt_count >= 0),

    constraint legacy_pub_migrations_mode_check
        check (migration_mode in ('published_copy', 'draft_recovery')),
    constraint legacy_pub_migrations_requested_status_check
        check (requested_target_status in ('draft', 'private', 'community')),
    constraint legacy_pub_migrations_state_check
        check (migration_state in ('planned', 'claimed', 'work_created', 'chapter_created',
            'asset_pending', 'asset_linked', 'recovery_completed', 'publication_authorized',
            'completed', 'failed_retryable', 'failed_terminal', 'cancelled', 'rolled_back',
            'invalidated')),
    constraint legacy_pub_migrations_authorization_state_check
        check (publication_authorization_state in ('none', 'authorized', 'invalidated', 'published')),

    -- Cross-dimension consistency (Decisão 12/13, state_machine_final.md).
    constraint legacy_pub_migrations_recovery_completed_requires_draft_mode
        check (migration_state <> 'recovery_completed' or migration_mode = 'draft_recovery'),
    constraint legacy_pub_migrations_publication_authorized_requires_recovery
        check (migration_state <> 'publication_authorized' or publication_authorization_state = 'authorized'),
    constraint legacy_pub_migrations_completed_draft_requires_authorization
        check (
            not (migration_mode = 'draft_recovery' and migration_state = 'completed')
            or publication_authorization_state = 'published'
        ),
    -- First structural layer blocking draft_recovery -> community without authorization; the
    -- second layer (function-level rejection + role separation) is Fase 2A2.
    constraint legacy_pub_migrations_draft_never_community_via_operator
        check (
            not (migration_mode = 'draft_recovery' and requested_target_status = 'community')
            or publication_authorization_state = 'published'
        ),

    -- Terminal-state and target-presence invariants (unchanged from the pre-identity design).
    constraint legacy_pub_migrations_completed_at_required
        check (migration_state not in ('completed', 'recovery_completed') or completed_at is not null),
    constraint legacy_pub_migrations_work_required_from_work_created
        check (migration_state not in ('work_created', 'chapter_created', 'asset_pending',
            'asset_linked', 'recovery_completed', 'publication_authorized', 'completed')
            or target_work_id is not null),
    constraint legacy_pub_migrations_chapter_required_from_chapter_created
        check (migration_state not in ('chapter_created', 'asset_pending', 'asset_linked',
            'recovery_completed', 'publication_authorized', 'completed')
            or target_chapter_id is not null),
    -- published_copy must have a verified asset to reach `completed`; draft_recovery may not
    -- (recovery itself is the goal — the asset can arrive later, out of this migrator's scope).
    constraint legacy_pub_migrations_asset_required_for_published_completed
        check (
            not (migration_mode = 'published_copy' and migration_state = 'completed')
            or target_chapter_asset_id is not null
        ),

    constraint legacy_pub_migrations_invalidated_reason_required
        check (migration_state <> 'invalidated' or invalidated_reason is not null),
    constraint legacy_pub_migrations_metadata_is_object
        check (jsonb_typeof(private_metadata) = 'object')
);

comment on table private.legacy_publication_migrations is
    'One row per legacy publication being migrated. Canonical identity is '
    '(source_system, source_instance_id, legacy_publication_id) — never the physical file hash. '
    'draft_recovery can never self-promote to community from this schema alone (Fase 2A2 adds the '
    'second structural layer). Never exposed to anon/authenticated; zero policies.';
comment on column private.legacy_publication_migrations.source_system is
    'Redundant with the source_instance_id FK on purpose (index/audit readability); a trigger '
    'below rejects any row whose source_system diverges from the registered source — the '
    'registered source is always the authority, this column can never drift from it.';
comment on column private.legacy_publication_migrations.migration_mode is
    'published_copy: full publish pipeline. draft_recovery (PUB-0002): recovery to draft only — '
    'never self-publishes to community; publication requires a separate administrative '
    'authorization function that does not exist in this schema (Fase 2A2).';
comment on column private.legacy_publication_migrations.publication_authorization_state is
    'Human-authorization dimension, independent of migration_state. none/authorized/invalidated/'
    'published. Never set by anything in this schema in Fase 2A1 — no function writes it yet.';
comment on column private.legacy_publication_migrations.owner_id is
    'Nullable by design: ON DELETE SET NULL preserves the provenance row and its events when the '
    'owning auth.users row is deleted; owner_deleted_at records when that happened.';
comment on column private.legacy_publication_migrations.target_chapter_asset_id is
    'References private.chapter_assets(chapter_id) — chapter_assets is keyed by chapter_id '
    '(one asset per chapter), not by a separate synthetic id.';
comment on column private.legacy_publication_migrations.private_metadata is
    'Sanitized only: never a token, cookie, password, PDF content, or translated text. Must be a '
    'JSON object.';

-- source_system must always match the source it references — enforced by trigger because
-- Postgres CHECK constraints cannot embed a subquery; this is a structural/schema validation
-- (not a Fase 2A2 operational function).
create or replace function private.legacy_pub_migrations_validate_source_system()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
    if new.source_system <> (
        select source_system from private.legacy_migration_sources
        where source_instance_id = new.source_instance_id
    ) then
        raise exception 'source_system does not match registered source %', new.source_instance_id
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;

comment on function private.legacy_pub_migrations_validate_source_system() is
    'Structural identity guard (Decisão 3): rejects INSERT/UPDATE when source_system diverges '
    'from the source already registered under source_instance_id. Not an operational function.';

create trigger legacy_pub_migrations_validate_source_system
    before insert or update on private.legacy_publication_migrations
    for each row execute function private.legacy_pub_migrations_validate_source_system();

-- Secondary identities — evidence/detection, scoped per source, never global.
create unique index legacy_pub_migrations_job_unique
    on private.legacy_publication_migrations (source_instance_id, source_job_id)
    where source_job_id is not null;
create unique index legacy_pub_migrations_run_unique
    on private.legacy_publication_migrations (source_instance_id, source_run_id)
    where source_run_id is not null;
create unique index legacy_pub_migrations_file_unique
    on private.legacy_publication_migrations (source_instance_id, legacy_file_id)
    where legacy_file_id is not null;

-- A target can carry provenance from only one migration (rolled_back/invalidated excluded).
create unique index legacy_pub_migrations_target_chapter_unique
    on private.legacy_publication_migrations (target_chapter_id)
    where target_chapter_id is not null and migration_state not in ('rolled_back', 'invalidated');
create unique index legacy_pub_migrations_target_asset_unique
    on private.legacy_publication_migrations (target_chapter_asset_id)
    where target_chapter_asset_id is not null and migration_state not in ('rolled_back', 'invalidated');

create index legacy_pub_migrations_source_instance_idx
    on private.legacy_publication_migrations (source_instance_id);
create index legacy_pub_migrations_state_idx
    on private.legacy_publication_migrations (migration_state);
create index legacy_pub_migrations_authorization_state_idx
    on private.legacy_publication_migrations (publication_authorization_state);
create index legacy_pub_migrations_owner_idx
    on private.legacy_publication_migrations (owner_id);
create index legacy_pub_migrations_target_work_idx
    on private.legacy_publication_migrations (target_work_id);
create index legacy_pub_migrations_retryable_idx
    on private.legacy_publication_migrations (migration_state)
    where migration_state = 'failed_retryable';
create index legacy_pub_migrations_pdf_sha256_idx
    on private.legacy_publication_migrations (pdf_sha256)
    where pdf_sha256 is not null;

alter table private.legacy_publication_migrations enable row level security;
revoke all on private.legacy_publication_migrations from public, anon, authenticated;

-- ===========================================================================
-- 3. private.legacy_publication_migration_events — append-only audit trail.
-- ===========================================================================
create table if not exists private.legacy_publication_migration_events (
    id uuid primary key default gen_random_uuid(),
    migration_id uuid not null
        references private.legacy_publication_migrations (id) on delete restrict,
    event_type text not null,
    previous_state text,
    next_state text,
    actor_id uuid references auth.users (id) on delete set null,
    error_code text,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    constraint legacy_pub_migration_events_type_check
        check (event_type in (
            'source_registered', 'migration_planned', 'migration_claimed', 'work_attached',
            'chapter_attached', 'asset_pending', 'asset_attached', 'migration_retryable_failure',
            'migration_terminal_failure', 'migration_completed', 'migration_cancelled',
            'migration_rolled_back', 'draft_publication_authorized',
            'draft_publication_authorization_invalidated', 'draft_published'
        )),
    constraint legacy_pub_migration_events_metadata_is_object
        check (jsonb_typeof(metadata) = 'object')
);

comment on table private.legacy_publication_migration_events is
    'Append-only event trail for legacy publication migrations. UPDATE/DELETE revoked from every '
    'role (Decisão 10). migration_id uses ON DELETE RESTRICT: a migration row is never deleted in '
    'normal operation, and this makes an accidental delete-while-referenced fail loudly instead of '
    'silently orphaning events. The full append-only guarantee (a trigger rejecting UPDATE/DELETE '
    'even for a privileged role) is Fase 2A2; Fase 2A1 relies on REVOKE alone.';
comment on column private.legacy_publication_migration_events.actor_id is
    'Nullable: ON DELETE SET NULL preserves the event when the acting auth.users row is deleted.';
comment on column private.legacy_publication_migration_events.metadata is
    'Sanitized only, same rule as private_metadata elsewhere. Must be a JSON object.';

create index legacy_pub_migration_events_migration_created_idx
    on private.legacy_publication_migration_events (migration_id, created_at);
create index legacy_pub_migration_events_type_idx
    on private.legacy_publication_migration_events (event_type);

alter table private.legacy_publication_migration_events enable row level security;
revoke all on private.legacy_publication_migration_events from public, anon, authenticated;

commit;
