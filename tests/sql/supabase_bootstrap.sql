-- Minimal stand-in for what Supabase provides before any project migration runs.
-- This is NOT a project migration: it only recreates the platform surface the real
-- migrations assume already exists (the three PostgREST roles, the auth schema and
-- auth.uid()).  Everything else under test comes from the real migration files.
--
-- auth.uid() reads the same GUC PostgREST sets from the JWT, so a test can
-- impersonate a user with:  set local request.jwt.claim.sub = '<uuid>';

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then
    create role anon nologin noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then
    create role service_role nologin noinherit bypassrls;
  end if;
end $$;

grant usage on schema public to anon, authenticated, service_role;

-- Supabase ships default privileges that hand the public schema to the client
-- roles.  Reproducing them is the whole point: without this the hardening
-- assertions would pass vacuously.
alter default privileges in schema public
  grant all on tables to anon, authenticated, service_role;
alter default privileges in schema public
  grant all on functions to anon, authenticated, service_role;
alter default privileges in schema public
  grant all on sequences to anon, authenticated, service_role;

create schema if not exists auth;
grant usage on schema auth to anon, authenticated, service_role;

create table if not exists auth.users (
  id uuid primary key default gen_random_uuid(),
  email text unique,
  created_at timestamptz not null default now()
);

create or replace function auth.uid() returns uuid
language sql stable
as $$
  select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid;
$$;

grant execute on function auth.uid() to anon, authenticated, service_role;
