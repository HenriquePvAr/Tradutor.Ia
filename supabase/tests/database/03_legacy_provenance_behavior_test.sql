-- Behavioral pgTAP tests for the legacy publication provenance schema (Fase 2A1).
-- Run via `supabase test db` (Docker). Everything happens in a rolled-back transaction.
-- Only synthetic UUIDs/hashes/text are used — no real legacy publication id, owner, storage
-- id, or title. No SECURITY DEFINER function exists yet (Fase 2A2): every insert here is a
-- direct INSERT under the local superuser role, exactly as the mission's test plan requires
-- for Fase 2A1 ("INSERT direto de teste, não via função").
begin;
select no_plan();

-- Fixed identities ------------------------------------------------------------
\set source_a       'a1000000-0000-4000-8000-000000000001'
\set source_b       'a1000000-0000-4000-8000-000000000002'
\set source_hc_1    'a1000000-0000-4000-8000-000000000003'
\set source_hc_2    'a1000000-0000-4000-8000-000000000004'
\set manifest_a     'b1000000-0000-4000-8000-000000000001'
\set manifest_b     'b1000000-0000-4000-8000-000000000002'
\set manifest_hc_1  'b1000000-0000-4000-8000-000000000003'
\set manifest_hc_2  'b1000000-0000-4000-8000-000000000004'
\set owner_ret       '33333333-3333-4333-8333-333333333301'
\set target_owner    '33333333-3333-4333-8333-333333333302'

-- ===========================================================================
-- Section 1 — identity: source_instance_id, never a hash, anchors identity.
-- ===========================================================================

-- 1.1 Two sources with the *same* schema version and the *same* snapshot hash still get
-- distinct source_instance_id and both coexist (hash never merges/identifies a source).
insert into private.legacy_migration_sources
    (source_instance_id, source_system, source_schema_version, initial_snapshot_sha256,
     initial_logical_fingerprint, registered_by, manifest_id)
values
    (:'source_hc_1', 'tradutor_ia_sqlite_test_v1', 3, repeat('b', 64), 'fp-collide', 'test-operator', :'manifest_hc_1'),
    (:'source_hc_2', 'tradutor_ia_sqlite_test_v1', 3, repeat('b', 64), 'fp-collide', 'test-operator', :'manifest_hc_2');
select is(
    (select count(*)::int from private.legacy_migration_sources
     where initial_snapshot_sha256 = repeat('b', 64)),
    2,
    '1.1 two sources with an identical snapshot hash both persist as distinct rows');
select isnt(
    (select source_instance_id from private.legacy_migration_sources where source_instance_id = :'source_hc_1'),
    (select source_instance_id from private.legacy_migration_sources where source_instance_id = :'source_hc_2'),
    '1.2 the two colliding-hash sources keep distinct source_instance_id');

-- 1.2 Register the two "real" sources used by the rest of the file.
insert into private.legacy_migration_sources
    (source_instance_id, source_system, source_schema_version, initial_snapshot_sha256,
     initial_logical_fingerprint, registered_by, manifest_id)
values
    (:'source_a', 'tradutor_ia_sqlite_test_v1', 3, repeat('a', 64), 'fp-a', 'test-operator', :'manifest_a'),
    (:'source_b', 'tradutor_ia_sqlite_test_v1', 3, repeat('c', 64), 'fp-b', 'test-operator', :'manifest_b');

-- 1.3 Same legacy_publication_id in two distinct sources: both allowed.
select lives_ok(
    $$insert into private.legacy_publication_migrations
        (source_system, source_instance_id, source_schema_version, legacy_publication_id,
         source_record_digest, owner_resolution_state, migration_mode, requested_target_status,
         migration_state)
      values ('tradutor_ia_sqlite_test_v1', 'a1000000-0000-4000-8000-000000000001', 3,
              'LEGACY-PUB-TEST-01', 'digest-a-1', 'unresolved', 'published_copy', 'community', 'planned')$$,
    '1.3a LEGACY-PUB-TEST-01 reserved under source A');
