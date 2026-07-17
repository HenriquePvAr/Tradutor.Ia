-- Behavioral RLS pgTAP tests: ownership, community visibility, anonymous denial.
-- Run via `supabase test db` (Docker). Everything happens in a rolled-back transaction.
--
-- Users are created through auth.users so the handle_new_user trigger builds their
-- profiles (also covering scenarios 33-34). Role/claims are switched with SET LOCAL to
-- emulate USER_A, USER_B and anonymous. auth.uid() reads request.jwt.claims->>'sub'.
begin;
select no_plan();

-- Fixed identities -----------------------------------------------------------
\set user_a  '11111111-1111-1111-1111-111111111111'
\set user_b  '22222222-2222-2222-2222-222222222222'

-- Seed two auth users as the superuser; the trigger auto-creates their profiles.
insert into auth.users (id, instance_id, aud, role, email, encrypted_password,
                        email_confirmed_at, created_at, updated_at,
                        raw_app_meta_data, raw_user_meta_data)
values
    (:'user_a', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
     'user_a@test.local', '', now(), now(), now(), '{}'::jsonb, '{}'::jsonb),
    (:'user_b', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
     'user_b@test.local', '', now(), now(), now(), '{}'::jsonb, '{}'::jsonb);

-- Scenario 33/34: the trigger created a profile per user, without any metadata role.
select is((select count(*)::int from public.profiles where id in (:'user_a', :'user_b')), 2,
    '33/34 handle_new_user created a profile per signup, no metadata needed');

-- Seed content as the superuser (bypasses RLS): A owns a community work + a private work.
insert into public.works (id, owner_id, title, slug, status)
values
    ('aaaa1111-0000-0000-0000-000000000001', :'user_a', 'A Community', 'a-community', 'community'),
    ('aaaa1111-0000-0000-0000-000000000002', :'user_a', 'A Private', 'a-private', 'private');
insert into public.chapters (id, work_id, chapter_number, status)
values
    ('cccc1111-0000-0000-0000-000000000001', 'aaaa1111-0000-0000-0000-000000000001', 1, 'community'),
    ('cccc1111-0000-0000-0000-000000000002', 'aaaa1111-0000-0000-0000-000000000002', 1, 'private');
insert into public.comments (id, chapter_id, author_id, content)
values ('dddd1111-0000-0000-0000-000000000001',
        'cccc1111-0000-0000-0000-000000000001', :'user_a', 'A root comment');

-- ===========================================================================
-- profiles
-- ===========================================================================
set local role anon;
set local request.jwt.claims = '';
select is((select count(*)::int from public.profiles), 0, '1 anonymous cannot read profiles');
reset role;

set local role authenticated;
set local request.jwt.claims = '{"sub":"22222222-2222-2222-2222-222222222222","role":"authenticated"}';
select ok((select count(*) from public.profiles) >= 2, '2 authenticated reads profiles');
select lives_ok(
    $$update public.profiles set display_name = 'B name' where id = '22222222-2222-2222-2222-222222222222'$$,
    '3 B updates own profile');
-- RLS silently filters A's row from B's update (0 rows affected, no error); verify unchanged.
update public.profiles set display_name = 'hacked-by-b' where id = '11111111-1111-1111-1111-111111111111';
select isnt(
    (select display_name from public.profiles where id = '11111111-1111-1111-1111-111111111111'),
    'hacked-by-b',
    '4 B cannot update A profile');
reset role;

-- ===========================================================================
-- works
-- ===========================================================================
set local role authenticated;
set local request.jwt.claims = '{"sub":"11111111-1111-1111-1111-111111111111","role":"authenticated"}';
select lives_ok(
    $$insert into public.works (owner_id, title, slug, status)
      values ('11111111-1111-1111-1111-111111111111', 'A New', 'a-new', 'draft')$$,
    '5 A creates a work as A');
select throws_ok(
    $$insert into public.works (owner_id, title, slug, status)
      values ('22222222-2222-2222-2222-222222222222', 'Spoof', 'spoof', 'draft')$$,
    '42501',
    NULL,
    '6 A cannot create a work owned by B');
select is((select count(*)::int from public.works where owner_id = '11111111-1111-1111-1111-111111111111'
           and slug = 'a-private'), 1, '10 A reads own private work');
reset role;

