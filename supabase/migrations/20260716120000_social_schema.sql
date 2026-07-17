-- Tradutor.Ia social foundation — additive schema, functions, triggers, grants, indexes.
--
-- This migration is purely additive: it creates a private schema, ten public social
-- tables and one private table, plus helper functions and triggers. It performs no
-- DROP/TRUNCATE and touches no existing data. Row Level Security policies live in the
-- companion migration 20260716120100_social_rls.sql.
--
-- Design notes:
--   * UUID primary keys via gen_random_uuid() (core since PG13; Supabase runs PG15).
--   * timestamptz everywhere; created_at/updated_at/deleted_at with soft delete.
--   * States are text + CHECK (not enums) so they can evolve without ALTER TYPE.
--   * Google Drive file ids never live in the public schema — only in private.chapter_assets.

begin;

-- ---------------------------------------------------------------------------
-- Private schema: never exposed to anon/authenticated. Backend-only, future use.
-- ---------------------------------------------------------------------------
create schema if not exists private;
revoke all on schema private from anon, authenticated;

-- ===========================================================================
-- 5.1 profiles — one row per auth user, created automatically on signup.
-- ===========================================================================
create table if not exists public.profiles (
    id uuid primary key references auth.users (id) on delete cascade,
    username text unique,
    display_name text,
    bio text,
    avatar_path text,
    banner_path text,
    profile_video_path text,
    theme_color text,
    show_favorites boolean not null default false,
    show_history boolean not null default false,
    allow_profile_comments boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    deleted_at timestamptz,
    constraint profiles_username_format check (
        username is null
        or (char_length(username) between 3 and 32 and username ~ '^[a-z0-9_]+$')
    ),
    constraint profiles_display_name_len check (
        display_name is null or char_length(display_name) <= 60
    ),
    constraint profiles_bio_len check (bio is null or char_length(bio) <= 500),
    constraint profiles_theme_color_format check (
        theme_color is null or theme_color ~ '^#[0-9a-fA-F]{6}$'
    )
);

-- ===========================================================================
-- 5.2 works
-- ===========================================================================
create table if not exists public.works (
    id uuid primary key default gen_random_uuid(),
    owner_id uuid not null references public.profiles (id) on delete cascade,
    title text not null,
    slug text not null,
    synopsis text,
    cover_path text,
    status text not null default 'draft',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    published_at timestamptz,
    deleted_at timestamptz,
    constraint works_status_check check (status in ('draft', 'private', 'community', 'archived')),
    constraint works_title_len check (char_length(title) between 1 and 200),
    constraint works_slug_format check (char_length(slug) between 1 and 120 and slug ~ '^[a-z0-9-]+$'),
    constraint works_synopsis_len check (synopsis is null or char_length(synopsis) <= 4000),
    -- One slug per owner; different owners may reuse the same slug.
    constraint works_owner_slug_unique unique (owner_id, slug)
);

-- ===========================================================================
-- 5.3 chapters — never stores the Google Drive file id (that lives in private).
-- ===========================================================================
create table if not exists public.chapters (
    id uuid primary key default gen_random_uuid(),
    work_id uuid not null references public.works (id) on delete cascade,
    chapter_number numeric(10, 2) not null,
    title text,
    status text not null default 'draft',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    published_at timestamptz,
    deleted_at timestamptz,
    constraint chapters_status_check check (status in ('draft', 'private', 'community', 'archived')),
    constraint chapters_number_positive check (chapter_number > 0),
    constraint chapters_title_len check (title is null or char_length(title) <= 200),
    constraint chapters_work_number_unique unique (work_id, chapter_number)
);

-- ===========================================================================
-- 5.4 comments — threaded, soft-deleted, author-owned.
-- ===========================================================================
create table if not exists public.comments (
    id uuid primary key default gen_random_uuid(),
    chapter_id uuid not null references public.chapters (id) on delete cascade,
    author_id uuid not null references public.profiles (id) on delete cascade,
    parent_id uuid references public.comments (id) on delete cascade,
    content text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    edited_at timestamptz,
    deleted_at timestamptz,
    moderation_status text not null default 'visible',
    constraint comments_content_len check (char_length(content) between 1 and 4000),
    constraint comments_moderation_check check (moderation_status in ('visible', 'hidden', 'removed')),
    constraint comments_no_self_parent check (parent_id is null or parent_id <> id)
);

-- ===========================================================================
-- 5.5 / 5.6 likes — one row per (target, user); user_id is the acting member.
-- ===========================================================================
create table if not exists public.chapter_likes (
    chapter_id uuid not null references public.chapters (id) on delete cascade,
    user_id uuid not null references public.profiles (id) on delete cascade,
    created_at timestamptz not null default now(),
    primary key (chapter_id, user_id)
);

create table if not exists public.comment_likes (
    comment_id uuid not null references public.comments (id) on delete cascade,
    user_id uuid not null references public.profiles (id) on delete cascade,
    created_at timestamptz not null default now(),
    primary key (comment_id, user_id)
);