select lives_ok(
    $$insert into private.legacy_publication_migrations
        (source_system, source_instance_id, source_schema_version, legacy_publication_id,
         source_record_digest, owner_resolution_state, migration_mode, requested_target_status,
         migration_state)
      values ('tradutor_ia_sqlite_test_v1', 'a1000000-0000-4000-8000-000000000002', 3,
              'LEGACY-PUB-TEST-01', 'digest-b-1', 'unresolved', 'published_copy', 'community', 'planned')$$,
    '1.3b the same legacy_publication_id under source B does not collide with source A');

-- 1.4 Same legacy_publication_id twice inside the *same* source: unique violation.
select throws_ok(
    $$insert into private.legacy_publication_migrations
        (source_system, source_instance_id, source_schema_version, legacy_publication_id,
         source_record_digest, owner_resolution_state, migration_mode, requested_target_status,
         migration_state)
      values ('tradutor_ia_sqlite_test_v1', 'a1000000-0000-4000-8000-000000000001', 3,
              'LEGACY-PUB-TEST-01', 'digest-a-1-dup', 'unresolved', 'published_copy', 'community', 'planned')$$,
    '23505',
    NULL,
    '1.4 re-reserving the same (source, publication) pair is a unique violation');

-- 1.5 source_job_id reused inside the same source: blocked. Across sources: allowed.
select lives_ok(
    $$insert into private.legacy_publication_migrations
        (source_system, source_instance_id, source_schema_version, legacy_publication_id,
         source_job_id, source_record_digest, owner_resolution_state, migration_mode,
         requested_target_status, migration_state)
      values ('tradutor_ia_sqlite_test_v1', 'a1000000-0000-4000-8000-000000000001', 3,
              'LEGACY-PUB-TEST-02', 'job-shared-001', 'digest-a-2', 'unresolved', 'published_copy',
              'community', 'planned')$$,
    '1.5a job-shared-001 reserved once under source A');
select throws_ok(
    $$insert into private.legacy_publication_migrations
        (source_system, source_instance_id, source_schema_version, legacy_publication_id,
         source_job_id, source_record_digest, owner_resolution_state, migration_mode,
         requested_target_status, migration_state)
      values ('tradutor_ia_sqlite_test_v1', 'a1000000-0000-4000-8000-000000000001', 3,
              'LEGACY-PUB-TEST-03', 'job-shared-001', 'digest-a-3', 'unresolved', 'published_copy',
              'community', 'planned')$$,
    '23505',
    NULL,
    '1.5b reusing job-shared-001 inside the same source is a unique violation');
select lives_ok(
    $$insert into private.legacy_publication_migrations
        (source_system, source_instance_id, source_schema_version, legacy_publication_id,
         source_job_id, source_record_digest, owner_resolution_state, migration_mode,
         requested_target_status, migration_state)
      values ('tradutor_ia_sqlite_test_v1', 'a1000000-0000-4000-8000-000000000002', 3,
              'LEGACY-PUB-TEST-02', 'job-shared-001', 'digest-b-2', 'unresolved', 'published_copy',
              'community', 'planned')$$,
    '1.5c job-shared-001 reused under a different source is allowed');

-- 1.6 file_sha256 / schema_fingerprint do not determine identity: two sources may share
-- source_schema_version (already proven in 1.1 for the hash) and may even carry the same
-- pdf_sha256 on unrelated publications without merging.
insert into private.legacy_publication_migrations
    (source_system, source_instance_id, source_schema_version, legacy_publication_id,
     pdf_sha256, source_record_digest, owner_resolution_state, migration_mode,
     requested_target_status, migration_state)
values
    ('tradutor_ia_sqlite_test_v1', :'source_a', 3, 'LEGACY-PUB-TEST-04', repeat('d', 64),
     'digest-a-4', 'unresolved', 'published_copy', 'community', 'planned'),
    ('tradutor_ia_sqlite_test_v1', :'source_b', 3, 'LEGACY-PUB-TEST-04', repeat('d', 64),
     'digest-b-4', 'unresolved', 'published_copy', 'community', 'planned');
select is(
    (select count(*)::int from private.legacy_publication_migrations where pdf_sha256 = repeat('d', 64)),
    2,
    '1.6 an identical pdf_sha256 across two sources never merges/collides identity');