set local role authenticated;
set local request.jwt.claims = '{"sub":"22222222-2222-2222-2222-222222222222","role":"authenticated"}';
select is((select count(*)::int from public.works where slug = 'a-community'), 1, '7 B reads A community work');
select is((select count(*)::int from public.works where slug = 'a-private'), 0, '8 B cannot read A private work');
update public.works set title = 'hacked-by-b' where slug = 'a-community';
select isnt(
    (select title from public.works where slug = 'a-community'),
    'hacked-by-b',
    '9 B cannot edit A work');
reset role;

-- ===========================================================================
-- chapters
-- ===========================================================================
set local role authenticated;
set local request.jwt.claims = '{"sub":"11111111-1111-1111-1111-111111111111","role":"authenticated"}';
select lives_ok(
    $$insert into public.chapters (work_id, chapter_number, status)
      values ('aaaa1111-0000-0000-0000-000000000001', 2, 'draft')$$,
    '11 A creates a chapter in own work');
reset role;

set local role authenticated;
set local request.jwt.claims = '{"sub":"22222222-2222-2222-2222-222222222222","role":"authenticated"}';
select throws_ok(
    $$insert into public.chapters (work_id, chapter_number, status)
      values ('aaaa1111-0000-0000-0000-000000000001', 99, 'draft')$$,
    '42501',
    NULL,
    '12 B cannot add a chapter to A work');
select is((select count(*)::int from public.chapters
           where id = 'cccc1111-0000-0000-0000-000000000001'), 1, '13 B reads community chapter');
select is((select count(*)::int from public.chapters
           where id = 'cccc1111-0000-0000-0000-000000000002'), 0, '14 B cannot read private chapter');
reset role;

-- ===========================================================================
-- comments
-- ===========================================================================
set local role authenticated;
set local request.jwt.claims = '{"sub":"11111111-1111-1111-1111-111111111111","role":"authenticated"}';
select lives_ok(
    $$insert into public.comments (chapter_id, author_id, content)
      values ('cccc1111-0000-0000-0000-000000000001', '11111111-1111-1111-1111-111111111111', 'hi from A')$$,
    '15 A comments as A');
select throws_ok(
    $$insert into public.comments (chapter_id, author_id, content)
      values ('cccc1111-0000-0000-0000-000000000001', '22222222-2222-2222-2222-222222222222', 'spoof')$$,
    '42501',
    NULL,
    '16 A cannot comment as B');
select lives_ok(
    $$update public.comments set content = 'A edited' where id = 'dddd1111-0000-0000-0000-000000000001'$$,
    '17 A edits own comment');
-- A soft-deletes own root comment; a reply by A must remain.
insert into public.comments (id, chapter_id, author_id, parent_id, content)
values ('dddd1111-0000-0000-0000-000000000002', 'cccc1111-0000-0000-0000-000000000001',
        '11111111-1111-1111-1111-111111111111', 'dddd1111-0000-0000-0000-000000000001', 'A reply');
select lives_ok(
    $$update public.comments set deleted_at = now() where id = 'dddd1111-0000-0000-0000-000000000001'$$,
    '19 A soft-deletes own comment');
select is((select count(*)::int from public.comments where id = 'dddd1111-0000-0000-0000-000000000002'), 1,
    '20 reply survives parent soft-delete');
-- author_id is immutable.
select throws_ok(
    $$update public.comments set author_id = '22222222-2222-2222-2222-222222222222'
      where id = 'dddd1111-0000-0000-0000-000000000002'$$,
    NULL,
    'author_id is immutable',
    '32 author_id is immutable');
reset role;

set local role authenticated;
set local request.jwt.claims = '{"sub":"22222222-2222-2222-2222-222222222222","role":"authenticated"}';
update public.comments set content = 'hacked-by-b' where id = 'dddd1111-0000-0000-0000-000000000002';
select isnt(
    (select content from public.comments where id = 'dddd1111-0000-0000-0000-000000000002'),
    'hacked-by-b',
    '18 B cannot edit A comment');
reset role;

-- owner_id immutability on works (scenario 31).
set local role authenticated;
set local request.jwt.claims = '{"sub":"11111111-1111-1111-1111-111111111111","role":"authenticated"}';
select throws_ok(
    $$update public.works set owner_id = '22222222-2222-2222-2222-222222222222' where slug = 'a-community'$$,
    NULL,
    'owner_id is immutable',
    '31 owner_id is immutable');
reset role;

-- ===========================================================================
-- likes
-- ===========================================================================
set local role authenticated;
set local request.jwt.claims = '{"sub":"22222222-2222-2222-2222-222222222222","role":"authenticated"}';
select lives_ok(
    $$insert into public.chapter_likes (chapter_id, user_id)
      values ('cccc1111-0000-0000-0000-000000000001', '22222222-2222-2222-2222-222222222222')$$,
    '21 B likes a community chapter as B');
