-- Restrict the reservation-gated translation entrypoint to authenticated callers.
REVOKE EXECUTE ON FUNCTION public.begin_translation_request(text, uuid, uuid, text, integer)
  FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.begin_translation_request(text, uuid, uuid, text, integer)
  TO authenticated, service_role, postgres;
-- Keep the pre-hardening overload fail-closed, but remove its callable surface.
REVOKE EXECUTE ON FUNCTION public.begin_translation_request(text, uuid, text, integer)
  FROM PUBLIC, anon, authenticated;
