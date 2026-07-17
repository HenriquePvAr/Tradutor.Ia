-- Corrective, additive migration (ALTER POLICY only — no drop, no data change).
--
-- The chapters SELECT policy previously used public.can_read_chapter(id), which re-scans
-- the chapters table. During INSERT ... RETURNING (PostgREST `return=representation`) the
-- freshly inserted row is not yet visible to that nested scan under the statement
-- snapshot, so the policy denied an owner the representation of their own new chapter
-- (HTTP 403). Referencing the chapter's OWN columns (work_id, status) plus a works
-- subquery evaluates correctly during RETURNING while keeping the exact visibility rule.
--
-- can_read_chapter(uuid) is intentionally left as-is: it is still used by the comments and
-- comment_likes policies, where the target chapter already exists (no self-insert race).

begin;

alter policy chapters_select_visible on public.chapters
    using (
        deleted_at is null and exists (
            select 1 from public.works w
            where w.id = chapters.work_id
              and w.deleted_at is null
              and (
                  w.owner_id = (select auth.uid())
                  or (w.status = 'community' and chapters.status = 'community')
              )
        )
    );

commit;
