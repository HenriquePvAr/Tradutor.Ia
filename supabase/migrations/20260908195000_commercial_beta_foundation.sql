-- Commercial beta foundation. Apply remotely only after review/approval.
create table if not exists public.plans (
  id uuid primary key default gen_random_uuid(), slug text unique not null,
  name text not null, active boolean not null default true, ad_free boolean not null default false,
  rewarded_ads_enabled boolean not null default false, translation_access_mode text not null default 'yk',
  daily_yk_target integer not null default 5, included_yk_per_cycle integer not null default 0,
  yk_cost_per_translation integer not null default 1, monthly_price integer, annual_price integer,
  currency text not null default 'BRL', max_devices_default integer not null default 1,
  features jsonb not null default '{}'::jsonb, created_at timestamptz not null default now(),
  check (translation_access_mode in ('yk','quota','unlimited')),
  check (daily_yk_target >= 0 and yk_cost_per_translation > 0)
);
insert into public.plans (slug,name,ad_free,rewarded_ads_enabled,translation_access_mode,daily_yk_target,yk_cost_per_translation)
values ('free','Free',false,true,'yk',5,1), ('pro_monthly','Pro mensal',true,false,'quota',0,1), ('pro_annual','Pro anual',true,false,'quota',0,1)
on conflict (slug) do nothing;
create table if not exists public.licenses (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  status text not null default 'pending' check (status in ('pending','active','suspended','revoked','expired')),
  plan_id uuid references public.plans(id), starts_at timestamptz not null default now(), expires_at timestamptz,
  max_devices integer not null default 1, offline_grace_seconds integer not null default 0,
  revoked_at timestamptz, revoked_reason text, created_by uuid, notes text, created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
create index if not exists licenses_user_idx on public.licenses(user_id);
create table if not exists public.license_devices (
  id uuid primary key default gen_random_uuid(), license_id uuid not null references public.licenses(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade, device_id text not null, public_key text,
  status text not null default 'active' check(status in ('active','revoked')), created_at timestamptz not null default now(), last_seen_at timestamptz,
  unique(license_id,device_id)
);
create table if not exists public.subscriptions (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null, provider_customer_id text, provider_subscription_id text unique, plan_id uuid references public.plans(id), status text not null,
  current_period_start timestamptz, current_period_end timestamptz, cancel_at_period_end boolean not null default false, canceled_at timestamptz, created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
create table if not exists public.yk_ledger (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  amount integer not null, bucket text not null check(bucket in ('daily','subscription','permanent')), event_type text not null,
  reference_id text, expires_at timestamptz, created_at timestamptz not null default now(), metadata_sanitized jsonb not null default '{}'::jsonb
);
create table if not exists public.yk_daily_claims (user_id uuid not null references auth.users(id) on delete cascade, claim_day date not null, amount integer not null, created_at timestamptz not null default now(), primary key(user_id,claim_day));
create table if not exists public.yk_reservations (id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade, job_id text unique not null, amount integer not null, status text not null check(status in ('reserved','consumed','released')), created_at timestamptz not null default now(), updated_at timestamptz not null default now());
create table if not exists public.ad_reward_events (id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade, provider text not null, provider_event_id text unique not null, amount integer not null, verified_at timestamptz not null default now(), metadata_sanitized jsonb not null default '{}'::jsonb);
create table if not exists public.admin_audit_log (id uuid primary key default gen_random_uuid(), actor uuid not null references auth.users(id), action text not null, target text not null, created_at timestamptz not null default now(), sanitized_metadata jsonb not null default '{}'::jsonb);
alter table public.plans enable row level security;
alter table public.licenses enable row level security;
alter table public.license_devices enable row level security;
alter table public.subscriptions enable row level security;
alter table public.yk_ledger enable row level security;
alter table public.yk_daily_claims enable row level security;
alter table public.yk_reservations enable row level security;
alter table public.ad_reward_events enable row level security;
alter table public.admin_audit_log enable row level security;
create policy plans_read_active on public.plans for select using (active = true);
create policy licenses_self_read on public.licenses for select using (auth.uid() = user_id);
create policy devices_self_read on public.license_devices for select using (auth.uid() = user_id);
create policy subscriptions_self_read on public.subscriptions for select using (auth.uid() = user_id);
create policy ledger_self_read on public.yk_ledger for select using (auth.uid() = user_id);
create policy reservations_self_read on public.yk_reservations for select using (auth.uid() = user_id);
revoke insert, update, delete on public.plans, public.licenses, public.license_devices, public.subscriptions, public.yk_ledger, public.yk_daily_claims, public.yk_reservations, public.ad_reward_events, public.admin_audit_log from authenticated;
