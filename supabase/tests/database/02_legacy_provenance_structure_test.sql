-- Structural pgTAP tests for the legacy publication provenance schema (Fase 2A1).
-- Run via `supabase test db` (Docker). Everything happens in a rolled-back transaction.
--
-- Scope: private.legacy_migration_sources, private.legacy_publication_migrations,
-- private.legacy_publication_migration_events. Purely structural — existence, columns,
-- types, constraints, indexes, RLS enabled with zero policies, and revoked grants.
-- No SECURITY DEFINER function, no operational role: those are Fase 2A2.
begin;
select no_plan();

-- Tables exist ---------------------------------------------------------------
select has_table('private', 'legacy_migration_sources', 'legacy_migration_sources exists');
select has_table('private', 'legacy_publication_migrations', 'legacy_publication_migrations exists');
select has_table('private', 'legacy_publication_migration_events', 'legacy_publication_migration_events exists');

-- No new public table/view ----------------------------------------------------
select is(
    (select count(*)::int from information_schema.tables
     where table_schema = 'public'
       and table_name in ('legacy_migration_sources', 'legacy_publication_migrations',
                           'legacy_publication_migration_events')),
    0,
    'no legacy provenance table/view leaks into public schema'
);

-- No seed rows -----------------------------------------------------------------
select is((select count(*)::int from private.legacy_migration_sources), 0, 'no seed rows in legacy_migration_sources');
select is((select count(*)::int from private.legacy_publication_migrations), 0, 'no seed rows in legacy_publication_migrations');
select is((select count(*)::int from private.legacy_publication_migration_events), 0, 'no seed rows in legacy_publication_migration_events');

-- ===========================================================================
-- private.legacy_migration_sources
-- ===========================================================================
select col_is_pk('private', 'legacy_migration_sources', 'source_instance_id', 'sources pk is source_instance_id');
select col_type_is('private', 'legacy_migration_sources', 'source_instance_id', 'uuid', 'source_instance_id is uuid');
select col_type_is('private', 'legacy_migration_sources', 'source_system', 'text', 'source_system is text');
select col_type_is('private', 'legacy_migration_sources', 'source_schema_version', 'integer', 'source_schema_version is int');
select col_type_is('private', 'legacy_migration_sources', 'initial_snapshot_sha256', 'text', 'initial_snapshot_sha256 is text');
select col_type_is('private', 'legacy_migration_sources', 'initial_logical_fingerprint', 'text', 'initial_logical_fingerprint is text');
select col_type_is('private', 'legacy_migration_sources', 'source_state', 'text', 'source_state is text');
select col_type_is('private', 'legacy_migration_sources', 'private_metadata', 'jsonb', 'private_metadata is jsonb');
select col_type_is('private', 'legacy_migration_sources', 'manifest_id', 'uuid', 'manifest_id is uuid');
select col_not_null('private', 'legacy_migration_sources', 'source_system', 'source_system is not null');
select col_not_null('private', 'legacy_migration_sources', 'source_schema_version', 'source_schema_version is not null');
select col_not_null('private', 'legacy_migration_sources', 'initial_snapshot_sha256', 'initial_snapshot_sha256 is not null');
select col_not_null('private', 'legacy_migration_sources', 'initial_logical_fingerprint', 'initial_logical_fingerprint is not null');
select col_not_null('private', 'legacy_migration_sources', 'registered_by', 'registered_by is not null');
select col_not_null('private', 'legacy_migration_sources', 'manifest_id', 'manifest_id is not null');
select col_has_default('private', 'legacy_migration_sources', 'source_instance_id', 'source_instance_id has default');
select col_has_default('private', 'legacy_migration_sources', 'source_state', 'source_state has default');
select col_has_default('private', 'legacy_migration_sources', 'registered_at', 'registered_at has default');
select col_has_default('private', 'legacy_migration_sources', 'private_metadata', 'private_metadata has default');
select has_column('private', 'legacy_migration_sources', 'verified_at', 'has verified_at');
select has_column('private', 'legacy_migration_sources', 'retired_at', 'has retired_at');
select has_column('private', 'legacy_migration_sources', 'blocked_at', 'has blocked_at');
select has_column('private', 'legacy_migration_sources', 'blocked_reason', 'has blocked_reason');
select has_column('private', 'legacy_migration_sources', 'invalidated_at', 'has invalidated_at');
select has_column('private', 'legacy_migration_sources', 'invalidated_reason', 'has invalidated_reason');
select has_column('private', 'legacy_migration_sources', 'source_reference', 'has source_reference');

select has_index('private', 'legacy_migration_sources', 'legacy_migration_sources_manifest_unique', 'manifest_id', 'manifest_id has a unique index');

