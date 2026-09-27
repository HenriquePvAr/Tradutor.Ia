-- OUT_OF_HISTORY_REMOTE_DRIFT captured read-only on 2026-09-27.
-- Not a deployable migration and intentionally absent from supabase/migrations.
-- The live schema already has nullable license_id with ON DELETE SET NULL,
-- while device_id remains NOT NULL with a restrictive FK.
ALTER TABLE public.translation_requests
  ALTER COLUMN license_id DROP NOT NULL;
ALTER TABLE public.translation_requests
  DROP CONSTRAINT IF EXISTS translation_requests_license_id_fkey;
ALTER TABLE public.translation_requests
  ADD CONSTRAINT translation_requests_license_id_fkey
  FOREIGN KEY (license_id) REFERENCES public.licenses(id) ON DELETE SET NULL;
