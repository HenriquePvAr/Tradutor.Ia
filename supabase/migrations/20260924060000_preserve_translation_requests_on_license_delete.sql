-- Preserve terminal translation history when its license or device is deleted.
-- Active requests must retain both authorization/provenance references, so the
-- CHECK below rejects deletion while a request is pending or processing.
alter table public.translation_requests alter column license_id drop not null;
alter table public.translation_requests drop constraint translation_requests_license_id_fkey;
alter table public.translation_requests
  add constraint translation_requests_license_id_fkey
  foreign key (license_id) references public.licenses(id) on delete set null;

alter table public.translation_requests alter column device_id drop not null;
alter table public.translation_requests drop constraint translation_requests_device_id_fkey;
alter table public.translation_requests
  add constraint translation_requests_device_id_fkey
  foreign key (device_id) references public.license_devices(id) on delete set null;

alter table public.translation_requests
  add constraint translation_requests_active_requires_license_device
  check (status not in ('pending', 'processing') or
         (license_id is not null and device_id is not null));
