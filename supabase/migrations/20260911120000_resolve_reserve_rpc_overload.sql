-- PostgREST cannot disambiguate an overload with a defaulted third argument:
-- a two-argument request matches both signatures. Keep the explicit legacy
-- three-argument entrypoint for already-shipped callers, but remove its
-- default so the two-argument request has exactly one match. The amount is
-- still ignored by the server-authoritative implementation.
drop function if exists public.reserve_translation_yk(text, uuid, integer);

create function public.reserve_translation_yk(
  p_job_id text, p_device_id uuid, p_amount integer
) returns jsonb language plpgsql security definer
set search_path=pg_catalog,public as $$
begin
  perform p_amount;
  return public.reserve_translation_yk(p_job_id, p_device_id);
end $$;
revoke all on function public.reserve_translation_yk(text, uuid) from public, anon;
revoke all on function public.reserve_translation_yk(text, uuid, integer) from public, anon;
grant execute on function public.reserve_translation_yk(text, uuid) to authenticated;
grant execute on function public.reserve_translation_yk(text, uuid, integer) to authenticated;
