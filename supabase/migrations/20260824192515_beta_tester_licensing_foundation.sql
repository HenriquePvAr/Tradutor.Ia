-- Scan Beta tester licensing foundation.
--
-- Local migration contract only in TDD #77. Do not apply remotely until the
-- controlled Supabase licensing integration phase.

create table if not exists public.beta_tester_entitlements (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    status text not null check (status in ('active', 'pending', 'suspended', 'revoked')),
    starts_at timestamptz,
    expires_at timestamptz not null,
    revoked_at timestamptz,
    revocation_reason text,
    max_devices integer not null default 1 check (max_devices >= 1),
    beta_channel text not null default 'scan-beta',
    notes jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now()),
    unique (user_id, beta_channel)
);

create table if not exists public.beta_tester_devices (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    device_fingerprint_hash text not null,
    device_label text,
    first_seen_at timestamptz not null default timezone('utc', now()),
    last_seen_at timestamptz not null default timezone('utc', now()),
    revoked_at timestamptz,
    created_at timestamptz not null default timezone('utc', now()),
    unique (user_id, device_fingerprint_hash),
    check (length(device_fingerprint_hash) between 32 and 128)
);

create table if not exists public.beta_tester_license_events (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    event_type text not null,
    device_fingerprint_hash text,
    reason text,
    created_at timestamptz not null default timezone('utc', now())
);

alter table public.beta_tester_entitlements enable row level security;
alter table public.beta_tester_devices enable row level security;
alter table public.beta_tester_license_events enable row level security;

-- Authenticated testers may inspect their own entitlement/device state only.
-- They cannot grant, extend, revoke, or increase device limits from the client.
create policy "tester can read own beta entitlement"
on public.beta_tester_entitlements
for select
to authenticated
using ((select auth.uid()) = user_id);

create policy "tester can read own beta devices"
on public.beta_tester_devices
for select
to authenticated
using ((select auth.uid()) = user_id);

create policy "tester can read own beta license events"
on public.beta_tester_license_events
for select
to authenticated
using ((select auth.uid()) = user_id);
;
