-- Structural pgTAP tests for the social schema. Run via `supabase test db` (Docker).
-- Wrapped in a transaction that rolls back, leaving no permanent data.
begin;
select no_plan();

-- Schemas ------------------------------------------------------------------
select has_schema('public', 'public schema exists');
select has_schema('private', 'private schema exists');

-- Tables exist -------------------------------------------------------------
select has_table('public', 'profiles', 'profiles table exists');
select has_table('public', 'works', 'works table exists');
select has_table('public', 'chapters', 'chapters table exists');
select has_table('public', 'comments', 'comments table exists');
select has_table('public', 'chapter_likes', 'chapter_likes table exists');
select has_table('public', 'comment_likes', 'comment_likes table exists');
select has_table('public', 'favorites', 'favorites table exists');
select has_table('public', 'reading_history', 'reading_history table exists');
select has_table('public', 'reports', 'reports table exists');
select has_table('public', 'notifications', 'notifications table exists');
select has_table('private', 'chapter_assets', 'private.chapter_assets exists');

-- Primary keys -------------------------------------------------------------
select col_is_pk('public', 'profiles', 'id', 'profiles pk is id');
select col_is_pk('public', 'works', 'id', 'works pk is id');
select col_is_pk('public', 'chapters', 'id', 'chapters pk is id');
select col_is_pk('public', 'comments', 'id', 'comments pk is id');
select col_is_pk('public', 'chapter_likes', ARRAY['chapter_id', 'user_id'], 'chapter_likes composite pk');
select col_is_pk('public', 'comment_likes', ARRAY['comment_id', 'user_id'], 'comment_likes composite pk');
select col_is_pk('public', 'favorites', ARRAY['user_id', 'work_id'], 'favorites composite pk');
select col_is_pk('public', 'reading_history', ARRAY['user_id', 'chapter_id'], 'reading_history composite pk');
select col_is_pk('public', 'reports', 'id', 'reports pk is id');
select col_is_pk('public', 'notifications', 'id', 'notifications pk is id');
select col_is_pk('private', 'chapter_assets', 'chapter_id', 'chapter_assets pk is chapter_id');

-- Key columns and types ----------------------------------------------------
select col_type_is('public', 'profiles', 'id', 'uuid', 'profiles.id is uuid');
select col_type_is('public', 'works', 'owner_id', 'uuid', 'works.owner_id is uuid');
select col_type_is('public', 'works', 'created_at', 'timestamp with time zone', 'works.created_at is timestamptz');
select col_type_is('public', 'chapters', 'chapter_number', 'numeric(10,2)', 'chapters.chapter_number numeric');
select col_type_is('public', 'comments', 'content', 'text', 'comments.content is text');
select col_type_is('public', 'notifications', 'payload', 'jsonb', 'notifications.payload is jsonb');
select has_column('public', 'works', 'deleted_at', 'works has soft-delete column');
select has_column('public', 'chapters', 'deleted_at', 'chapters has soft-delete column');
select has_column('public', 'comments', 'deleted_at', 'comments has soft-delete column');
select has_column('private', 'chapter_assets', 'storage_file_id', 'chapter_assets keeps the Drive id');

-- The public schema must never carry a Drive file id column.
select is(
    (select count(*)::int from information_schema.columns
     where table_schema = 'public' and column_name = 'storage_file_id'),
    0,
    'no storage_file_id column anywhere in public schema'
);

-- Foreign keys -------------------------------------------------------------
select fk_ok('public', 'profiles', 'id', 'auth', 'users', 'id');
select fk_ok('public', 'works', 'owner_id', 'public', 'profiles', 'id');
select fk_ok('public', 'chapters', 'work_id', 'public', 'works', 'id');
select fk_ok('public', 'comments', 'chapter_id', 'public', 'chapters', 'id');
select fk_ok('public', 'comments', 'author_id', 'public', 'profiles', 'id');
select fk_ok('public', 'comments', 'parent_id', 'public', 'comments', 'id');
select fk_ok('public', 'chapter_likes', 'chapter_id', 'public', 'chapters', 'id');
select fk_ok('public', 'favorites', 'work_id', 'public', 'works', 'id');
select fk_ok('public', 'reading_history', 'chapter_id', 'public', 'chapters', 'id');
select fk_ok('private', 'chapter_assets', 'chapter_id', 'public', 'chapters', 'id');