-- ===========================================================================
-- Section 2 — source_system authority trigger (Decisão 3).
-- ===========================================================================
select throws_ok(
    $$insert into private.legacy_publication_migrations
        (source_system, source_instance_id, source_schema_version, legacy_publication_id,
         source_record_digest, owner_resolution_state, migration_mode, requested_target_status,
         migration_state)
      values ('wrong_source_system', 'a1000000-0000-4000-8000-000000000001', 3,
              'LEGACY-PUB-TEST-05', 'digest-a-5', 'unresolved', 'published_copy', 'community', 'planned')$$,
    '23514',
    NULL,
    '2.1 source_system diverging from the registered source is rejected by trigger');

-- ===========================================================================
-- Section 3 — FK and hash-format checks.
-- ===========================================================================
select throws_ok(
    $$insert into private.legacy_publication_migrations
        (source_system, source_instance_id, source_schema_version, legacy_publication_id,
         source_record_digest, owner_resolution_state, migration_mode, requested_target_status,
         migration_state)
      values ('tradutor_ia_sqlite_test_v1', '99999999-9999-4999-8999-999999999999', 3,
              'LEGACY-PUB-TEST-06', 'digest-x-6', 'unresolved', 'published_copy', 'community', 'planned')$$,
    '23503',
    NULL,
    '3.1 source_instance_id pointing at a non-existent source is a foreign key violation');

select throws_ok(
    $$insert into private.legacy_migration_sources
        (source_system, source_schema_version, initial_snapshot_sha256, initial_logical_fingerprint,
         registered_by, manifest_id)
      values ('tradutor_ia_sqlite_test_v1', 3, 'not-a-valid-hash', 'fp-bad', 'test-operator',
              'c1000000-0000-4000-8000-000000000001')$$,
    NULL,
    NULL,
    '3.2 a malformed initial_snapshot_sha256 (not 64 hex chars) is rejected');

select throws_ok(
    $$insert into private.legacy_publication_migrations
        (source_system, source_instance_id, source_schema_version, legacy_publication_id,
         pdf_sha256, source_record_digest, owner_resolution_state, migration_mode,
         requested_target_status, migration_state)
      values ('tradutor_ia_sqlite_test_v1', 'a1000000-0000-4000-8000-000000000001', 3,
              'LEGACY-PUB-TEST-07', 'ZZ-not-hex', 'digest-a-7', 'unresolved', 'published_copy',
              'community', 'planned')$$,
    NULL,
    NULL,
    '3.3 a malformed pdf_sha256 is rejected');

select throws_ok(
    $$insert into private.legacy_migration_sources
        (source_system, source_schema_version, initial_snapshot_sha256, initial_logical_fingerprint,
         registered_by, manifest_id, private_metadata)
      values ('tradutor_ia_sqlite_test_v1', 3, repeat('e', 64), 'fp-meta', 'test-operator',
              'c1000000-0000-4000-8000-000000000002', '["not", "an", "object"]'::jsonb)$$,
    NULL,
    NULL,
    '3.4 private_metadata must be a JSON object, an array is rejected');

-- ===========================================================================
-- Section 4 — PUB-0001 semantics (published_copy).
-- ===========================================================================

-- Real public targets to attach.
insert into auth.users (id, instance_id, aud, role, email, encrypted_password,
                        email_confirmed_at, created_at, updated_at,
                        raw_app_meta_data, raw_user_meta_data)
values (:'target_owner', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
        'target-owner@test.local', '', now(), now(), now(), '{}'::jsonb, '{}'::jsonb);
insert into public.works (id, owner_id, title, slug, status)
values ('44444444-4444-4444-8444-444444444401', :'target_owner', 'Provenance Test Work', 'prov-test-work', 'draft');
-- Three distinct chapters: each successfully-persisted target-bearing row below claims its
-- own chapter, since legacy_pub_migrations_target_chapter_unique allows only one live
-- provenance row per target chapter.
insert into public.chapters (id, work_id, chapter_number, status)
values
    ('55555555-5555-4555-8555-555555555501', '44444444-4444-4444-8444-444444444401', 1, 'draft'),
    ('55555555-5555-4555-8555-555555555502', '44444444-4444-4444-8444-444444444401', 2, 'draft'),
    ('55555555-5555-4555-8555-555555555503', '44444444-4444-4444-8444-444444444401', 3, 'draft'),
    ('55555555-5555-4555-8555-555555555504', '44444444-4444-4444-8444-444444444401', 4, 'draft');