-- No unique on the snapshot hash (evidence, not identity).
select is(
    (select count(*)::int from pg_constraint co
     join pg_class c on c.oid = co.conrelid
     join pg_namespace n on n.oid = c.relnamespace
     where n.nspname = 'private' and c.relname = 'legacy_migration_sources'
       and co.contype = 'u'
       and co.conkey = (
           select array_agg(a.attnum) from pg_attribute a
           where a.attrelid = c.oid and a.attname = 'initial_snapshot_sha256'
       )),
    0,
    'initial_snapshot_sha256 carries no unique constraint'
);

-- ===========================================================================
-- private.legacy_publication_migrations
-- ===========================================================================
select col_is_pk('private', 'legacy_publication_migrations', 'id', 'migrations pk is id');
select col_type_is('private', 'legacy_publication_migrations', 'source_instance_id', 'uuid', 'source_instance_id is uuid');
select col_type_is('private', 'legacy_publication_migrations', 'legacy_publication_id', 'text', 'legacy_publication_id is text');
select col_type_is('private', 'legacy_publication_migrations', 'owner_id', 'uuid', 'owner_id is uuid');
select col_type_is('private', 'legacy_publication_migrations', 'pdf_sha256', 'text', 'pdf_sha256 is text');
select col_type_is('private', 'legacy_publication_migrations', 'pdf_size_bytes', 'bigint', 'pdf_size_bytes is bigint');
select col_type_is('private', 'legacy_publication_migrations', 'migration_mode', 'text', 'migration_mode is text');
select col_type_is('private', 'legacy_publication_migrations', 'migration_state', 'text', 'migration_state is text');
select col_type_is('private', 'legacy_publication_migrations', 'publication_authorization_state', 'text', 'publication_authorization_state is text');
select col_type_is('private', 'legacy_publication_migrations', 'private_metadata', 'jsonb', 'private_metadata is jsonb');
select col_type_is('private', 'legacy_publication_migrations', 'target_work_id', 'uuid', 'target_work_id is uuid');
select col_type_is('private', 'legacy_publication_migrations', 'target_chapter_id', 'uuid', 'target_chapter_id is uuid');
select col_type_is('private', 'legacy_publication_migrations', 'target_chapter_asset_id', 'uuid', 'target_chapter_asset_id is uuid');

select has_column('private', 'legacy_publication_migrations', 'legacy_file_id', 'has legacy_file_id');
select has_column('private', 'legacy_publication_migrations', 'source_job_id', 'has source_job_id');
select has_column('private', 'legacy_publication_migrations', 'source_run_id', 'has source_run_id');
select has_column('private', 'legacy_publication_migrations', 'source_record_digest', 'has source_record_digest');
select has_column('private', 'legacy_publication_migrations', 'source_snapshot_digest', 'has source_snapshot_digest');
select has_column('private', 'legacy_publication_migrations', 'owner_resolution_state', 'has owner_resolution_state');
select has_column('private', 'legacy_publication_migrations', 'owner_verified_at', 'has owner_verified_at');
select has_column('private', 'legacy_publication_migrations', 'requested_target_status', 'has requested_target_status');
select has_column('private', 'legacy_publication_migrations', 'preserve_legacy_publication_state', 'has preserve_legacy_publication_state');
select has_column('private', 'legacy_publication_migrations', 'attempt_count', 'has attempt_count');
select has_column('private', 'legacy_publication_migrations', 'last_error_code', 'has last_error_code');
select has_column('private', 'legacy_publication_migrations', 'created_by', 'has created_by');
select has_column('private', 'legacy_publication_migrations', 'completed_by', 'has completed_by');
select has_column('private', 'legacy_publication_migrations', 'owner_deleted_at', 'has owner_deleted_at (tombstone)');
select has_column('private', 'legacy_publication_migrations', 'source_retired_at', 'has source_retired_at (tombstone)');
select has_column('private', 'legacy_publication_migrations', 'target_removed_at', 'has target_removed_at (tombstone)');
select has_column('private', 'legacy_publication_migrations', 'invalidated_at', 'has invalidated_at (tombstone)');
select has_column('private', 'legacy_publication_migrations', 'invalidated_reason', 'has invalidated_reason (tombstone)');

select col_not_null('private', 'legacy_publication_migrations', 'source_system', 'source_system not null');
select col_not_null('private', 'legacy_publication_migrations', 'source_instance_id', 'source_instance_id not null');
select col_not_null('private', 'legacy_publication_migrations', 'legacy_publication_id', 'legacy_publication_id not null');
select col_not_null('private', 'legacy_publication_migrations', 'source_record_digest', 'source_record_digest not null');
-- owner_id must stay nullable so ON DELETE SET NULL can preserve the row (retention contract).
select ok(
    (select is_nullable = 'YES' from information_schema.columns
     where table_schema = 'private' and table_name = 'legacy_publication_migrations' and column_name = 'owner_id'),
    'owner_id is nullable (retention: SET NULL on user delete)'
);