-- ===========================================================================
-- 5.7 favorites
-- ===========================================================================
create table if not exists public.favorites (
    user_id uuid not null references public.profiles (id) on delete cascade,
    work_id uuid not null references public.works (id) on delete cascade,
    created_at timestamptz not null default now(),
    primary key (user_id, work_id)
);

-- ===========================================================================
-- 5.8 reading_history
-- ===========================================================================
create table if not exists public.reading_history (
    user_id uuid not null references public.profiles (id) on delete cascade,
    chapter_id uuid not null references public.chapters (id) on delete cascade,
    progress_value numeric(5, 2) not null default 0,
    last_position text,
    started_at timestamptz not null default now(),
    last_read_at timestamptz not null default now(),
    completed_at timestamptz,
    primary key (user_id, chapter_id),
    constraint reading_history_progress_range check (progress_value >= 0 and progress_value <= 100)
);

-- ===========================================================================
-- 5.9 reports
-- ===========================================================================
create table if not exists public.reports (
    id uuid primary key default gen_random_uuid(),
    reporter_id uuid not null references public.profiles (id) on delete cascade,
    target_type text not null,
    target_id uuid not null,
    reason text not null,
    details text,
    status text not null default 'open',
    created_at timestamptz not null default now(),
    resolved_at timestamptz,
    constraint reports_target_type_check check (target_type in ('work', 'chapter', 'comment', 'profile')),
    constraint reports_status_check check (status in ('open', 'reviewing', 'resolved', 'dismissed')),
    constraint reports_reason_len check (char_length(reason) between 1 and 100),
    constraint reports_details_len check (details is null or char_length(details) <= 2000)
);

-- ===========================================================================
-- 5.10 notifications — recipient-owned; inserts are backend-only (no RLS insert policy).
-- ===========================================================================
create table if not exists public.notifications (
    id uuid primary key default gen_random_uuid(),
    recipient_id uuid not null references public.profiles (id) on delete cascade,
    actor_id uuid references public.profiles (id) on delete set null,
    notification_type text not null,
    entity_type text,
    entity_id uuid,
    payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    read_at timestamptz,
    deleted_at timestamptz,
    constraint notifications_type_len check (char_length(notification_type) between 1 and 60)
);

-- ===========================================================================
-- 6. private.chapter_assets — sensitive Drive metadata, never in public.
-- ===========================================================================
create table if not exists private.chapter_assets (
    chapter_id uuid primary key references public.chapters (id) on delete cascade,
    storage_provider text not null default 'google_drive',
    storage_file_id text not null,
    mime_type text not null default 'application/pdf',
    byte_size bigint,
    checksum_sha256 text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    deleted_at timestamptz,
    constraint chapter_assets_provider_check check (storage_provider in ('google_drive')),
    constraint chapter_assets_byte_size_positive check (byte_size is null or byte_size >= 0)
);

-- ===========================================================================
-- 7. Functions and triggers
-- ===========================================================================

-- Generic updated_at maintenance. SECURITY INVOKER, locked search_path, no dynamic SQL.
create or replace function public.set_updated_at()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

-- Immutable owner_id on works.
create or replace function public.prevent_owner_id_change()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
    if new.owner_id is distinct from old.owner_id then
        raise exception 'owner_id is immutable';
    end if;
    return new;
end;
$$;

-- Immutable author_id on comments.
create or replace function public.prevent_author_id_change()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
    if new.author_id is distinct from old.author_id then
        raise exception 'author_id is immutable';
    end if;
    return new;
end;
$$;

-- A reply must live in the same chapter as its parent, and the parent must exist.
create or replace function public.enforce_comment_reply_consistency()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
declare
    parent_chapter uuid;
begin
    if new.parent_id is null then
        return new;
    end if;
    if new.parent_id = new.id then
        raise exception 'a comment cannot be its own parent';
    end if;
    select chapter_id into parent_chapter
    from public.comments
    where id = new.parent_id;
    if parent_chapter is null then
        raise exception 'parent comment does not exist';
    end if;
    if parent_chapter <> new.chapter_id then
        raise exception 'reply must belong to the same chapter as its parent';
    end if;
    return new;
end;
$$;

-- Stamp edited_at whenever a comment's content actually changes on update.
create or replace function public.stamp_comment_edited_at()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
    if new.content is distinct from old.content then
        new.edited_at := now();
    end if;
    return new;
end;
$$;

-- Auto-create a profile row when an auth user is created. SECURITY DEFINER so it can
-- write public.profiles from the auth trigger; search_path is locked and it never reads
-- metadata for roles. Idempotent via ON CONFLICT so signup is never blocked.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
    insert into public.profiles (id)
    values (new.id)
    on conflict (id) do nothing;
    return new;
end;
$$;