insert into private.chapter_assets (chapter_id, storage_file_id)
values
    ('55555555-5555-4555-8555-555555555501', 'drive-file-test-asset-1'),
    ('55555555-5555-4555-8555-555555555503', 'drive-file-test-asset-3');

-- 4.1 published_copy cannot reach `completed` without a linked asset.
select throws_ok(
    $$insert into private.legacy_publication_migrations
        (source_system, source_instance_id, source_schema_version, legacy_publication_id,
         source_record_digest, owner_resolution_state, migration_mode, requested_target_status,
         migration_state, target_work_id, target_chapter_id, completed_at)
      values ('tradutor_ia_sqlite_test_v1', 'a1000000-0000-4000-8000-000000000001', 3,
              'LEGACY-PUB-0001-TEST', 'digest-pub0001', 'resolved_locally_by_uuid_match',
              'published_copy', 'community', 'completed',
              '44444444-4444-4444-8444-444444444401', '55555555-5555-4555-8555-555555555501', now())$$,
    NULL,
    NULL,
    '4.1 published_copy at completed without target_chapter_asset_id is rejected');

-- 4.2 published_copy fully populated (work+chapter+asset+completed_at) succeeds.
select lives_ok(
    $$insert into private.legacy_publication_migrations
        (source_system, source_instance_id, source_schema_version, legacy_publication_id,
         source_record_digest, owner_resolution_state, migration_mode, requested_target_status,
         migration_state, target_work_id, target_chapter_id, target_chapter_asset_id, completed_at)
      values ('tradutor_ia_sqlite_test_v1', 'a1000000-0000-4000-8000-000000000001', 3,
              'LEGACY-PUB-0001-TEST', 'digest-pub0001', 'resolved_locally_by_uuid_match',
              'published_copy', 'community', 'completed',
              '44444444-4444-4444-8444-444444444401', '55555555-5555-4555-8555-555555555501',
              '55555555-5555-4555-8555-555555555501', now())$$,
    '4.2 published_copy at completed with work+chapter+asset+completed_at succeeds');

-- 4.3 published_copy at chapter_created without target_chapter_id is rejected.
select throws_ok(
    $$insert into private.legacy_publication_migrations
        (source_system, source_instance_id, source_schema_version, legacy_publication_id,
         source_record_digest, owner_resolution_state, migration_mode, requested_target_status,
         migration_state, target_work_id)
      values ('tradutor_ia_sqlite_test_v1', 'a1000000-0000-4000-8000-000000000001', 3,
              'LEGACY-PUB-0001-TEST-B', 'digest-pub0001b', 'resolved_locally_by_uuid_match',
              'published_copy', 'community', 'chapter_created',
              '44444444-4444-4444-8444-444444444401')$$,
    NULL,
    NULL,
    '4.3 published_copy at chapter_created without target_chapter_id is rejected');

-- ===========================================================================
-- Section 5 — PUB-0002 semantics (draft_recovery).
-- ===========================================================================

-- 5.1 draft_recovery with requested_target_status = 'draft', no asset yet, reaching
-- recovery_completed: allowed (asset absence is not an error for draft_recovery).
select lives_ok(
    $$insert into private.legacy_publication_migrations
        (source_system, source_instance_id, source_schema_version, legacy_publication_id,
         source_record_digest, owner_resolution_state, migration_mode, requested_target_status,
         migration_state, target_work_id, target_chapter_id, completed_at)
      values ('tradutor_ia_sqlite_test_v1', 'a1000000-0000-4000-8000-000000000002', 3,
              'LEGACY-PUB-0002-TEST', 'digest-pub0002', 'unresolved', 'draft_recovery', 'draft',
              'recovery_completed', '44444444-4444-4444-8444-444444444401',
              '55555555-5555-4555-8555-555555555502', now())$$,
    '5.1 draft_recovery reaching recovery_completed without an asset succeeds');

-- 5.2 draft_recovery cannot request community status without a published authorization.
select throws_ok(
    $$insert into private.legacy_publication_migrations
        (source_system, source_instance_id, source_schema_version, legacy_publication_id,
         source_record_digest, owner_resolution_state, migration_mode, requested_target_status,
         migration_state)
      values ('tradutor_ia_sqlite_test_v1', 'a1000000-0000-4000-8000-000000000002', 3,
              'LEGACY-PUB-0002-TEST-B', 'digest-pub0002b', 'unresolved', 'draft_recovery', 'community',
              'planned')$$,
    NULL,
    NULL,
    '5.2 draft_recovery requesting community status without publication_authorization_state=published is rejected');