select throws_ok(
    $$insert into public.chapter_likes (chapter_id, user_id)
      values ('cccc1111-0000-0000-0000-000000000001', '11111111-1111-1111-1111-111111111111')$$,
    '42501',
    NULL,
    '22 B cannot like as A');
reset role;

-- ===========================================================================
-- favorites and reading history — private per user
-- ===========================================================================
insert into public.favorites (user_id, work_id) values
    (:'user_a', 'aaaa1111-0000-0000-0000-000000000001'),
    (:'user_b', 'aaaa1111-0000-0000-0000-000000000001');
insert into public.reading_history (user_id, chapter_id, progress_value) values
    (:'user_a', 'cccc1111-0000-0000-0000-000000000001', 50),
    (:'user_b', 'cccc1111-0000-0000-0000-000000000001', 10);

set local role authenticated;
set local request.jwt.claims = '{"sub":"11111111-1111-1111-1111-111111111111","role":"authenticated"}';
select is((select count(*)::int from public.favorites), 1, '23 favorites isolated per user');
select is((select count(*)::int from public.reading_history), 1, '24 reading history isolated per user');
reset role;

-- ===========================================================================
-- reports
-- ===========================================================================
set local role authenticated;
set local request.jwt.claims = '{"sub":"11111111-1111-1111-1111-111111111111","role":"authenticated"}';
select lives_ok(
    $$insert into public.reports (reporter_id, target_type, target_id, reason)
      values ('11111111-1111-1111-1111-111111111111', 'comment',
              'dddd1111-0000-0000-0000-000000000002', 'spam')$$,
    '25 report created as the authenticated reporter');
select throws_ok(
    $$insert into public.reports (reporter_id, target_type, target_id, reason, status)
      values ('11111111-1111-1111-1111-111111111111', 'comment',
              'dddd1111-0000-0000-0000-000000000002', 'spam', 'resolved')$$,
    NULL,
    NULL,
    '26 user cannot open a report already in a resolved status');
-- No UPDATE grant/policy on reports: a plain member cannot change status at all.
select throws_ok(
    $$update public.reports set status = 'dismissed'
      where reporter_id = '11111111-1111-1111-1111-111111111111'$$,
    '42501',
    NULL,
    '26b user cannot change report status');
reset role;

-- ===========================================================================
-- notifications
-- ===========================================================================
insert into public.notifications (recipient_id, notification_type)
values (:'user_b', 'test');

set local role authenticated;
set local request.jwt.claims = '{"sub":"11111111-1111-1111-1111-111111111111","role":"authenticated"}';
select is((select count(*)::int from public.notifications), 0, '27 A cannot see B notification');
select throws_ok(
    $$insert into public.notifications (recipient_id, notification_type)
      values ('11111111-1111-1111-1111-111111111111', 'spoof')$$,
    '42501',
    NULL,
    '27b authenticated cannot insert notifications directly');
reset role;

-- ===========================================================================
-- private.chapter_assets — never visible to anon/authenticated (28/29/30)
-- ===========================================================================
insert into private.chapter_assets (chapter_id, storage_file_id)
values ('cccc1111-0000-0000-0000-000000000001', 'drive-file-secret-xyz');

set local role authenticated;
set local request.jwt.claims = '{"sub":"11111111-1111-1111-1111-111111111111","role":"authenticated"}';
select throws_ok(
    $$select storage_file_id from private.chapter_assets$$,
    '42501',
    NULL,
    '28 authenticated cannot read private.chapter_assets');
reset role;

set local role anon;
set local request.jwt.claims = '';
select throws_ok(
    $$select storage_file_id from private.chapter_assets$$,
    '42501',
    NULL,
    '29 anon cannot read private.chapter_assets');
reset role;

-- 30: the Drive id lives only in private (asserted structurally in 00_structure_test.sql).

-- ===========================================================================
-- updated_at trigger (scenario 35)
-- ===========================================================================
do $$
declare
    before_ts timestamptz;
    after_ts timestamptz;
begin
    select updated_at into before_ts from public.works where slug = 'a-community';
    perform pg_sleep(0.01);
    update public.works set title = 'A Community v2' where slug = 'a-community';
    select updated_at into after_ts from public.works where slug = 'a-community';
    if after_ts <= before_ts then
        raise exception 'updated_at did not advance';
    end if;
end;
$$;
select ok(true, '35 updated_at advances on update');

select * from finish();
rollback;
