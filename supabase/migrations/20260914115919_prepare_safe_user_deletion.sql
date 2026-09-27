begin;

alter table public.translation_requests alter column user_id drop not null;
alter table public.translation_requests drop constraint if exists translation_requests_user_id_fkey;
alter table public.translation_requests
  add constraint translation_requests_user_id_fkey
  foreign key (user_id) references auth.users(id) on delete set null;

alter table public.admin_audit_log alter column actor drop not null;
alter table public.admin_audit_log drop constraint if exists admin_audit_log_actor_fkey;
alter table public.admin_audit_log
  add constraint admin_audit_log_actor_fkey
  foreign key (actor) references auth.users(id) on delete set null;

create or replace function public.yomu_user_delete_schema_ready()
returns boolean
language sql
security definer
set search_path = public
as $$
  select
    exists (
      select 1 from pg_constraint c
      join pg_class r on r.oid = c.conrelid
      join pg_class f on f.oid = c.confrelid
      where c.conname = 'translation_requests_user_id_fkey'
        and r.relname = 'translation_requests'
        and f.relname = 'users'
        and c.confdeltype = 'a'
    )
    and exists (
      select 1 from pg_constraint c
      join pg_class r on r.oid = c.conrelid
      join pg_class f on f.oid = c.confrelid
      where c.conname = 'admin_audit_log_actor_fkey'
        and r.relname = 'admin_audit_log'
        and f.relname = 'users'
        and c.confdeltype = 'a'
    );
$$;

revoke all on function public.yomu_user_delete_schema_ready() from public;
grant execute on function public.yomu_user_delete_schema_ready() to service_role;
commit;
