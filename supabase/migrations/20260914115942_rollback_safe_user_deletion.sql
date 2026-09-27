begin;
drop function if exists public.yomu_user_delete_schema_ready();
alter table public.translation_requests drop constraint if exists translation_requests_user_id_fkey;
alter table public.translation_requests
  add constraint translation_requests_user_id_fkey
  foreign key (user_id) references auth.users(id);
alter table public.translation_requests alter column user_id set not null;
alter table public.admin_audit_log drop constraint if exists admin_audit_log_actor_fkey;
alter table public.admin_audit_log
  add constraint admin_audit_log_actor_fkey
  foreign key (actor) references auth.users(id);
alter table public.admin_audit_log alter column actor set not null;
commit;
