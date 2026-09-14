revoke all on function public.commit_translation_success(text, integer, text, text, jsonb) from public;
revoke all on function public.commit_translation_success(text, integer, text, text, jsonb) from anon;
revoke all on function public.commit_translation_success(text, integer, text, text, jsonb) from authenticated;
grant execute on function public.commit_translation_success(text, integer, text, text, jsonb) to service_role;