-- Unique constraints -------------------------------------------------------
select col_is_unique('public', 'profiles', 'username', 'username is unique');
select col_is_unique('public', 'works', ARRAY['owner_id', 'slug'], 'work slug unique per owner');
select col_is_unique('public', 'chapters', ARRAY['work_id', 'chapter_number'], 'chapter number unique per work');

-- RLS enabled --------------------------------------------------------------
select is(
    (select bool_and(relrowsecurity) from pg_class c
     join pg_namespace n on n.oid = c.relnamespace
     where n.nspname = 'public'
       and c.relname in ('profiles','works','chapters','comments','chapter_likes',
                         'comment_likes','favorites','reading_history','reports','notifications')),
    true,
    'RLS enabled on every public social table'
);
select is(
    (select relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace
     where n.nspname = 'private' and c.relname = 'chapter_assets'),
    true,
    'RLS enabled on private.chapter_assets'
);

-- Policies exist and target authenticated ----------------------------------
select policies_are('public', 'profiles',
    ARRAY['profiles_select_authenticated', 'profiles_insert_self', 'profiles_update_self'],
    'profiles policies as expected');
select policies_are('public', 'works',
    ARRAY['works_select_community_or_owner', 'works_insert_own', 'works_update_own'],
    'works policies as expected');
select policies_are('public', 'chapters',
    ARRAY['chapters_select_visible', 'chapters_insert_owner_work', 'chapters_update_owner_work'],
    'chapters policies as expected');
select policies_are('public', 'reports',
    ARRAY['reports_select_own', 'reports_insert_own'],
    'reports has no user-facing status change policy');
select policies_are('public', 'notifications',
    ARRAY['notifications_select_own', 'notifications_update_own'],
    'notifications has no client insert policy');

-- private.chapter_assets has zero policies (fail-closed).
select is(
    (select count(*)::int from pg_policies where schemaname = 'private' and tablename = 'chapter_assets'),
    0,
    'private.chapter_assets exposes no policy'
);

-- Privileges: anon and authenticated cannot touch the private schema --------
select is(
    (select count(*)::int from information_schema.role_table_grants
     where table_schema = 'private' and grantee in ('anon', 'authenticated')),
    0,
    'no table grants to anon/authenticated in private schema'
);
select ok(
    not has_schema_privilege('anon', 'private', 'USAGE'),
    'anon has no USAGE on private schema'
);
select ok(
    not has_schema_privilege('authenticated', 'private', 'USAGE'),
    'authenticated has no USAGE on private schema'
);

-- anon has no DML on the social tables.
select is(
    (select count(*)::int from information_schema.role_table_grants
     where table_schema = 'public' and grantee = 'anon'
       and table_name in ('profiles','works','chapters','comments','favorites',
                         'reading_history','reports','notifications','chapter_likes','comment_likes')),
    0,
    'anon has no grants on the social tables'
);

-- Functions and trigger ----------------------------------------------------
select has_function('public', 'handle_new_user', 'handle_new_user exists');
select has_function('public', 'can_read_chapter', ARRAY['uuid'], 'can_read_chapter(uuid) exists');
select has_function('public', 'owns_work', ARRAY['uuid'], 'owns_work(uuid) exists');
select has_function('public', 'set_updated_at', 'set_updated_at exists');
select ok(
    exists (select 1 from pg_trigger where tgname = 'on_auth_user_created' and not tgisinternal),
    'auth signup trigger installed'
);

-- Indexes ------------------------------------------------------------------
select has_index('public', 'works', 'idx_works_owner_id', 'works.owner_id indexed');
select has_index('public', 'chapters', 'idx_chapters_work_id', 'chapters.work_id indexed');
select has_index('public', 'comments', 'idx_comments_chapter_id', 'comments.chapter_id indexed');
select has_index('public', 'notifications', 'idx_notifications_recipient_id', 'notifications.recipient_id indexed');

select * from finish();
rollback;