-- 5.3 publication_authorized state requires publication_authorization_state = authorized.
select throws_ok(
    $$insert into private.legacy_publication_migrations
        (source_system, source_instance_id, source_schema_version, legacy_publication_id,
         source_record_digest, owner_resolution_state, migration_mode, requested_target_status,
         migration_state, target_work_id, target_chapter_id)
      values ('tradutor_ia_sqlite_test_v1', 'a1000000-0000-4000-8000-000000000002', 3,
              'LEGACY-PUB-0002-TEST-C', 'digest-pub0002c', 'unresolved', 'draft_recovery', 'draft',
              'publication_authorized', '44444444-4444-4444-8444-444444444401',
              '55555555-5555-4555-8555-555555555501')$$,
    NULL,
    NULL,
    '5.3 migration_state=publication_authorized without publication_authorization_state=authorized is rejected');

-- 5.4 recovery_completed can only happen under migration_mode = draft_recovery.
select throws_ok(
    $$insert into private.legacy_publication_migrations
        (source_system, source_instance_id, source_schema_version, legacy_publication_id,
         source_record_digest, owner_resolution_state, migration_mode, requested_target_status,
         migration_state, target_work_id, target_chapter_id, completed_at)
      values ('tradutor_ia_sqlite_test_v1', 'a1000000-0000-4000-8000-000000000002', 3,
              'LEGACY-PUB-0002-TEST-D', 'digest-pub0002d', 'unresolved', 'published_copy', 'community',
              'recovery_completed', '44444444-4444-4444-8444-444444444401',
              '55555555-5555-4555-8555-555555555501', now())$$,
    NULL,
    NULL,
    '5.4 recovery_completed under migration_mode=published_copy is rejected');

-- 5.5 draft_recovery reaching completed without publication_authorization_state=published
-- is rejected (no self-promotion via the migrator's normal completion path — first
-- structural layer; the second layer, an administrative authorization function, is Fase 2A2).
select throws_ok(
    $$insert into private.legacy_publication_migrations
        (source_system, source_instance_id, source_schema_version, legacy_publication_id,
         source_record_digest, owner_resolution_state, migration_mode, requested_target_status,
         migration_state, target_work_id, target_chapter_id, completed_at)
      values ('tradutor_ia_sqlite_test_v1', 'a1000000-0000-4000-8000-000000000002', 3,
              'LEGACY-PUB-0002-TEST-E', 'digest-pub0002e', 'unresolved', 'draft_recovery', 'draft',
              'completed', '44444444-4444-4444-8444-444444444401',
              '55555555-5555-4555-8555-555555555501', now())$$,
    NULL,
    NULL,
    '5.5 draft_recovery at completed without publication_authorization_state=published is rejected');

-- 5.6 the fully-authorized promotion combination (as it would exist once Fase 2A2's
-- authorize_draft_recovery_publication + promotion function have both run) is structurally
-- representable — proving the schema does not block the legitimate end state, only the
-- unauthorized shortcut.
select lives_ok(
    $$insert into private.legacy_publication_migrations
        (source_system, source_instance_id, source_schema_version, legacy_publication_id,
         source_record_digest, owner_resolution_state, migration_mode, requested_target_status,
         migration_state, publication_authorization_state,
         target_work_id, target_chapter_id, target_chapter_asset_id, completed_at)
      values ('tradutor_ia_sqlite_test_v1', 'a1000000-0000-4000-8000-000000000002', 3,
              'LEGACY-PUB-0002-TEST-F', 'digest-pub0002f', 'unresolved', 'draft_recovery', 'community',
              'completed', 'published',
              '44444444-4444-4444-8444-444444444401', '55555555-5555-4555-8555-555555555503',
              '55555555-5555-4555-8555-555555555503', now())$$,
    '5.6 draft_recovery reaching completed+community with publication_authorization_state=published succeeds');

-- ===========================================================================
-- Section 6 — retention: exclusion never deletes provenance/events (Decisão 9).
-- ===========================================================================

