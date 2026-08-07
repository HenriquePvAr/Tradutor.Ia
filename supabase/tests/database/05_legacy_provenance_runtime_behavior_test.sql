-- Behavioral pgTAP tests for the legacy publication migration runtime (Fase 2A2).
-- Run via `supabase test db` (Docker). Everything happens in a rolled-back transaction.
-- Only synthetic identifiers/hashes are used (prefixed synthetic-). No real LEGACY-PUB-0001/
-- 0002, no real owner, no real Google Drive id. True cross-connection concurrency (two actual
-- simultaneous backends) is covered separately by a shell script outside pgTAP (pgTAP runs
-- inside a single transaction, which cannot itself hold two overlapping sessions); this file
-- covers everything that is observable from repeated, sequential calls plus role-scoped
-- privilege attempts.
begin;
select no_plan();

-- Test-harness-only, rolled back with everything else at the end of this file: migration_operator/
-- migration_publication_admin correctly have zero USAGE on schema public (the migration itself
-- never grants it — confirmed structurally in 04). Without it, this file cannot call pgTAP's own
-- assertion functions (throws_ok/lives_ok, which live in public) *while impersonated* via
-- `set local role`, which this file needs to prove real access-denial attempts (not just catalog
-- inspection). This grant exists only inside this test transaction and is never part of the
-- shipped migration.
grant usage on schema public to migration_operator, migration_publication_admin;
do $$
begin
    if exists (select 1 from pg_namespace where nspname = 'extensions') then
        execute 'grant usage on schema extensions to migration_operator, migration_publication_admin';
    end if;
end
$$;

-- ===========================================================================
-- Fixtures: one public owner + work + four chapters + two chapter_assets, one registered
-- legacy source used by most of the file.
-- ===========================================================================
\set owner_x        '11111111-1111-4111-8111-111111111101'
\set admin_x         '11111111-1111-4111-8111-111111111102'
\set other_owner     '11111111-1111-4111-8111-111111111103'
\set work_x          '22222222-2222-4222-8222-222222222201'
\set other_work      '22222222-2222-4222-8222-222222222202'
\set chapter_1       '33333333-3333-4333-8333-333333333301'
\set chapter_2       '33333333-3333-4333-8333-333333333302'
\set chapter_3       '33333333-3333-4333-8333-333333333303'
\set chapter_4       '33333333-3333-4333-8333-333333333304'
\set chapter_5       '33333333-3333-4333-8333-333333333306'
\set chapter_6       '33333333-3333-4333-8333-333333333307'
\set chapter_other   '33333333-3333-4333-8333-333333333305'
\set source_r1       'a1000000-0000-4000-8000-000000000101'
\set manifest_r1     'b1000000-0000-4000-8000-000000000101'

insert into auth.users (id, instance_id, aud, role, email, encrypted_password,
                        email_confirmed_at, created_at, updated_at,
                        raw_app_meta_data, raw_user_meta_data)
values
    (:'owner_x', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
     'owner-x@test.local', '', now(), now(), now(), '{}'::jsonb, '{}'::jsonb),
    (:'admin_x', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
     'admin-x@test.local', '', now(), now(), now(), '{}'::jsonb, '{}'::jsonb),
    (:'other_owner', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
     'other-owner@test.local', '', now(), now(), now(), '{}'::jsonb, '{}'::jsonb);

insert into public.works (id, owner_id, title, slug, status)
values
    (:'work_x', :'owner_x', 'Runtime Test Work', 'runtime-test-work', 'draft'),
    (:'other_work', :'other_owner', 'Other Owner Work', 'other-owner-work', 'draft');

insert into public.chapters (id, work_id, chapter_number, status)
values
    (:'chapter_1', :'work_x', 1, 'draft'),
    (:'chapter_2', :'work_x', 2, 'draft'),
    (:'chapter_3', :'work_x', 3, 'draft'),
    (:'chapter_4', :'work_x', 4, 'draft'),
    (:'chapter_5', :'work_x', 5, 'draft'),
    (:'chapter_6', :'work_x', 6, 'draft'),
    (:'chapter_other', :'other_work', 1, 'draft');

insert into private.chapter_assets (chapter_id, storage_provider, storage_file_id, checksum_sha256, byte_size)
values
    (:'chapter_1', 'google_drive', 'synthetic-drive-file-1', repeat('1', 64), 1000),
    (:'chapter_2', 'google_drive', 'synthetic-drive-file-2', repeat('2', 64), 2000),
    (:'chapter_3', 'google_drive', 'synthetic-drive-file-3', repeat('3', 64), 3000),
    (:'chapter_5', 'google_drive', 'synthetic-drive-file-5', repeat('5', 64), 5000),
    (:'chapter_6', 'google_drive', 'synthetic-drive-file-6', repeat('7', 64), 7000);

