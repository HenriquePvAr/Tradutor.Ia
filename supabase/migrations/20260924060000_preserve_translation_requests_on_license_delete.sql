-- Preserve translation history when a user's license is deleted.
-- translation_requests.license_id was NOT NULL + ON DELETE NO ACTION, which blocked
-- deleting a user (auth.users delete -> licenses cascade -> blocked by this FK).
-- Making it nullable + ON DELETE SET NULL lets the license be removed while the
-- translation_requests row survives (detached), preserving history.
alter table public.translation_requests alter column license_id drop not null;
alter table public.translation_requests drop constraint translation_requests_license_id_fkey;
alter table public.translation_requests
  add constraint translation_requests_license_id_fkey
  foreign key (license_id) references public.licenses(id) on delete set null;