-- Canonical identity unique --------------------------------------------------
select columns_are('private', 'legacy_publication_migrations', ARRAY[
    'id','source_system','source_instance_id','source_schema_version','legacy_publication_id',
    'legacy_file_id','source_job_id','source_run_id','source_record_digest',
    'owner_id','owner_resolution_state','owner_verified_at',
    'pdf_sha256','pdf_size_bytes','source_storage_provider','source_storage_file_id','source_storage_folder_id',
    'migration_mode','requested_target_status','preserve_legacy_publication_state',
    'migration_state','publication_authorization_state','attempt_count','last_error_code',
    'started_at','completed_at','created_at','updated_at',
    'target_work_id','target_chapter_id','target_chapter_asset_id','source_snapshot_digest',
    'created_by','completed_by','private_metadata',
    'owner_deleted_at','source_retired_at','target_removed_at','invalidated_at','invalidated_reason'
], 'legacy_publication_migrations has exactly the contract columns');

select ok(
    exists (
        select 1 from pg_constraint co
        join pg_class c on c.oid = co.conrelid
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'private' and c.relname = 'legacy_publication_migrations'
          and co.contype = 'u'
          and co.conname = 'legacy_pub_migrations_identity_unique'
    ),
    'canonical identity unique constraint exists'
);

-- FKs -------------------------------------------------------------------------
select fk_ok('private', 'legacy_publication_migrations', 'source_instance_id', 'private', 'legacy_migration_sources', 'source_instance_id');
select fk_ok('private', 'legacy_publication_migrations', 'owner_id', 'auth', 'users', 'id');
select fk_ok('private', 'legacy_publication_migrations', 'target_work_id', 'public', 'works', 'id');
select fk_ok('private', 'legacy_publication_migrations', 'target_chapter_id', 'public', 'chapters', 'id');
select fk_ok('private', 'legacy_publication_migrations', 'target_chapter_asset_id', 'private', 'chapter_assets', 'chapter_id');

-- Trigger validating source_system against the registered source -------------
select has_trigger('private', 'legacy_publication_migrations', 'legacy_pub_migrations_validate_source_system', 'source_system validation trigger exists');

-- ===========================================================================
-- private.legacy_publication_migration_events
-- ===========================================================================
select col_is_pk('private', 'legacy_publication_migration_events', 'id', 'events pk is id');
select col_type_is('private', 'legacy_publication_migration_events', 'migration_id', 'uuid', 'events.migration_id is uuid');
select col_type_is('private', 'legacy_publication_migration_events', 'event_type', 'text', 'events.event_type is text');
select col_type_is('private', 'legacy_publication_migration_events', 'actor_id', 'uuid', 'events.actor_id is uuid');
select col_type_is('private', 'legacy_publication_migration_events', 'metadata', 'jsonb', 'events.metadata is jsonb');
select col_not_null('private', 'legacy_publication_migration_events', 'migration_id', 'events.migration_id not null');
select col_not_null('private', 'legacy_publication_migration_events', 'event_type', 'events.event_type not null');
select has_column('private', 'legacy_publication_migration_events', 'previous_state', 'events has previous_state');
select has_column('private', 'legacy_publication_migration_events', 'next_state', 'events has next_state');
select has_column('private', 'legacy_publication_migration_events', 'error_code', 'events has error_code');

select fk_ok('private', 'legacy_publication_migration_events', 'migration_id', 'private', 'legacy_publication_migrations', 'id');
select fk_ok('private', 'legacy_publication_migration_events', 'actor_id', 'auth', 'users', 'id');

-- ===========================================================================
-- RLS enabled, zero policies on all three tables
-- ===========================================================================
select is(
    (select bool_and(relrowsecurity) from pg_class c
     join pg_namespace n on n.oid = c.relnamespace
     where n.nspname = 'private'
       and c.relname in ('legacy_migration_sources', 'legacy_publication_migrations',
                          'legacy_publication_migration_events')),
    true,
    'RLS enabled on all three legacy provenance tables'
);

select is(
    (select count(*)::int from pg_policies
     where schemaname = 'private'
       and tablename in ('legacy_migration_sources', 'legacy_publication_migrations',
                          'legacy_publication_migration_events')),
    0,
    'zero policies across all three legacy provenance tables'
);

-- ===========================================================================
-- Grants: PUBLIC/anon/authenticated have no privilege on any of the 3 tables
-- ===========================================================================
select is(
    (select count(*)::int from information_schema.role_table_grants
     where table_schema = 'private'
       and table_name in ('legacy_migration_sources', 'legacy_publication_migrations',
                           'legacy_publication_migration_events')
       and grantee in ('PUBLIC', 'anon', 'authenticated')),
    0,
    'no grants to PUBLIC/anon/authenticated on any legacy provenance table'
);

select * from finish();
rollback;
