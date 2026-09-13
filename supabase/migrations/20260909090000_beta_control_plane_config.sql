-- Control-plane defaults; safe/idempotent and intentionally not applied remotely here.
create table if not exists public.progression_ranks (
  threshold integer primary key check (threshold >= 0), name text unique not null
);
insert into public.progression_ranks(threshold,name) values
 (0,'Leitor Iniciante'),(50,'Explorador de Balões'),(200,'Caçador de Kanji'),
 (500,'Tradutor do Sekai'),(1000,'Mestre de Capítulos'),(2500,'Guardião das Histórias'),(5000,'Lenda do Sekai')
on conflict (threshold) do update set name=excluded.name;
create table if not exists public.beta_feature_flags (name text primary key, enabled boolean not null);
insert into public.beta_feature_flags values
 ('translation_enabled',true),('community_enabled',true),('billing_enabled',false),
 ('rewarded_ads_enabled',false),('new_signups_enabled',true),('maintenance_mode',false),('auto_update_enabled',true)
on conflict (name) do nothing;
create table if not exists public.beta_remote_config (key text primary key, value jsonb not null);
insert into public.beta_remote_config values
 ('heartbeat_seconds','120'),('daily_yk_target','5'),('minimum_app_version','"0.0.0"')
on conflict (key) do nothing;
alter table public.progression_ranks enable row level security;
alter table public.beta_feature_flags enable row level security;
alter table public.beta_remote_config enable row level security;
create policy progression_ranks_read on public.progression_ranks for select using (true);
create policy beta_flags_read on public.beta_feature_flags for select using (true);
create policy beta_config_read on public.beta_remote_config for select using (true);
revoke insert, update, delete on public.progression_ranks, public.beta_feature_flags, public.beta_remote_config from authenticated;
