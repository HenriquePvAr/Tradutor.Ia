-- Structural pgTAP tests for the legacy publication migration runtime (Fase 2A2).
-- Run via `supabase test db` (Docker). Everything happens in a rolled-back transaction.
--
-- Scope: migration_operator / migration_publication_admin roles, the eight migrator
-- SECURITY DEFINER functions, the separate authorize_draft_recovery_publication function,
-- current_admin_actor_id(), and the unconditional append-only triggers. Purely structural —
-- existence, SECURITY DEFINER, search_path, EXECUTE grants. Functional behavior is
-- 05_legacy_provenance_runtime_behavior_test.sql.
begin;
select no_plan();

-- ===========================================================================
-- Roles
-- ===========================================================================
select has_role('migration_operator', 'migration_operator role exists');
select has_role('migration_publication_admin', 'migration_publication_admin role exists');

select is(
    (select rolinherit from pg_roles where rolname = 'migration_operator'),
    false, 'migration_operator is NOINHERIT');
select is(
    (select rolcanlogin from pg_roles where rolname = 'migration_operator'),
    false, 'migration_operator is NOLOGIN');
select is(
    (select rolinherit from pg_roles where rolname = 'migration_publication_admin'),
    false, 'migration_publication_admin is NOINHERIT');
select is(
    (select rolcanlogin from pg_roles where rolname = 'migration_publication_admin'),
    false, 'migration_publication_admin is NOLOGIN');

-- Neither role is a member of anon/authenticated (no inheritance path to client roles).
select is(
    (select count(*)::int from pg_auth_members m
     join pg_roles r on r.oid = m.roleid
     join pg_roles mem on mem.oid = m.member
     where mem.rolname in ('migration_operator', 'migration_publication_admin')
       and r.rolname in ('anon', 'authenticated')),
    0, 'neither role is a member of anon or authenticated');

-- Neither role received schema-wide table privilege.
select is(
    (select count(*)::int from information_schema.role_table_grants
     where table_schema in ('private', 'public')
       and grantee in ('migration_operator', 'migration_publication_admin')),
    0, 'neither role holds any direct table grant in private or public');

-- ===========================================================================
-- Functions exist
-- ===========================================================================
select has_function('private', 'register_legacy_source', ARRAY['jsonb'], 'register_legacy_source exists');
select has_function('private', 'claim_legacy_publication', ARRAY[
    'uuid','text','integer','text','text','text','uuid','text','text','text','text','text',
    'bigint','text','text','text','text','jsonb'
], 'claim_legacy_publication exists');
select has_function('private', 'attach_migration_work', ARRAY['uuid','uuid'], 'attach_migration_work exists');
select has_function('private', 'attach_migration_chapter', ARRAY['uuid','uuid'], 'attach_migration_chapter exists');
select has_function('private', 'attach_migration_asset', ARRAY['uuid','uuid','text','bigint','text','text'], 'attach_migration_asset exists');
select has_function('private', 'mark_migration_failure', ARRAY['uuid','text','boolean'], 'mark_migration_failure exists');
select has_function('private', 'complete_legacy_migration', ARRAY['uuid','text'], 'complete_legacy_migration exists');
select has_function('private', 'append_legacy_migration_event', ARRAY['uuid','text','jsonb','text'], 'append_legacy_migration_event exists');
select has_function('private', 'authorize_draft_recovery_publication', ARRAY['uuid','uuid','text','uuid','text','text'], 'authorize_draft_recovery_publication exists');
select has_function('private', 'current_admin_actor_id', ARRAY[]::text[], 'current_admin_actor_id exists');

-- ===========================================================================
-- SECURITY DEFINER + fixed search_path for every migrator/admin function
-- (current_admin_actor_id is intentionally SECURITY INVOKER — it only reads session
-- context, never writes, so definer rights would be an unnecessary privilege escalation).
-- ===========================================================================
select is_definer('private', 'register_legacy_source', ARRAY['jsonb'], 'register_legacy_source is SECURITY DEFINER');
select is_definer('private', 'claim_legacy_publication', ARRAY[
    'uuid','text','integer','text','text','text','uuid','text','text','text','text','text',
    'bigint','text','text','text','text','jsonb'
], 'claim_legacy_publication is SECURITY DEFINER');
select is_definer('private', 'attach_migration_work', ARRAY['uuid','uuid'], 'attach_migration_work is SECURITY DEFINER');
select is_definer('private', 'attach_migration_chapter', ARRAY['uuid','uuid'], 'attach_migration_chapter is SECURITY DEFINER');
select is_definer('private', 'attach_migration_asset', ARRAY['uuid','uuid','text','bigint','text','text'], 'attach_migration_asset is SECURITY DEFINER');
select is_definer('private', 'mark_migration_failure', ARRAY['uuid','text','boolean'], 'mark_migration_failure is SECURITY DEFINER');
select is_definer('private', 'complete_legacy_migration', ARRAY['uuid','text'], 'complete_legacy_migration is SECURITY DEFINER');
select is_definer('private', 'append_legacy_migration_event', ARRAY['uuid','text','jsonb','text'], 'append_legacy_migration_event is SECURITY DEFINER');
select is_definer('private', 'authorize_draft_recovery_publication', ARRAY['uuid','uuid','text','uuid','text','text'], 'authorize_draft_recovery_publication is SECURITY DEFINER');

