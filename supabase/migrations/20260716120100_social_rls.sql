-- Tradutor.Ia social foundation — Row Level Security.
--
-- Every public social table is RLS-protected and every policy targets `authenticated`
-- only (never public/anon). Anonymous callers therefore read nothing. Ownership is
-- derived from (select auth.uid()); owner_id/author_id/user_id are never trusted from a
-- client payload (immutability is additionally enforced by triggers/PKs). The frozen
-- visibility rule: community content is readable by any authenticated member;
-- draft/private is owner-only; nothing is anonymous-readable.

begin;

alter table public.profiles enable row level security;
alter table public.works enable row level security;
alter table public.chapters enable row level security;
alter table public.comments enable row level security;
alter table public.chapter_likes enable row level security;
alter table public.comment_likes enable row level security;
alter table public.favorites enable row level security;
alter table public.reading_history enable row level security;
alter table public.reports enable row level security;
alter table public.notifications enable row level security;

-- private.chapter_assets: keep RLS on with zero policies so even a stray grant denies.
alter table private.chapter_assets enable row level security;

-- ---------------------------------------------------------------------------
-- profiles
-- ---------------------------------------------------------------------------
create policy profiles_select_authenticated on public.profiles
    for select to authenticated
    using (deleted_at is null);

create policy profiles_insert_self on public.profiles
    for insert to authenticated
    with check (id = (select auth.uid()));

create policy profiles_update_self on public.profiles
    for update to authenticated
    using (id = (select auth.uid()))
    with check (id = (select auth.uid()));

-- ---------------------------------------------------------------------------
-- works — owner reads own (any non-deleted state); members read community.
-- ---------------------------------------------------------------------------
create policy works_select_community_or_owner on public.works
    for select to authenticated
    using (
        deleted_at is null
        and (owner_id = (select auth.uid()) or status = 'community')
    );

create policy works_insert_own on public.works
    for insert to authenticated
    with check (owner_id = (select auth.uid()));

create policy works_update_own on public.works
    for update to authenticated
    using (owner_id = (select auth.uid()))
    with check (owner_id = (select auth.uid()));

-- ---------------------------------------------------------------------------
-- chapters — visibility derived from the parent work; only the work owner writes.
-- ---------------------------------------------------------------------------
create policy chapters_select_visible on public.chapters
    for select to authenticated
    using (public.can_read_chapter(id));

create policy chapters_insert_owner_work on public.chapters
    for insert to authenticated
    with check (public.owns_work(work_id));

create policy chapters_update_owner_work on public.chapters
    for update to authenticated
    using (public.owns_work(work_id))
    with check (public.owns_work(work_id));

-- ---------------------------------------------------------------------------
-- comments — read on accessible chapters; author owns writes; soft delete via update.
-- ---------------------------------------------------------------------------
create policy comments_select_on_readable_chapter on public.comments
    for select to authenticated
    using (
        public.can_read_chapter(chapter_id)
        and (author_id = (select auth.uid()) or deleted_at is null)
    );

create policy comments_insert_self_on_readable_chapter on public.comments
    for insert to authenticated
    with check (
        author_id = (select auth.uid())
        and public.can_read_chapter(chapter_id)
    );

create policy comments_update_own on public.comments
    for update to authenticated
    using (author_id = (select auth.uid()))
    with check (author_id = (select auth.uid()));

-- ---------------------------------------------------------------------------
-- chapter_likes / comment_likes — a member manages only its own row on readable content.
-- ---------------------------------------------------------------------------
create policy chapter_likes_select_authenticated on public.chapter_likes
    for select to authenticated
    using (public.can_read_chapter(chapter_id));

create policy chapter_likes_insert_self on public.chapter_likes
    for insert to authenticated
    with check (
        user_id = (select auth.uid())
        and public.can_read_chapter(chapter_id)
    );

create policy chapter_likes_delete_self on public.chapter_likes
    for delete to authenticated
    using (user_id = (select auth.uid()));

create policy comment_likes_select_authenticated on public.comment_likes
    for select to authenticated
    using (
        exists (
            select 1 from public.comments c
            where c.id = comment_id and public.can_read_chapter(c.chapter_id)
        )
    );

create policy comment_likes_insert_self on public.comment_likes
    for insert to authenticated
    with check (
        user_id = (select auth.uid())
        and exists (
            select 1 from public.comments c
            where c.id = comment_id and public.can_read_chapter(c.chapter_id)
        )
    );

create policy comment_likes_delete_self on public.comment_likes
    for delete to authenticated
    using (user_id = (select auth.uid()));

-- ---------------------------------------------------------------------------
-- favorites — strictly private to the owning user.
-- ---------------------------------------------------------------------------
create policy favorites_select_own on public.favorites
    for select to authenticated
    using (user_id = (select auth.uid()));

create policy favorites_insert_own on public.favorites
    for insert to authenticated
    with check (user_id = (select auth.uid()));

create policy favorites_delete_own on public.favorites
    for delete to authenticated
    using (user_id = (select auth.uid()));

-- ---------------------------------------------------------------------------
-- reading_history — strictly private to the owning user.
-- ---------------------------------------------------------------------------
create policy reading_history_select_own on public.reading_history
    for select to authenticated
    using (user_id = (select auth.uid()));

create policy reading_history_insert_own on public.reading_history
    for insert to authenticated
    with check (user_id = (select auth.uid()));

create policy reading_history_update_own on public.reading_history
    for update to authenticated
    using (user_id = (select auth.uid()))
    with check (user_id = (select auth.uid()));

create policy reading_history_delete_own on public.reading_history
    for delete to authenticated
    using (user_id = (select auth.uid()));

-- ---------------------------------------------------------------------------
-- reports — a member files and sees only its own; status is not user-writable.
-- ---------------------------------------------------------------------------
create policy reports_select_own on public.reports
    for select to authenticated
    using (reporter_id = (select auth.uid()));

create policy reports_insert_own on public.reports
    for insert to authenticated
    with check (
        reporter_id = (select auth.uid())
        and status = 'open'
    );

-- ---------------------------------------------------------------------------
-- notifications — recipient reads and marks own as read; no client-side insert.
-- ---------------------------------------------------------------------------
create policy notifications_select_own on public.notifications
    for select to authenticated
    using (recipient_id = (select auth.uid()));

create policy notifications_update_own on public.notifications
    for update to authenticated
    using (recipient_id = (select auth.uid()))
    with check (recipient_id = (select auth.uid()));

commit;