-- 6.1 Owner deleted: the migration row and its event survive; owner_id/actor_id go NULL.
insert into auth.users (id, instance_id, aud, role, email, encrypted_password,
                        email_confirmed_at, created_at, updated_at,
                        raw_app_meta_data, raw_user_meta_data)
values (:'owner_ret', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
        'owner-ret@test.local', '', now(), now(), now(), '{}'::jsonb, '{}'::jsonb);

insert into private.legacy_publication_migrations
    (id, source_system, source_instance_id, source_schema_version, legacy_publication_id,
     source_record_digest, owner_resolution_state, owner_id, migration_mode,
     requested_target_status, migration_state)
values ('66666666-6666-4666-8666-666666666601', 'tradutor_ia_sqlite_test_v1',
        :'source_a', 3, 'LEGACY-PUB-RET-01', 'digest-ret-01', 'resolved_locally_by_uuid_match',
        :'owner_ret', 'published_copy', 'community', 'planned');

insert into private.legacy_publication_migration_events
    (id, migration_id, event_type, actor_id, metadata)
values ('77777777-7777-4777-8777-777777777701', '66666666-6666-4666-8666-666666666601',
        'migration_planned', :'owner_ret', '{}'::jsonb);

delete from auth.users where id = :'owner_ret';

select is(
    (select count(*)::int from private.legacy_publication_migrations
     where id = '66666666-6666-4666-8666-666666666601'),
    1,
    '6.1a migration row survives owner deletion (not cascaded)');
select is(
    (select owner_id from private.legacy_publication_migrations
     where id = '66666666-6666-4666-8666-666666666601'),
    NULL,
    '6.1b owner_id is NULL after the owning auth.users row is deleted');
select is(
    (select count(*)::int from private.legacy_publication_migration_events
     where id = '77777777-7777-4777-8777-777777777701'),
    1,
    '6.1c event row survives owner/actor deletion (not cascaded)');
select is(
    (select actor_id from private.legacy_publication_migration_events
     where id = '77777777-7777-4777-8777-777777777701'),
    NULL,
    '6.1d actor_id is NULL after the acting auth.users row is deleted');

-- 6.2 Target removed: target_chapter_id/target_work_id go NULL, row survives (no re-migration
-- side effect). Uses migration_state = 'planned', where a target is not yet required, so the
-- FK's ON DELETE SET NULL update does not collide with the "target required from X onward"
-- checks (those checks correctly refuse to let a required target silently vanish).
insert into private.legacy_publication_migrations
    (id, source_system, source_instance_id, source_schema_version, legacy_publication_id,
     source_record_digest, owner_resolution_state, migration_mode, requested_target_status,
     migration_state, target_work_id, target_chapter_id)
values ('66666666-6666-4666-8666-666666666602', 'tradutor_ia_sqlite_test_v1',
        :'source_a', 3, 'LEGACY-PUB-RET-02', 'digest-ret-02', 'unresolved', 'published_copy',
        'community', 'planned', '44444444-4444-4444-8444-444444444401',
        '55555555-5555-4555-8555-555555555504');

delete from public.chapters where id = '55555555-5555-4555-8555-555555555504';

select is(
    (select count(*)::int from private.legacy_publication_migrations
     where id = '66666666-6666-4666-8666-666666666602'),
    1,
    '6.2a migration row survives target chapter deletion (not cascaded)');
select is(
    (select target_chapter_id from private.legacy_publication_migrations
     where id = '66666666-6666-4666-8666-666666666602'),
    NULL,
    '6.2b target_chapter_id is NULL after the target chapter is deleted');

-- 6.3 A source with a migration attached cannot be silently deleted (ON DELETE RESTRICT).
select throws_ok(
    $$delete from private.legacy_migration_sources
      where source_instance_id = 'a1000000-0000-4000-8000-000000000001'$$,
    '23503',
    NULL,
    '6.3 deleting a source that still has migrations attached is a foreign key violation');

-- 6.4 A migration with events attached cannot be silently deleted (ON DELETE RESTRICT).
select throws_ok(
    $$delete from private.legacy_publication_migrations
      where id = '66666666-6666-4666-8666-666666666601'$$,
    '23503',
    NULL,
    '6.4 deleting a migration that still has events attached is a foreign key violation');

-- ===========================================================================
-- Section 7 — events table: validity, append-only via REVOKE, no client policy.
-- ===========================================================================