select is(
    (select count(*)::int from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = 'private' and p.proname in (
         'register_legacy_source','claim_legacy_publication','attach_migration_work',
         'attach_migration_chapter','attach_migration_asset','mark_migration_failure',
         'complete_legacy_migration','append_legacy_migration_event',
         'authorize_draft_recovery_publication'
     )
     and (p.proconfig is null or not exists (
         select 1 from unnest(p.proconfig) cfg where cfg = 'search_path=""'
     ))),
    0,
    'every 2A2 SECURITY DEFINER function has search_path explicitly set to empty');

-- ===========================================================================
-- EXECUTE grants — PUBLIC always revoked; each function granted to exactly one role.
-- ===========================================================================
select is(
    (select count(*)::int from information_schema.role_routine_grants
     where routine_schema = 'private'
       and routine_name in (
           'register_legacy_source','claim_legacy_publication','attach_migration_work',
           'attach_migration_chapter','attach_migration_asset','mark_migration_failure',
           'complete_legacy_migration','append_legacy_migration_event',
           'authorize_draft_recovery_publication'
       )
       and grantee = 'PUBLIC'),
    0,
    'PUBLIC has no EXECUTE on any 2A2 function');

select is(
    (select count(*)::int from information_schema.role_routine_grants
     where routine_schema = 'private'
       and routine_name in (
           'register_legacy_source','claim_legacy_publication','attach_migration_work',
           'attach_migration_chapter','attach_migration_asset','mark_migration_failure',
           'complete_legacy_migration','append_legacy_migration_event'
       )
       and grantee = 'migration_operator'),
    8,
    'migration_operator has EXECUTE on exactly the eight migrator functions');

select is(
    (select count(*)::int from information_schema.role_routine_grants
     where routine_schema = 'private'
       and routine_name = 'authorize_draft_recovery_publication'
       and grantee = 'migration_publication_admin'),
    1,
    'migration_publication_admin has EXECUTE on authorize_draft_recovery_publication');

select is(
    (select count(*)::int from information_schema.role_routine_grants
     where routine_schema = 'private'
       and routine_name = 'authorize_draft_recovery_publication'
       and grantee = 'migration_operator'),
    0,
    'migration_operator does NOT have EXECUTE on authorize_draft_recovery_publication');

select is(
    (select count(*)::int from information_schema.role_routine_grants
     where routine_schema = 'private'
       and routine_name in (
           'register_legacy_source','claim_legacy_publication','attach_migration_work',
           'attach_migration_chapter','attach_migration_asset','mark_migration_failure',
           'complete_legacy_migration','append_legacy_migration_event'
       )
       and grantee = 'migration_publication_admin'),
    0,
    'migration_publication_admin does NOT have EXECUTE on any migrator function');

select is(
    (select count(*)::int from information_schema.role_routine_grants
     where routine_schema = 'private'
       and routine_name in (
           'register_legacy_source','claim_legacy_publication','attach_migration_work',
           'attach_migration_chapter','attach_migration_asset','mark_migration_failure',
           'complete_legacy_migration','append_legacy_migration_event',
           'authorize_draft_recovery_publication'
       )
       and grantee in ('anon', 'authenticated')),
    0,
    'anon/authenticated have no EXECUTE on any 2A2 function');

-- ===========================================================================
-- Append-only enforcement — triggers exist and reject UPDATE/DELETE unconditionally.
-- ===========================================================================
select has_trigger('private', 'legacy_publication_migration_events', 'legacy_pub_migration_events_no_update', 'append-only UPDATE guard trigger exists');
select has_trigger('private', 'legacy_publication_migration_events', 'legacy_pub_migration_events_no_delete', 'append-only DELETE guard trigger exists');

select * from finish();
rollback;