-- Read-authorization helpers. SECURITY DEFINER + locked search_path avoids recursive RLS
-- while implementing exactly the frozen visibility rule. They only read.
create or replace function public.owns_work(p_work_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
    select exists (
        select 1
        from public.works w
        where w.id = p_work_id
          and w.owner_id = (select auth.uid())
          and w.deleted_at is null
    );
$$;

create or replace function public.can_read_chapter(p_chapter_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
    select exists (
        select 1
        from public.chapters c
        join public.works w on w.id = c.work_id
        where c.id = p_chapter_id
          and c.deleted_at is null
          and w.deleted_at is null
          and (
              w.owner_id = (select auth.uid())
              or (w.status = 'community' and c.status = 'community')
          )
    );
$$;

-- Triggers: updated_at on every table that has it.
create trigger trg_profiles_updated_at before update on public.profiles
    for each row execute function public.set_updated_at();
create trigger trg_works_updated_at before update on public.works
    for each row execute function public.set_updated_at();
create trigger trg_chapters_updated_at before update on public.chapters
    for each row execute function public.set_updated_at();
create trigger trg_comments_updated_at before update on public.comments
    for each row execute function public.set_updated_at();
create trigger trg_chapter_assets_updated_at before update on private.chapter_assets
    for each row execute function public.set_updated_at();

-- Immutability + consistency triggers.
create trigger trg_works_owner_immutable before update on public.works
    for each row execute function public.prevent_owner_id_change();
create trigger trg_comments_author_immutable before update on public.comments
    for each row execute function public.prevent_author_id_change();
create trigger trg_comments_reply_consistency before insert or update on public.comments
    for each row execute function public.enforce_comment_reply_consistency();
create trigger trg_comments_edited_at before update on public.comments
    for each row execute function public.stamp_comment_edited_at();

-- Auth signup -> profile. AFTER INSERT so the auth row already exists. Additive: this
-- trigger name is new to the project, so no drop guard is needed.
create trigger on_auth_user_created after insert on auth.users
    for each row execute function public.handle_new_user();

-- ===========================================================================
-- 9. Privileges — anon has no social access; authenticated gets table DML gated by RLS.
-- ===========================================================================
revoke all on all tables in schema private from anon, authenticated;
revoke execute on function public.handle_new_user() from anon, authenticated;

-- anon: no access to any social table.
revoke all on public.profiles, public.works, public.chapters, public.comments,
    public.chapter_likes, public.comment_likes, public.favorites,
    public.reading_history, public.reports, public.notifications from anon;

-- authenticated: table-level DML (RLS decides row visibility). Deletion of
-- profiles/works/chapters/comments is *logical* (UPDATE deleted_at), so no hard DELETE is
-- granted there; likes/favorites/history are real rows a user can remove. Every missing
-- policy fails closed regardless of grant.
grant select, insert, update on public.profiles to authenticated;
grant select, insert, update on public.works to authenticated;
grant select, insert, update on public.chapters to authenticated;
grant select, insert, update on public.comments to authenticated;
grant select, insert, delete on public.chapter_likes to authenticated;
grant select, insert, delete on public.comment_likes to authenticated;
grant select, insert, delete on public.favorites to authenticated;
grant select, insert, update, delete on public.reading_history to authenticated;
grant select, insert on public.reports to authenticated;
grant select, update on public.notifications to authenticated;

grant execute on function public.owns_work(uuid) to authenticated;
grant execute on function public.can_read_chapter(uuid) to authenticated;

-- ===========================================================================
-- 10. Indexes — foreign keys, status filters and common lookups. Partial where useful.
-- Primary-key / unique-constraint columns are intentionally not re-indexed.
-- ===========================================================================
create index if not exists idx_works_owner_id on public.works (owner_id);
create index if not exists idx_works_status on public.works (status);
create index if not exists idx_works_published_at on public.works (published_at);
create index if not exists idx_works_community_live on public.works (id)
    where status = 'community' and deleted_at is null;

create index if not exists idx_chapters_work_id on public.chapters (work_id);
create index if not exists idx_chapters_status on public.chapters (status);
create index if not exists idx_chapters_published_at on public.chapters (published_at);
create index if not exists idx_chapters_community_live on public.chapters (work_id)
    where status = 'community' and deleted_at is null;

create index if not exists idx_comments_chapter_id on public.comments (chapter_id);
create index if not exists idx_comments_author_id on public.comments (author_id);
create index if not exists idx_comments_parent_id on public.comments (parent_id);
create index if not exists idx_comments_created_at on public.comments (created_at);
create index if not exists idx_comments_live on public.comments (chapter_id)
    where deleted_at is null;

create index if not exists idx_chapter_likes_chapter_id on public.chapter_likes (chapter_id);
create index if not exists idx_comment_likes_comment_id on public.comment_likes (comment_id);
create index if not exists idx_favorites_user_id on public.favorites (user_id);
create index if not exists idx_reading_history_user_id on public.reading_history (user_id);
create index if not exists idx_reading_history_last_read_at on public.reading_history (last_read_at);
create index if not exists idx_reports_reporter_id on public.reports (reporter_id);
create index if not exists idx_reports_status on public.reports (status);
create index if not exists idx_notifications_recipient_id on public.notifications (recipient_id);
create index if not exists idx_notifications_created_at on public.notifications (created_at);
create index if not exists idx_notifications_unread on public.notifications (recipient_id)
    where read_at is null and deleted_at is null;

commit;
