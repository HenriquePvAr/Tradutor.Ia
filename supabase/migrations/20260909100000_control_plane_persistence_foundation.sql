-- Local control-plane persistence foundation.  No remote deployment is performed here.
-- Authority for privileged mutations is added only by a later, tested RPC migration.

create table if not exists public.staff_members (
    user_id uuid primary key references auth.users(id) on delete cascade,
    role text not null check (role in ('dev')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    created_by uuid references auth.users(id)
);
create table if not exists public.beta_invites (
    id uuid primary key default gen_random_uuid(),
    code_hash text not null unique,
    created_by uuid not null references auth.users(id),
    status text not null default 'active' check (status in ('active','revoked','exhausted','expired')),
    max_uses integer not null check (max_uses >= 1),
    uses integer not null default 0 check (uses >= 0 and uses <= max_uses),
    expires_at timestamptz not null,
    default_license_days integer not null default 30 check (default_license_days > 0),
    default_plan_id uuid references public.plans(id),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    revoked_at timestamptz,
    metadata jsonb
);
create index if not exists beta_invites_status_expires_idx on public.beta_invites(status, expires_at);
create table if not exists public.device_challenges (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    device_id uuid not null references public.license_devices(id) on delete cascade,
    nonce_hash text not null unique,
    expires_at timestamptz not null,
    consumed_at timestamptz,
    created_at timestamptz not null default now()
);
create index if not exists device_challenges_active_idx on public.device_challenges(user_id, device_id, expires_at) where consumed_at is null;
create table if not exists public.license_sessions (
    id uuid primary key default gen_random_uuid(),
    license_id uuid not null references public.licenses(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    device_id uuid not null references public.license_devices(id) on delete cascade,
    status text not null default 'active' check (status in ('active','ended','revoked')),
    started_at timestamptz not null default now(),
    last_heartbeat_at timestamptz not null default now(),
    ended_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create unique index if not exists license_sessions_active_ctx_idx on public.license_sessions(license_id, device_id) where status = 'active';
create table if not exists public.progression_ledger (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    amount integer not null,
    event_type text not null,
    reference_id text not null,
    created_at timestamptz not null default now(),
    metadata jsonb,
    unique(user_id, event_type, reference_id)
);
create table if not exists public.translation_requests (
    id uuid primary key default gen_random_uuid(),
    request_id text not null unique,
    user_id uuid not null references auth.users(id) on delete cascade,
    license_id uuid not null references public.licenses(id),
    device_id uuid not null references public.license_devices(id),
    job_id text,
    reservation_id uuid references public.yk_reservations(id),
    provider text not null,
    status text not null default 'pending' check (status in ('pending','processing','completed','failed')),
    character_count integer not null default 0 check (character_count >= 0),
    created_at timestamptz not null default now(),
    started_at timestamptz,
    completed_at timestamptz,
    failed_at timestamptz,
    error_code text
);
create table if not exists public.admin_idempotency_keys (
    key text primary key,
    actor uuid not null references auth.users(id) on delete cascade,
    operation text not null,
    result jsonb,
    created_at timestamptz not null default now()
);
alter table public.staff_members enable row level security;
alter table public.beta_invites enable row level security;
alter table public.device_challenges enable row level security;
alter table public.license_sessions enable row level security;
alter table public.progression_ledger enable row level security;
alter table public.translation_requests enable row level security;
alter table public.admin_idempotency_keys enable row level security;
revoke all on public.staff_members, public.beta_invites, public.device_challenges,
    public.license_sessions, public.progression_ledger, public.translation_requests,
    public.admin_idempotency_keys from anon, authenticated;
create or replace function public.is_current_user_dev()
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
    select exists (
        select 1 from public.staff_members
        where user_id = auth.uid() and role = 'dev'
    );
$$;
revoke all on function public.is_current_user_dev() from public, anon, authenticated;
