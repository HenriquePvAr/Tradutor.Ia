-- Harden raw table privileges for Scan Beta licensing objects.
--
-- Authenticated clients may read their own safe rows through RLS and execute the
-- authorization RPC. They must not mutate, truncate, trigger, or reference raw
-- licensing tables directly.

revoke all privileges on public.beta_tester_entitlements from anon, authenticated;
revoke all privileges on public.beta_tester_devices from anon, authenticated;
revoke all privileges on public.beta_tester_license_events from anon, authenticated;

grant select on public.beta_tester_entitlements to authenticated;
grant select on public.beta_tester_devices to authenticated;
grant select on public.beta_tester_license_events to authenticated;

revoke all on function public.authorize_beta_tester_device(text, text) from public;
revoke all on function public.authorize_beta_tester_device(text, text) from anon;
grant execute on function public.authorize_beta_tester_device(text, text) to authenticated;
;