-- 7.1 An event needs a real migration_id.
select throws_ok(
    $$insert into private.legacy_publication_migration_events (migration_id, event_type)
      values ('99999999-9999-4999-8999-999999999998', 'migration_planned')$$,
    '23503',
    NULL,
    '7.1 an event referencing a non-existent migration_id is a foreign key violation');

-- 7.2 metadata must be a JSON object.
select throws_ok(
    $$insert into private.legacy_publication_migration_events (migration_id, event_type, metadata)
      values ('66666666-6666-4666-8666-666666666602', 'migration_planned', '"just a string"'::jsonb)$$,
    NULL,
    NULL,
    '7.2 event metadata must be a JSON object, a bare string is rejected');

-- 7.3 A valid event insert succeeds.
select lives_ok(
    $$insert into private.legacy_publication_migration_events (migration_id, event_type, metadata)
      values ('66666666-6666-4666-8666-666666666602', 'migration_planned', '{"note": "synthetic"}'::jsonb)$$,
    '7.3 a well-formed event insert succeeds');

-- 7.4 anon/authenticated hold no privilege at all on any of the three tables (no GRANT
-- means the attempt fails outright, before RLS is even evaluated — same fail-closed pattern
-- as private.chapter_assets).
set local role anon;
set local request.jwt.claims = '';
select throws_ok(
    $$select 1 from private.legacy_migration_sources$$, '42501', NULL,
    '7.5 anon cannot select legacy_migration_sources');
select throws_ok(
    $$select 1 from private.legacy_publication_migrations$$, '42501', NULL,
    '7.6 anon cannot select legacy_publication_migrations');
select throws_ok(
    $$select 1 from private.legacy_publication_migration_events$$, '42501', NULL,
    '7.7 anon cannot select legacy_publication_migration_events');
select throws_ok(
    $$insert into private.legacy_publication_migration_events (migration_id, event_type)
      values ('66666666-6666-4666-8666-666666666602', 'migration_planned')$$, '42501', NULL,
    '7.8 anon cannot insert into legacy_publication_migration_events');
reset role;

set local role authenticated;
set local request.jwt.claims = '{"sub":"33333333-3333-4333-8333-333333333302","role":"authenticated"}';
select throws_ok(
    $$select 1 from private.legacy_migration_sources$$, '42501', NULL,
    '7.9 authenticated cannot select legacy_migration_sources');
select throws_ok(
    $$select 1 from private.legacy_publication_migrations$$, '42501', NULL,
    '7.10 authenticated cannot select legacy_publication_migrations');
select throws_ok(
    $$select 1 from private.legacy_publication_migration_events$$, '42501', NULL,
    '7.11 authenticated cannot select legacy_publication_migration_events');
select throws_ok(
    $$insert into private.legacy_publication_migration_events (migration_id, event_type)
      values ('66666666-6666-4666-8666-666666666602', 'migration_planned')$$, '42501', NULL,
    '7.12 authenticated cannot insert into legacy_publication_migration_events');
select throws_ok(
    $$update private.legacy_publication_migration_events set event_type = 'migration_planned'
      where id = '77777777-7777-4777-8777-777777777701'$$, '42501', NULL,
    '7.13 authenticated cannot update legacy_publication_migration_events');
select throws_ok(
    $$delete from private.legacy_publication_migration_events
      where id = '77777777-7777-4777-8777-777777777701'$$, '42501', NULL,
    '7.14 authenticated cannot delete from legacy_publication_migration_events');
reset role;

-- 7.15 UPDATE/DELETE on events are revoked even from the unprivileged PUBLIC role baseline
-- (structural confirmation, already covered by the grants catalog check in
-- 02_legacy_provenance_structure_test.sql; repeated here as a live attempt per the mission's
-- "catálogos E tentativa real" requirement).
select is(
    (select count(*)::int from information_schema.role_table_grants
     where table_schema = 'private' and table_name = 'legacy_publication_migration_events'
       and privilege_type in ('UPDATE', 'DELETE')
       and grantee in ('PUBLIC', 'anon', 'authenticated')),
    0,
    '7.15 no UPDATE/DELETE grant to PUBLIC/anon/authenticated on legacy_publication_migration_events '
    '(the table owner keeps implicit privileges, as with any Postgres table; that is not a client role)');

select * from finish();
rollback;