-- ===========================================================================
-- Section 1 — register_legacy_source
-- ===========================================================================
select lives_ok(
    format($$select private.register_legacy_source('{
        "manifest_id": "%s", "manifest_version": 1,
        "source_system": "tradutor_ia_sqlite_runtime_v1",
        "proposed_source_instance_id": "%s", "source_schema_version": 3,
        "snapshot_sha256": "%s", "logical_fingerprint": "fp-r1",
        "source_reference": "synthetic-batch-1", "capture_timestamp": "2026-08-06T00:00:00Z",
        "publication_count": 1,
        "source_record_digests": [{"legacy_publication_id": "SYN-PUB-01", "digest": "d1"}],
        "approval_state": "approved", "approved_by": "synthetic-operator", "approved_at": "2026-08-06T00:00:00Z"
    }'::jsonb)$$, :'manifest_r1', :'source_r1', repeat('a', 64)),
    '1.1 register_legacy_source with a valid approved manifest succeeds');

select is(
    (select source_state from private.legacy_migration_sources where source_instance_id = :'source_r1'),
    'registered', '1.2 newly registered source starts in state registered');

-- 1.3 Idempotent replay of the exact same manifest_id returns the same identity, no new row.
select is(
    (select private.register_legacy_source(format('{
        "manifest_id": "%s", "manifest_version": 1,
        "source_system": "tradutor_ia_sqlite_runtime_v1",
        "proposed_source_instance_id": "%s", "source_schema_version": 3,
        "snapshot_sha256": "%s", "logical_fingerprint": "fp-r1",
        "source_reference": "synthetic-batch-1", "capture_timestamp": "2026-08-06T00:00:00Z",
        "publication_count": 1,
        "source_record_digests": [{"legacy_publication_id": "SYN-PUB-01", "digest": "d1"}],
        "approval_state": "approved", "approved_by": "synthetic-operator", "approved_at": "2026-08-06T00:00:00Z"
    }', :'manifest_r1', :'source_r1', repeat('a', 64))::jsonb)),
    :'source_r1'::uuid,
    '1.3 replaying the exact same manifest_id is idempotent and returns the same source_instance_id');
select is(
    (select count(*)::int from private.legacy_migration_sources where source_instance_id = :'source_r1'),
    1, '1.4 idempotent replay creates no second row');

-- 1.5 Legitimate evolution: same proposed_source_instance_id, a NEW manifest_id (re-capture),
-- a DIFFERENT physical hash, but a compatible (identical) logical_fingerprint — reuses the
-- existing identity, never mints a new source_instance_id.
select is(
    (select private.register_legacy_source(format('{
        "manifest_id": "b1000000-0000-4000-8000-000000000102", "manifest_version": 1,
        "source_system": "tradutor_ia_sqlite_runtime_v1",
        "proposed_source_instance_id": "%s", "source_schema_version": 3,
        "snapshot_sha256": "%s", "logical_fingerprint": "fp-r1",
        "source_reference": "synthetic-batch-2", "capture_timestamp": "2026-08-07T00:00:00Z",
        "publication_count": 1,
        "source_record_digests": [{"legacy_publication_id": "SYN-PUB-01", "digest": "d1"}],
        "approval_state": "approved", "approved_by": "synthetic-operator", "approved_at": "2026-08-07T00:00:00Z"
    }', :'source_r1', repeat('f', 64))::jsonb)),
    :'source_r1'::uuid,
    '1.5 a later re-capture with a different physical hash but compatible logical_fingerprint reuses source_instance_id');
select is(
    (select count(*)::int from private.legacy_migration_sources),
    1, '1.6 legitimate evolution never creates a second legacy_migration_sources row');
select is(
    (select initial_snapshot_sha256 from private.legacy_migration_sources where source_instance_id = :'source_r1'),
    repeat('a', 64),
    '1.7 the original approved manifest snapshot hash is never edited in place');

-- 1.8 Incompatible logical_fingerprint on the same proposed_source_instance_id: blocked,
-- categorical rejection, no automatic new source.
select throws_ok(
    format($$select private.register_legacy_source('{
        "manifest_id": "b1000000-0000-4000-8000-000000000103", "manifest_version": 1,
        "source_system": "tradutor_ia_sqlite_runtime_v1",
        "proposed_source_instance_id": "%s", "source_schema_version": 3,
        "snapshot_sha256": "%s", "logical_fingerprint": "fp-DIFFERENT-content",
        "source_reference": "synthetic-batch-3", "capture_timestamp": "2026-08-08T00:00:00Z",
        "publication_count": 1,
        "source_record_digests": [{"legacy_publication_id": "SYN-PUB-01", "digest": "d1"}],
        "approval_state": "approved", "approved_by": "synthetic-operator", "approved_at": "2026-08-08T00:00:00Z"
    }'::jsonb)$$, :'source_r1', repeat('9', 64)),
    'PT001', NULL,
    '1.8 an incompatible logical_fingerprint on the same source is rejected as source_snapshot_changed');
select is(
    (select count(*)::int from private.legacy_migration_sources),
    1, '1.9 the rejected incompatible re-capture creates no row');

-- 1.10 manifest_id already bound to a different proposed_source_instance_id is a categorical conflict.
select throws_ok(
    format($$select private.register_legacy_source('{
        "manifest_id": "%s", "manifest_version": 1,
        "source_system": "tradutor_ia_sqlite_runtime_v1",
        "proposed_source_instance_id": "a1000000-0000-4000-8000-000000000199", "source_schema_version": 3,
        "snapshot_sha256": "%s", "logical_fingerprint": "fp-other",
        "source_reference": "synthetic-batch-4", "capture_timestamp": "2026-08-08T00:00:00Z",
        "publication_count": 1,
        "source_record_digests": [{"legacy_publication_id": "SYN-PUB-01", "digest": "d1"}],
        "approval_state": "approved", "approved_by": "synthetic-operator", "approved_at": "2026-08-08T00:00:00Z"
    }'::jsonb)$$, :'manifest_r1', repeat('8', 64)),
    '23505', NULL,
    '1.10 reusing manifest_id for a different proposed_source_instance_id is a categorical conflict');

-- 1.11 An unapproved manifest is rejected outright.
select throws_ok(
    $$select private.register_legacy_source('{
        "manifest_id": "b1000000-0000-4000-8000-000000000199", "manifest_version": 1,
        "source_system": "tradutor_ia_sqlite_runtime_v1",
        "proposed_source_instance_id": "a1000000-0000-4000-8000-000000000198", "source_schema_version": 3,
        "snapshot_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "logical_fingerprint": "fp-pending", "source_reference": "synthetic-batch-5",
        "capture_timestamp": "2026-08-08T00:00:00Z", "publication_count": 1,
        "source_record_digests": [{"legacy_publication_id": "SYN-PUB-01", "digest": "d1"}],
        "approval_state": "pending", "approved_by": null, "approved_at": null
    }'::jsonb)$$,
    '42501', NULL,
    '1.11 an unapproved manifest (approval_state != approved) is rejected');

-- 1.12 publication_count inconsistent with source_record_digests length is rejected.
select throws_ok(
    $$select private.register_legacy_source('{
        "manifest_id": "b1000000-0000-4000-8000-000000000198", "manifest_version": 1,
        "source_system": "tradutor_ia_sqlite_runtime_v1",
        "proposed_source_instance_id": "a1000000-0000-4000-8000-000000000197", "source_schema_version": 3,
        "snapshot_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "logical_fingerprint": "fp-count-mismatch", "source_reference": "synthetic-batch-6",
        "capture_timestamp": "2026-08-08T00:00:00Z", "publication_count": 5,
        "source_record_digests": [{"legacy_publication_id": "SYN-PUB-01", "digest": "d1"}],
        "approval_state": "approved", "approved_by": "synthetic-operator", "approved_at": "2026-08-08T00:00:00Z"
    }'::jsonb)$$,
    NULL, NULL,
    '1.13 publication_count inconsistent with source_record_digests length is rejected');

-- 1.14 malformed snapshot hash is rejected.
select throws_ok(
    $$select private.register_legacy_source('{
        "manifest_id": "b1000000-0000-4000-8000-000000000197", "manifest_version": 1,
        "source_system": "tradutor_ia_sqlite_runtime_v1",
        "proposed_source_instance_id": "a1000000-0000-4000-8000-000000000196", "source_schema_version": 3,
        "snapshot_sha256": "not-a-hash",
        "logical_fingerprint": "fp-bad-hash", "source_reference": "synthetic-batch-7",
        "capture_timestamp": "2026-08-08T00:00:00Z", "publication_count": 1,
        "source_record_digests": [{"legacy_publication_id": "SYN-PUB-01", "digest": "d1"}],
        "approval_state": "approved", "approved_by": "synthetic-operator", "approved_at": "2026-08-08T00:00:00Z"
    }'::jsonb)$$,
    NULL, NULL,
    '1.15 a malformed snapshot_sha256 is rejected');

-- Verify the source (mark verified for the rest of the file).
update private.legacy_migration_sources set source_state = 'verified', verified_at = now()
 where source_instance_id = :'source_r1';

-- ===========================================================================
-- Section 2 — identity immutability: no operational function accepts changing
-- source_instance_id / source_system / legacy_publication_id after creation.
-- ===========================================================================

-- 2.1 First claim creates the row.
select migration_id as immutable_migration_id, result as immutable_result
from private.claim_legacy_publication(
    :'source_r1'::uuid, 'SYN-PUB-IMMUTABLE', 3, 'digest-immutable-1', 'published_copy', 'community',
    :'owner_x'::uuid, 'resolved_locally_by_uuid_match')
\gset

select is(:'immutable_result'::text, 'created', '2.1 initial claim for SYN-PUB-IMMUTABLE is created');

-- 2.2 No function accepts a source_instance_id/source_system/legacy_publication_id argument
-- for an existing migration_id: none of attach_migration_work/chapter/asset,
-- mark_migration_failure, complete_legacy_migration, append_legacy_migration_event take those
-- parameters at all (structural immutability — confirmed by signature in 04, re-confirmed
-- here behaviorally: calling claim_legacy_publication again with the SAME identity but a
-- DIFFERENT source_record_digest never mutates the stored identity/digest of the existing row).
select migration_id as retry_migration_id, result as retry_result
from private.claim_legacy_publication(
    :'source_r1'::uuid, 'SYN-PUB-IMMUTABLE', 3, 'digest-DIFFERENT', 'published_copy', 'community',
    :'owner_x'::uuid, 'resolved_locally_by_uuid_match')
\gset

select is(:'retry_result'::text, 'conflicting_identity', '2.3 a digest mismatch on retry is classified conflicting_identity, never silently overwritten');
select is(
    (select source_record_digest from private.legacy_publication_migrations where id = :'immutable_migration_id'::uuid),
    'digest-immutable-1',
    '2.4 the stored source_record_digest is unchanged after the conflicting retry');
select is(
    (select source_instance_id from private.legacy_publication_migrations where id = :'immutable_migration_id'::uuid),
    :'source_r1'::uuid,
    '2.5 source_instance_id is unchanged after the conflicting retry');
select is(
    (select legacy_publication_id from private.legacy_publication_migrations where id = :'immutable_migration_id'::uuid),
    'SYN-PUB-IMMUTABLE',
    '2.6 legacy_publication_id is unchanged after the conflicting retry');

-- 2.7 A plain retry with the SAME digest does not alter identity either (idempotent path).
select migration_id as same_retry_id, result as same_retry_result
from private.claim_legacy_publication(
    :'source_r1'::uuid, 'SYN-PUB-IMMUTABLE', 3, 'digest-immutable-1', 'published_copy', 'community',
    :'owner_x'::uuid, 'resolved_locally_by_uuid_match')
\gset
select is(:'same_retry_id'::text, :'immutable_migration_id', '2.8 a matching-digest retry resolves to the same row id');
select isnt(:'same_retry_result'::text, 'created', '2.9 a matching-digest retry is never classified created again');

-- ===========================================================================
-- Section 3 — claim_legacy_publication classification coverage
-- ===========================================================================

-- 3.1 already_completed: drive a fresh row all the way to completed, then re-claim.
select migration_id as ac_id from private.claim_legacy_publication(
    :'source_r1'::uuid, 'SYN-PUB-AC', 3, 'digest-ac', 'published_copy', 'community',
    :'owner_x'::uuid, 'resolved_locally_by_uuid_match') \gset
select private.attach_migration_work(:'ac_id'::uuid, :'work_x'::uuid);
select private.attach_migration_chapter(:'ac_id'::uuid, :'chapter_1'::uuid);
select private.attach_migration_asset(:'ac_id'::uuid, :'chapter_1'::uuid, repeat('1', 64), 1000, 'google_drive', 'synthetic-drive-file-1');
select private.complete_legacy_migration(:'ac_id'::uuid, 'synthetic-runner');

select migration_id as ac_retry_id, result as ac_retry_result from private.claim_legacy_publication(
    :'source_r1'::uuid, 'SYN-PUB-AC', 3, 'digest-ac', 'published_copy', 'community',
    :'owner_x'::uuid, 'resolved_locally_by_uuid_match') \gset
select is(:'ac_retry_result'::text, 'already_completed', '3.1 re-claiming a completed published_copy migration returns already_completed');

-- 3.2 recovery_completed: draft_recovery row driven to recovery_completed, then re-claimed.
select migration_id as rc_id from private.claim_legacy_publication(
    :'source_r1'::uuid, 'SYN-PUB-RC', 3, 'digest-rc', 'draft_recovery', 'draft',
    :'owner_x'::uuid, 'resolved_locally_by_uuid_match') \gset
select private.attach_migration_work(:'rc_id'::uuid, :'work_x'::uuid);
select private.attach_migration_chapter(:'rc_id'::uuid, :'chapter_2'::uuid);
select private.complete_legacy_migration(:'rc_id'::uuid, 'synthetic-runner');

select migration_id as rc_retry_id, result as rc_retry_result from private.claim_legacy_publication(
    :'source_r1'::uuid, 'SYN-PUB-RC', 3, 'digest-rc', 'draft_recovery', 'draft',
    :'owner_x'::uuid, 'resolved_locally_by_uuid_match') \gset
select is(:'rc_retry_result'::text, 'recovery_completed', '3.2 re-claiming a recovery_completed migration returns recovery_completed');

-- 3.3 resumable: intermediate state (claimed), digests match.
select migration_id as rs_id, result as rs_result from private.claim_legacy_publication(
    :'source_r1'::uuid, 'SYN-PUB-RESUMABLE', 3, 'digest-resumable', 'published_copy', 'community',
    :'owner_x'::uuid, 'resolved_locally_by_uuid_match') \gset
select migration_id as rs_retry_id, result as rs_retry_result from private.claim_legacy_publication(
    :'source_r1'::uuid, 'SYN-PUB-RESUMABLE', 3, 'digest-resumable', 'published_copy', 'community',
    :'owner_x'::uuid, 'resolved_locally_by_uuid_match') \gset
select is(:'rs_retry_result'::text, 'resumable', '3.3 re-claiming an intermediate-state migration with matching digests returns resumable');

-- 3.4 failed_retryable resumed via claim.
select private.mark_migration_failure(:'rs_id'::uuid, 'synthetic_transient_error', true);
select migration_id as fr_retry_id, result as fr_retry_result from private.claim_legacy_publication(
    :'source_r1'::uuid, 'SYN-PUB-RESUMABLE', 3, 'digest-resumable', 'published_copy', 'community',
    :'owner_x'::uuid, 'resolved_locally_by_uuid_match') \gset
select is(:'fr_retry_result'::text, 'resumable', '3.4 a failed_retryable migration is classified resumable on retry');

-- 3.5 conflicting_identity: same identity, terminal failed_terminal state.
select migration_id as ft_id from private.claim_legacy_publication(
    :'source_r1'::uuid, 'SYN-PUB-TERMINAL', 3, 'digest-terminal', 'published_copy', 'community',
    :'owner_x'::uuid, 'resolved_locally_by_uuid_match') \gset
select private.mark_migration_failure(:'ft_id'::uuid, 'synthetic_permanent_error', false);
select migration_id as ft_retry_id, result as ft_retry_result from private.claim_legacy_publication(
    :'source_r1'::uuid, 'SYN-PUB-TERMINAL', 3, 'digest-terminal', 'published_copy', 'community',
    :'owner_x'::uuid, 'resolved_locally_by_uuid_match') \gset
select is(:'ft_retry_result'::text, 'conflicting_identity', '3.5 re-claiming a failed_terminal migration is classified conflicting_identity (requires human decision)');

-- 3.6 partial_target_consistent / partial_target_conflicting.
select migration_id as pt_id from private.claim_legacy_publication(
    :'source_r1'::uuid, 'SYN-PUB-PARTIAL', 3, 'digest-partial', 'published_copy', 'community',
    :'owner_x'::uuid, 'resolved_locally_by_uuid_match') \gset
select private.attach_migration_work(:'pt_id'::uuid, :'work_x'::uuid);
select private.attach_migration_chapter(:'pt_id'::uuid, :'chapter_3'::uuid);

select migration_id as pt_retry_id, result as pt_retry_result from private.claim_legacy_publication(
    :'source_r1'::uuid, 'SYN-PUB-PARTIAL', 3, 'digest-partial', 'published_copy', 'community',
    :'owner_x'::uuid, 'resolved_locally_by_uuid_match') \gset
select is(:'pt_retry_result'::text, 'partial_target_consistent', '3.6 a chapter_created migration with matching owner is partial_target_consistent');

select migration_id as pt_conflict_id, result as pt_conflict_result from private.claim_legacy_publication(
    :'source_r1'::uuid, 'SYN-PUB-PARTIAL', 3, 'digest-partial', 'published_copy', 'community',
    :'other_owner'::uuid, 'resolved_locally_by_uuid_match') \gset
select is(:'pt_conflict_result'::text, 'partial_target_conflicting', '3.7 the same partial target claimed with a different owner is partial_target_conflicting');

-- 3.8 A blocked source rejects new claims.
insert into private.legacy_migration_sources
    (source_instance_id, source_system, source_schema_version, initial_snapshot_sha256,
     initial_logical_fingerprint, registered_by, manifest_id, source_state, blocked_at, blocked_reason)
values ('a1000000-0000-4000-8000-000000000199', 'tradutor_ia_sqlite_runtime_v1', 3, repeat('7', 64),
        'fp-blocked', 'synthetic-operator', 'b1000000-0000-4000-8000-000000000199', 'blocked', now(), 'synthetic investigation');
select throws_ok(
    $$select * from private.claim_legacy_publication(
        'a1000000-0000-4000-8000-000000000199'::uuid, 'SYN-PUB-BLOCKED', 3, 'digest-blocked',
        'published_copy', 'community')$$,
    '42501', NULL,
    '3.9 claiming against a blocked source is rejected');

-- ===========================================================================
-- Section 4 — attach_migration_work / attach_migration_chapter validation
-- ===========================================================================
select migration_id as aw_id from private.claim_legacy_publication(
    :'source_r1'::uuid, 'SYN-PUB-ATTACH', 3, 'digest-attach', 'published_copy', 'community',
    :'owner_x'::uuid, 'resolved_locally_by_uuid_match') \gset

-- 4.1 attaching a work owned by a different owner than the migration is rejected.
select throws_ok(
    format('select private.attach_migration_work(%L::uuid, %L::uuid)', :'aw_id', :'other_work'),
    '42501', NULL,
    '4.1 attaching a work whose owner does not match the migration owner is rejected');

-- 4.2 happy path attach_migration_work.
select lives_ok(
    format('select private.attach_migration_work(%L::uuid, %L::uuid)', :'aw_id', :'work_x'),
    '4.2 attaching the correctly-owned work succeeds');
select is(
    (select migration_state from private.legacy_publication_migrations where id = :'aw_id'::uuid),
    'work_created', '4.3 migration_state advances to work_created');

-- 4.4 idempotent retry with the SAME work_id is a no-op, not an error.
select lives_ok(
    format('select private.attach_migration_work(%L::uuid, %L::uuid)', :'aw_id', :'work_x'),
    '4.4 re-attaching the same work_id is idempotent');

-- 4.5 attaching a DIFFERENT work after one is already set is rejected.
select throws_ok(
    format('select private.attach_migration_work(%L::uuid, %L::uuid)', :'aw_id', :'other_work'),
    '55000', NULL,
    '4.5 attaching a different work_id after one is already set is rejected');

-- 4.6 attach_migration_chapter: chapter belonging to a different work is rejected.
select throws_ok(
    format('select private.attach_migration_chapter(%L::uuid, %L::uuid)', :'aw_id', :'chapter_other'),
    '42501', NULL,
    '4.6 attaching a chapter that does not belong to the migration target work is rejected');

select lives_ok(
    format('select private.attach_migration_chapter(%L::uuid, %L::uuid)', :'aw_id', :'chapter_4'),
    '4.7 attaching a chapter that belongs to the target work succeeds');
select is(
    (select migration_state from private.legacy_publication_migrations where id = :'aw_id'::uuid),
    'chapter_created', '4.8 migration_state advances to chapter_created');

-- 4.9 a chapter already carrying live provenance from another migration cannot be attached again.
select migration_id as aw2_id from private.claim_legacy_publication(
    :'source_r1'::uuid, 'SYN-PUB-ATTACH-2', 3, 'digest-attach-2', 'published_copy', 'community',
    :'owner_x'::uuid, 'resolved_locally_by_uuid_match') \gset
select private.attach_migration_work(:'aw2_id'::uuid, :'work_x'::uuid);
select throws_ok(
    format('select private.attach_migration_chapter(%L::uuid, %L::uuid)', :'aw2_id', :'chapter_4'),
    '23505', NULL,
    '4.9 attaching a chapter already carrying live provenance from another migration is rejected');

-- ===========================================================================
-- Section 5 — attach_migration_asset
-- ===========================================================================
-- 5.1 checksum mismatch is rejected.
select throws_ok(
    format('select private.attach_migration_asset(%L::uuid, %L::uuid, %L, 1000, %L, %L)',
        :'aw_id', :'chapter_4', repeat('9', 64), 'google_drive', 'synthetic-drive-file-4'),
    NULL, NULL,
    '5.1 attaching an asset with a checksum that does not match the stored asset is rejected (no chapter_assets row exists for chapter_4 yet)');

insert into private.chapter_assets (chapter_id, storage_provider, storage_file_id, checksum_sha256, byte_size)
values (:'chapter_4', 'google_drive', 'synthetic-drive-file-4', repeat('4', 64), 4000);

select throws_ok(
    format('select private.attach_migration_asset(%L::uuid, %L::uuid, %L, 4000, %L, %L)',
        :'aw_id', :'chapter_4', repeat('9', 64), 'google_drive', 'synthetic-drive-file-4'),
    NULL, NULL,
    '5.2 a wrong expected checksum against the real stored checksum is rejected');

select lives_ok(
    format('select private.attach_migration_asset(%L::uuid, %L::uuid, %L, 4000, %L, %L)',
        :'aw_id', :'chapter_4', repeat('4', 64), 'google_drive', 'synthetic-drive-file-4'),
    '5.3 attaching the asset with the correct checksum/size/provider/file id succeeds');
select is(
    (select migration_state from private.legacy_publication_migrations where id = :'aw_id'::uuid),
    'asset_linked', '5.4 migration_state advances to asset_linked');

-- ===========================================================================
-- Section 6 — mark_migration_failure
-- ===========================================================================
select migration_id as mf_id from private.claim_legacy_publication(
    :'source_r1'::uuid, 'SYN-PUB-FAIL', 3, 'digest-fail', 'published_copy', 'community',
    :'owner_x'::uuid, 'resolved_locally_by_uuid_match') \gset

select throws_ok(
    format('select private.mark_migration_failure(%L::uuid, %L, true)', :'mf_id', 'Not A Valid Code!'),
    NULL, NULL,
    '6.1 a non-categorical (freeform) error_code is rejected');

select lives_ok(
    format('select private.mark_migration_failure(%L::uuid, %L, true)', :'mf_id', 'synthetic_timeout'),
    '6.2 a categorical error_code with retryable=true succeeds');
select is(
    (select migration_state from private.legacy_publication_migrations where id = :'mf_id'::uuid),
    'failed_retryable', '6.3 migration_state becomes failed_retryable');
select is(
    (select attempt_count from private.legacy_publication_migrations where id = :'mf_id'::uuid),
    1, '6.4 attempt_count increments');

select lives_ok(
    format('select private.mark_migration_failure(%L::uuid, %L, false)', :'mf_id', 'synthetic_fatal'),
    '6.5 marking terminal failure from failed_retryable succeeds');
select is(
    (select migration_state from private.legacy_publication_migrations where id = :'mf_id'::uuid),
    'failed_terminal', '6.6 migration_state becomes failed_terminal');

select throws_ok(
    format('select private.mark_migration_failure(%L::uuid, %L, true)', :'mf_id', 'synthetic_retry_after_terminal'),
    '55000', NULL,
    '6.7 marking failure on an already-terminal migration is rejected (failed_terminal requires explicit later action, not another failure call)');

-- No stack trace / raw payload is ever accepted as a parameter for mark_migration_failure —
-- structurally impossible: the function signature has no such parameter at all (confirmed by
-- 04's has_function arg-type check).

-- ===========================================================================
-- Section 7 — complete_legacy_migration: published_copy failures + idempotency
-- ===========================================================================
select migration_id as cp_id from private.claim_legacy_publication(
    :'source_r1'::uuid, 'SYN-PUB-COMPLETE', 3, 'digest-complete', 'published_copy', 'community',
    :'owner_x'::uuid, 'resolved_locally_by_uuid_match') \gset

select throws_ok(
    format('select private.complete_legacy_migration(%L::uuid, %L)', :'cp_id', 'synthetic-runner'),
    '55000', NULL,
    '7.1 completing a published_copy migration with no work/chapter/asset attached is rejected');

select private.attach_migration_work(:'cp_id'::uuid, :'work_x'::uuid);
select throws_ok(
    format('select private.complete_legacy_migration(%L::uuid, %L)', :'cp_id', 'synthetic-runner'),
    '55000', NULL,
    '7.2 completing without a chapter attached is rejected');

select private.attach_migration_chapter(:'cp_id'::uuid, :'chapter_5'::uuid);
select throws_ok(
    format('select private.complete_legacy_migration(%L::uuid, %L)', :'cp_id', 'synthetic-runner'),
    '55000', NULL,
    '7.3 completing published_copy without an asset attached is rejected');

-- chapter_5's asset checksum was seeded earlier (repeat('5',64), byte_size 5000).
select private.attach_migration_asset(:'cp_id'::uuid, :'chapter_5'::uuid, repeat('5', 64), 5000, 'google_drive', 'synthetic-drive-file-5');

select is(
    (select private.complete_legacy_migration(:'cp_id'::uuid, 'synthetic-runner')),
    'completed', '7.4 completing a fully-populated published_copy migration succeeds');
select is(
    (select migration_state from private.legacy_publication_migrations where id = :'cp_id'::uuid),
    'completed', '7.5 migration_state is completed');

-- 7.6 idempotent: a second call returns already_completed, does not error.
select is(
    (select private.complete_legacy_migration(:'cp_id'::uuid, 'synthetic-runner')),
    'already_completed', '7.6 completing an already-completed migration a second time is idempotent');

-- ===========================================================================
-- Section 8 — draft_recovery never reaches community, at either structural layer.
-- ===========================================================================
-- 8.1 The migrator cannot even CLAIM a draft_recovery + community pair: the Fase 2A1 CHECK
-- constraint legacy_pub_migrations_draft_never_community_via_operator would reject the raw
-- INSERT (publication_authorization_state cannot be 'published' before the row exists), and
-- claim_legacy_publication turns that into a clean categorical error before ever attempting it.
select throws_ok(
    $$select * from private.claim_legacy_publication(
        'a1000000-0000-4000-8000-000000000101'::uuid, 'SYN-PUB-DRAFT-COMMUNITY', 3,
        'digest-draft-community', 'draft_recovery', 'community')$$,
    '42501', NULL,
    '8.1 the migrator cannot even claim a draft_recovery + community pair (first structural layer)');
select is(
    (select count(*)::int from private.legacy_publication_migrations where legacy_publication_id = 'SYN-PUB-DRAFT-COMMUNITY'),
    0, '8.2 the rejected draft_recovery + community claim creates no row');

-- complete_legacy_migration ALSO carries its own unconditional draft_recovery+community guard
-- (draft_authorization_policy.md's "Bloqueio estrutural na função de conclusão normal do
-- migrador"), independent of 8.1/8.2. It is not separately exercisable here with a live row:
-- the Fase 2A1 CHECK constraint legacy_pub_migrations_draft_never_community_via_operator
-- applies to every INSERT *and* UPDATE, so no row can ever legally hold
-- (migration_mode=draft_recovery, requested_target_status=community,
-- publication_authorization_state <> 'published') for complete_legacy_migration to observe —
-- the two layers are intentionally redundant per the contract, and the 2A1 constraint alone
-- already makes complete_legacy_migration's internal check unreachable in practice, which is
-- exactly the point: there is no path, at any layer, that produces community from draft_recovery
-- outside authorize_draft_recovery_publication + a separate promotion operation.

-- ===========================================================================
-- Section 9 — draft_recovery authorization (migration_publication_admin only)
-- ===========================================================================
select migration_id as auth_id from private.claim_legacy_publication(
    :'source_r1'::uuid, 'SYN-PUB-AUTH', 3, 'digest-auth', 'draft_recovery', 'draft',
    :'owner_x'::uuid, 'resolved_locally_by_uuid_match') \gset
select private.attach_migration_work(:'auth_id'::uuid, :'work_x'::uuid);
select private.attach_migration_chapter(:'auth_id'::uuid, :'chapter_6'::uuid);
select private.attach_migration_asset(:'auth_id'::uuid, :'chapter_6'::uuid, repeat('7', 64), 7000, 'google_drive', 'synthetic-drive-file-6');
select private.complete_legacy_migration(:'auth_id'::uuid, 'synthetic-runner');
select is(
    (select migration_state from private.legacy_publication_migrations where id = :'auth_id'::uuid),
    'recovery_completed', '9.1 the fixture migration reaches recovery_completed before authorization tests');

-- 9.2 migration_operator cannot even reach the point of evaluating admin logic (no EXECUTE).
set local role migration_operator;
select throws_ok(
    format('select private.authorize_draft_recovery_publication(%L::uuid, %L::uuid, %L, %L::uuid, %L, %L)',
        :'auth_id', :'chapter_6', repeat('7', 64), :'chapter_6', 'recovery_completed', 'synthetic operator attempt'),
    '42501', NULL,
    '9.2 migration_operator cannot call authorize_draft_recovery_publication (no EXECUTE grant, fails on privilege, not business logic)');
reset role;

-- 9.3 migration_publication_admin WITHOUT an authenticated admin session is rejected inside the function.
set local role migration_publication_admin;
select throws_ok(
    format('select private.authorize_draft_recovery_publication(%L::uuid, %L::uuid, %L, %L::uuid, %L, %L)',
        :'auth_id', :'chapter_6', repeat('7', 64), :'chapter_6', 'recovery_completed', 'synthetic missing session'),
    '42501', NULL,
    '9.3 authorize_draft_recovery_publication rejects a call with no authenticated session (current_admin_actor_id() resolves to NULL)');
reset role;

-- 9.4 a reason is mandatory.
set local role migration_publication_admin;
set local request.jwt.claims = '{"sub":"11111111-1111-4111-8111-111111111102","role":"authenticated"}';
select throws_ok(
    format('select private.authorize_draft_recovery_publication(%L::uuid, %L::uuid, %L, %L::uuid, %L, %L)',
        :'auth_id', :'chapter_6', repeat('7', 64), :'chapter_6', 'recovery_completed', ''),
    '23502', NULL,
    '9.4 an empty reason is rejected');

-- 9.5 a wrong expected_state is rejected.
select throws_ok(
    format('select private.authorize_draft_recovery_publication(%L::uuid, %L::uuid, %L, %L::uuid, %L, %L)',
        :'auth_id', :'chapter_6', repeat('7', 64), :'chapter_6', 'chapter_created', 'synthetic reason'),
    '55000', NULL,
    '9.5 an expected_state that does not match the current migration_state is rejected');

-- 9.6 a wrong expected asset hash is rejected.
select throws_ok(
    format('select private.authorize_draft_recovery_publication(%L::uuid, %L::uuid, %L, %L::uuid, %L, %L)',
        :'auth_id', :'chapter_6', repeat('8', 64), :'chapter_6', 'recovery_completed', 'synthetic reason'),
    NULL, NULL,
    '9.6 a wrong expected_pdf_sha256 is rejected');

-- 9.7 the fully-correct authorization succeeds.
select lives_ok(
    format('select private.authorize_draft_recovery_publication(%L::uuid, %L::uuid, %L, %L::uuid, %L, %L)',
        :'auth_id', :'chapter_6', repeat('7', 64), :'chapter_6', 'recovery_completed', 'synthetic administrative reason'),
    '9.7 a fully-correct authorization by migration_publication_admin succeeds');
reset role;

select is(
    (select publication_authorization_state from private.legacy_publication_migrations where id = :'auth_id'::uuid),
    'authorized', '9.8 publication_authorization_state becomes authorized');
select is(
    (select migration_state from private.legacy_publication_migrations where id = :'auth_id'::uuid),
    'publication_authorized', '9.9 migration_state advances to publication_authorized');
select is(
    (select count(*)::int from private.legacy_publication_migration_events
     where migration_id = :'auth_id'::uuid and event_type = 'draft_publication_authorized'),
    1, '9.10 exactly one draft_publication_authorized event was recorded');
select is(
    (select actor_id from private.legacy_publication_migration_events
     where migration_id = :'auth_id'::uuid and event_type = 'draft_publication_authorized'),
    :'admin_x'::uuid,
    '9.11 the event actor_id is the resolved admin session, never a client-supplied field');

-- 9.12 authorization does not publish anything by itself: requested_target_status/chapter
-- status are untouched, and no `published` state exists yet.
select isnt(
    (select publication_authorization_state from private.legacy_publication_migrations where id = :'auth_id'::uuid),
    'published', '9.12 authorization alone never reaches publication_authorization_state = published');
select is(
    (select status from public.chapters where id = :'chapter_6'::uuid),
    'draft', '9.13 the chapter status is untouched by authorization alone (still draft)');

-- ===========================================================================
-- Section 10 — authorization invalidation on asset/hash change
-- ===========================================================================
update private.chapter_assets set checksum_sha256 = repeat('6', 64) where chapter_id = :'chapter_6'::uuid;
select private.attach_migration_asset(:'auth_id'::uuid, :'chapter_6'::uuid, repeat('6', 64), 7000, 'google_drive', 'synthetic-drive-file-6');

select is(
    (select publication_authorization_state from private.legacy_publication_migrations where id = :'auth_id'::uuid),
    'invalidated', '10.1 changing the linked asset hash after authorization invalidates the standing authorization');
select is(
    (select count(*)::int from private.legacy_publication_migration_events
     where migration_id = :'auth_id'::uuid and event_type = 'draft_publication_authorization_invalidated'),
    1, '10.2 exactly one draft_publication_authorization_invalidated event was recorded');

-- ===========================================================================
-- Section 11 — append_legacy_migration_event
-- ===========================================================================
select migration_id as ev_id from private.claim_legacy_publication(
    :'source_r1'::uuid, 'SYN-PUB-EVENT', 3, 'digest-event', 'published_copy', 'community',
    :'owner_x'::uuid, 'resolved_locally_by_uuid_match') \gset

select throws_ok(
    format('select private.append_legacy_migration_event(%L::uuid, %L, %L::jsonb, NULL)', :'ev_id', 'not_a_real_event_type', '{}'),
    NULL, NULL,
    '11.1 an event_type outside the fixed vocabulary is rejected');
select throws_ok(
    format('select private.append_legacy_migration_event(%L::uuid, %L, %L::jsonb, NULL)', :'ev_id', 'migration_cancelled', '["not","an","object"]'),
    NULL, NULL,
    '11.2 metadata that is not a JSON object is rejected');
select lives_ok(
    format('select private.append_legacy_migration_event(%L::uuid, %L, %L::jsonb, NULL)', :'ev_id', 'migration_cancelled', '{"note":"synthetic"}'),
    '11.3 a valid annotation event succeeds');

-- ===========================================================================
-- Section 12 — events remain append-only under every role, real attempts (not just catalog).
-- ===========================================================================
set local role migration_operator;
select throws_ok(
    format('update private.legacy_publication_migration_events set event_type = %L where migration_id = %L::uuid', 'migration_cancelled', :'ev_id'),
    NULL, NULL,
    '12.1 migration_operator cannot UPDATE an event row directly (no grant, and the trigger would reject it even if there were one)');
select throws_ok(
    format('delete from private.legacy_publication_migration_events where migration_id = %L::uuid', :'ev_id'),
    NULL, NULL,
    '12.2 migration_operator cannot DELETE an event row directly');
select throws_ok(
    format('insert into private.legacy_publication_migration_events (migration_id, event_type) values (%L::uuid, %L)', :'ev_id', 'migration_cancelled'),
    NULL, NULL,
    '12.3 migration_operator cannot INSERT an event row directly (no table grant; only the SECURITY DEFINER functions may write)');
reset role;

-- The append-only trigger itself rejects UPDATE/DELETE unconditionally, even for the
-- superuser session running this test file (defense in depth beyond REVOKE).
select throws_ok(
    format('update private.legacy_publication_migration_events set event_type = %L where migration_id = %L::uuid', 'migration_cancelled', :'ev_id'),
    '42501', NULL,
    '12.4 the append-only trigger rejects UPDATE even for a privileged session');
select throws_ok(
    format('delete from private.legacy_publication_migration_events where migration_id = %L::uuid', :'ev_id'),
    '42501', NULL,
    '12.5 the append-only trigger rejects DELETE even for a privileged session');

-- ===========================================================================
-- Section 13 — migration_operator security: no direct DML anywhere, real attempts.
-- ===========================================================================
set local role migration_operator;
select throws_ok(
    $$select 1 from private.legacy_migration_sources$$, NULL, NULL,
    '13.1 migration_operator cannot SELECT legacy_migration_sources directly');
select throws_ok(
    $$select 1 from private.legacy_publication_migrations$$, NULL, NULL,
    '13.2 migration_operator cannot SELECT legacy_publication_migrations directly');
select throws_ok(
    $$insert into private.legacy_migration_sources
        (source_system, source_schema_version, initial_snapshot_sha256, initial_logical_fingerprint, registered_by, manifest_id)
      values ('x', 1, repeat('0', 64), 'fp', 'op', gen_random_uuid())$$,
    NULL, NULL,
    '13.3 migration_operator cannot INSERT into legacy_migration_sources directly');
select throws_ok(
    $$select 1 from public.works$$, NULL, NULL,
    '13.4 migration_operator cannot SELECT public.works directly');
select throws_ok(
    $$select 1 from public.chapters$$, NULL, NULL,
    '13.5 migration_operator cannot SELECT public.chapters directly');
select throws_ok(
    $$select 1 from private.chapter_assets$$, NULL, NULL,
    '13.6 migration_operator cannot SELECT private.chapter_assets directly');
reset role;

-- 13.7 anon/authenticated still have zero access (reaffirms Fase 2A1, now that functions exist).
set local role anon;
select throws_ok(
    $$select private.claim_legacy_publication(
        gen_random_uuid(), 'x', 1, 'x', 'published_copy', 'community')$$,
    '42501', NULL,
    '13.7 anon cannot call claim_legacy_publication (no EXECUTE grant)');
reset role;

set local role authenticated;
select throws_ok(
    $$select private.register_legacy_source('{}'::jsonb)$$,
    '42501', NULL,
    '13.8 authenticated cannot call register_legacy_source (no EXECUTE grant)');
reset role;

-- ===========================================================================
-- Section 14 — parameters are typed; free-text injection attempts are inert.
-- ===========================================================================
select migration_id as inj_id, result as inj_result from private.claim_legacy_publication(
    :'source_r1'::uuid, $$SYN-PUB'; DROP TABLE private.legacy_publication_migrations; --$$, 3,
    'digest-injection', 'published_copy', 'community', :'owner_x'::uuid, 'resolved_locally_by_uuid_match') \gset
select is(:'inj_result'::text, 'created', '14.1 a legacy_publication_id containing quotes/SQL keywords is accepted as inert literal text');
select is(
    (select legacy_publication_id from private.legacy_publication_migrations where id = :'inj_id'::uuid),
    $$SYN-PUB'; DROP TABLE private.legacy_publication_migrations; --$$,
    '14.2 the injection-shaped text round-trips exactly as stored, proving no dynamic SQL was executed');
select has_table('private', 'legacy_publication_migrations', '14.3 the table still exists — no injected DROP ever ran');

select * from finish();
rollback;
