-- Persistent public profile identity for community joins.
-- Additive and safe to apply to an existing social schema.
-- RLS remains inherited from public.profiles: authenticated users may read public
-- metadata, while writes remain self-scoped. Object keys are backend-only fields.
-- Rollback (operator-reviewed only): drop the trigger/function, unique index and
-- added constraints, then drop only the added columns; never run automatically.
begin;

alter table public.profiles
    add column if not exists display_name_normalized text,
    add column if not exists avatar_object_key text not null default '',
    add column if not exists banner_object_key text not null default '',
    add column if not exists public_role text not null default '',
    add column if not exists pronouns text not null default '',
    add column if not exists status text not null default 'online',
    add column if not exists status_message text not null default '',
    add column if not exists accent_color text not null default '#c5372c';

update public.profiles
set display_name = nullif(regexp_replace(trim(display_name), '\s+', ' ', 'g'), ''),
    display_name_normalized = nullif(lower(regexp_replace(trim(display_name), '\s+', ' ', 'g')), ''),
    updated_at = now()
where display_name is not null;

create unique index if not exists profiles_display_name_normalized_uq
    on public.profiles (display_name_normalized)
    where deleted_at is null and display_name_normalized is not null;

create or replace function public.normalize_profile_identity()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
declare
    cleaned text;
    folded text;
begin
    cleaned := nullif(regexp_replace(trim(coalesce(new.display_name, '')), '\s+', ' ', 'g'), '');
    folded := lower(cleaned);
    if folded in ('admin', 'administrator', 'moderator', 'support', 'system', 'root', 'official') then
        raise exception using errcode = '23514', message = 'display_name_reserved';
    end if;
    new.display_name := cleaned;
    new.display_name_normalized := folded;
    return new;
end;
$$;

drop trigger if exists profiles_normalize_identity on public.profiles;
create trigger profiles_normalize_identity
before insert or update of display_name, deleted_at on public.profiles
for each row execute function public.normalize_profile_identity();

do $$
begin
    if not exists (select 1 from pg_constraint where conname = 'profiles_status_check') then
        alter table public.profiles add constraint profiles_status_check
            check (status in ('online', 'away', 'busy', 'offline'));
    end if;
    if not exists (select 1 from pg_constraint where conname = 'profiles_public_role_len') then
        alter table public.profiles add constraint profiles_public_role_len
            check (char_length(public_role) <= 80);
    end if;
    if not exists (select 1 from pg_constraint where conname = 'profiles_pronouns_len') then
        alter table public.profiles add constraint profiles_pronouns_len
            check (char_length(pronouns) <= 30);
    end if;
    if not exists (select 1 from pg_constraint where conname = 'profiles_status_message_len') then
        alter table public.profiles add constraint profiles_status_message_len
            check (char_length(status_message) <= 120);
    end if;
    if not exists (select 1 from pg_constraint where conname = 'profiles_accent_color_format') then
        alter table public.profiles add constraint profiles_accent_color_format
            check (accent_color ~ '^#[0-9a-fA-F]{6}$');
    end if;
end;
$$;

commit;
