alter table public.beta_tester_entitlements
  add column if not exists source_license_id uuid null references public.licenses(id) on delete cascade;

drop index if exists public.beta_tester_entitlements_source_license_id_key;
create unique index if not exists beta_tester_entitlements_source_license_id_key
  on public.beta_tester_entitlements(source_license_id)
  where source_license_id is not null;

alter table public.beta_tester_entitlements
  drop constraint if exists beta_tester_entitlements_status_check;

alter table public.beta_tester_entitlements
  add constraint beta_tester_entitlements_status_check
  check (status = any (array['active'::text,'pending'::text,'suspended'::text,'revoked'::text,'expired'::text]));

create or replace function public.sync_beta_entitlement_from_license_update()
returns trigger
language plpgsql
security definer
set search_path to 'pg_catalog','public'
as $function$
begin
  update public.beta_tester_entitlements
     set status = new.status,
         starts_at = new.starts_at,
         expires_at = coalesce(new.expires_at, 'infinity'::timestamptz),
         revoked_at = new.revoked_at,
         revocation_reason = new.revoked_reason,
         max_devices = new.max_devices,
         updated_at = timezone('utc', now())
   where source_license_id = new.id;
  return new;
end;
$function$;

drop trigger if exists trg_sync_beta_entitlement_from_license_update on public.licenses;
create trigger trg_sync_beta_entitlement_from_license_update
after update of status, starts_at, expires_at, max_devices, revoked_at, revoked_reason
on public.licenses
for each row
execute function public.sync_beta_entitlement_from_license_update();

create or replace function public.admin_grant_beta_license(
  p_target_user_id uuid,
  p_duration_days integer default 30,
  p_max_devices integer default 1,
  p_plan_id uuid default null::uuid,
  p_idempotency_key text default null::text
)
returns jsonb
language plpgsql
security definer
set search_path to 'pg_catalog','public'
as $function$
declare
  v_actor uuid := auth.uid();
  v_id uuid;
  v_entitlement_id uuid;
  v_plan uuid;
  v_result jsonb;
  v_starts_at timestamptz := now();
  v_expires_at timestamptz;
begin
  if v_actor is null or not public.is_current_user_dev() then
    raise exception 'forbidden';
  end if;
  if p_duration_days < 1 or p_max_devices < 1 then
    raise exception 'invalid_license_parameters';
  end if;
  if p_idempotency_key is not null then
    select result into v_result
      from public.admin_idempotency_keys
     where key=p_idempotency_key
       and actor=v_actor
       and operation='admin_grant_beta_license';
    if v_result is not null then
      return v_result;
    end if;
  end if;
  if exists(
    select 1 from public.licenses
     where user_id=p_target_user_id
       and status='active'
       and (expires_at is null or expires_at > now())
  ) then
    raise exception 'license_already_active';
  end if;

  v_plan := coalesce(p_plan_id,(select id from public.plans where slug='free' and active limit 1));
  v_expires_at := v_starts_at + make_interval(days=>p_duration_days);

  insert into public.licenses(user_id,status,plan_id,starts_at,expires_at,max_devices,created_by,notes)
  values(p_target_user_id,'active',v_plan,v_starts_at,v_expires_at,p_max_devices,v_actor,'beta_channel=scan-beta')
  returning id into v_id;

  insert into public.beta_tester_entitlements as bte(
    user_id,status,starts_at,expires_at,revoked_at,revocation_reason,max_devices,beta_channel,notes,source_license_id
  )
  values(
    p_target_user_id,'active',v_starts_at,v_expires_at,null,null,p_max_devices,'scan-beta',
    jsonb_build_object('provisioned_by','admin_grant_beta_license'),v_id
  )
  on conflict (user_id,beta_channel) do update
     set status=excluded.status,
         starts_at=excluded.starts_at,
         expires_at=excluded.expires_at,
         revoked_at=null,
         revocation_reason=null,
         max_devices=excluded.max_devices,
         source_license_id=excluded.source_license_id,
         notes=coalesce(bte.notes,'{}'::jsonb) || excluded.notes,
         updated_at=timezone('utc',now())
  returning id into v_entitlement_id;

  insert into public.admin_audit_log(actor,action,target,sanitized_metadata)
  values(v_actor,'grant_beta_license',p_target_user_id::text,
         jsonb_build_object('license_id',v_id,'entitlement_id',v_entitlement_id,'duration_days',p_duration_days,'beta_channel','scan-beta'));

  v_result:=jsonb_build_object(
    'license_id',v_id,
    'entitlement_id',v_entitlement_id,
    'user_id',p_target_user_id,
    'status','active',
    'beta_channel','scan-beta',
    'expires_at',v_expires_at
  );
  if p_idempotency_key is not null then
    insert into public.admin_idempotency_keys(key,actor,operation,result)
    values(p_idempotency_key,v_actor,'admin_grant_beta_license',v_result);
  end if;
  return v_result;
end
$function$;

create or replace function public.redeem_beta_invite(p_code text)
returns jsonb
language plpgsql
security definer
set search_path to 'pg_catalog','public'
as $function$
declare
  v_user uuid:=auth.uid();
  v_hash text;
  v_inv public.beta_invites%rowtype;
  v_license uuid;
  v_entitlement_id uuid;
  v_starts_at timestamptz := now();
  v_expires_at timestamptz;
begin
  if v_user is null then raise exception 'auth_required'; end if;
  if nullif(trim(p_code),'') is null then raise exception 'invite_invalid_or_expired'; end if;
  v_hash:=encode(extensions.digest(trim(p_code),'sha256'),'hex');
  select * into v_inv from public.beta_invites where code_hash=v_hash for update;
  if not found or v_inv.status<>'active' or v_inv.expires_at<=now() or v_inv.uses>=v_inv.max_uses then
    raise exception 'invite_invalid_or_expired';
  end if;
  if exists(select 1 from public.licenses where user_id=v_user and status='active' and (expires_at is null or expires_at>now())) then
    raise exception 'license_already_active';
  end if;

  update public.beta_invites
     set uses=uses+1,
         status=case when uses+1>=max_uses then 'exhausted' else status end,
         updated_at=now()
   where id=v_inv.id;

  v_expires_at := v_starts_at + make_interval(days=>v_inv.default_license_days);

  insert into public.licenses(user_id,status,plan_id,starts_at,expires_at,max_devices,created_by,notes)
  values(v_user,'active',v_inv.default_plan_id,v_starts_at,v_expires_at,1,v_user,'beta_channel=scan-beta')
  returning id into v_license;

  insert into public.beta_tester_entitlements as bte(
    user_id,status,starts_at,expires_at,revoked_at,revocation_reason,max_devices,beta_channel,notes,source_license_id
  )
  values(
    v_user,'active',v_starts_at,v_expires_at,null,null,1,'scan-beta',
    jsonb_build_object('provisioned_by','redeem_beta_invite'),v_license
  )
  on conflict (user_id,beta_channel) do update
     set status=excluded.status,
         starts_at=excluded.starts_at,
         expires_at=excluded.expires_at,
         revoked_at=null,
         revocation_reason=null,
         max_devices=excluded.max_devices,
         source_license_id=excluded.source_license_id,
         notes=coalesce(bte.notes,'{}'::jsonb) || excluded.notes,
         updated_at=timezone('utc',now())
  returning id into v_entitlement_id;

  return jsonb_build_object(
    'license_id',v_license,
    'entitlement_id',v_entitlement_id,
    'status','active',
    'plan_id',v_inv.default_plan_id,
    'beta_channel','scan-beta',
    'expires_at',v_expires_at
  );
end
$function$;

create or replace function public.authorize_beta_tester_device(
  p_device_fingerprint_hash text,
  p_beta_channel text default 'scan-beta'::text
)
returns jsonb
language plpgsql
security definer
set search_path to 'pg_catalog','public'
as $function$
declare
    v_user_id uuid := auth.uid();
    v_checked_at timestamptz := timezone('utc', now());
    v_device_hash text := lower(trim(coalesce(p_device_fingerprint_hash, '')));
    v_channel text := coalesce(nullif(trim(p_beta_channel), ''), 'scan-beta');
    v_entitlement public.beta_tester_entitlements%rowtype;
    v_device public.beta_tester_devices%rowtype;
    v_active_devices integer := 0;
    v_new_device_id uuid;
begin
    if v_user_id is null then
        return jsonb_build_object('allowed',false,'state','auth_required','reason_code','auth_required','checked_at',v_checked_at,'retryable',false);
    end if;
    if v_device_hash !~ '^[a-f0-9]{64}$' then
        return jsonb_build_object('allowed',false,'state','malformed_license','reason_code','malformed_device_fingerprint_hash','user_id',v_user_id,'checked_at',v_checked_at,'retryable',false);
    end if;

    select * into v_entitlement
      from public.beta_tester_entitlements
     where user_id=v_user_id and beta_channel=v_channel
     for update;

    if not found then
        return jsonb_build_object('allowed',false,'state','not_entitled','reason_code','not_entitled','user_id',v_user_id,'device_fingerprint_hash',v_device_hash,'checked_at',v_checked_at,'retryable',false);
    end if;
    if v_entitlement.status='revoked' or v_entitlement.revoked_at is not null then
        return jsonb_build_object('allowed',false,'state','revoked','reason_code','license_revoked','user_id',v_user_id,'device_fingerprint_hash',v_device_hash,'checked_at',v_checked_at,'retryable',false);
    end if;
    if v_entitlement.status='expired' then
        return jsonb_build_object('allowed',false,'state','expired','reason_code','license_expired','user_id',v_user_id,'device_fingerprint_hash',v_device_hash,'expires_at',v_entitlement.expires_at,'checked_at',v_checked_at,'retryable',false);
    end if;
    if v_entitlement.status<>'active' then
        return jsonb_build_object('allowed',false,'state','not_entitled','reason_code','license_not_active','user_id',v_user_id,'device_fingerprint_hash',v_device_hash,'checked_at',v_checked_at,'retryable',false);
    end if;
    if v_entitlement.starts_at is not null and v_entitlement.starts_at>v_checked_at then
        return jsonb_build_object('allowed',false,'state','not_started','reason_code','license_not_started','user_id',v_user_id,'device_fingerprint_hash',v_device_hash,'checked_at',v_checked_at,'retryable',false);
    end if;
    if v_checked_at>=v_entitlement.expires_at then
        return jsonb_build_object('allowed',false,'state','expired','reason_code','license_expired','user_id',v_user_id,'device_fingerprint_hash',v_device_hash,'expires_at',v_entitlement.expires_at,'checked_at',v_checked_at,'retryable',false);
    end if;

    select * into v_device
      from public.beta_tester_devices
     where user_id=v_user_id and device_fingerprint_hash=v_device_hash;

    if found then
        if v_device.revoked_at is not null then
            return jsonb_build_object('allowed',false,'state','device_revoked','reason_code','device_revoked','user_id',v_user_id,'device_fingerprint_hash',v_device_hash,'checked_at',v_checked_at,'retryable',false);
        end if;
        update public.beta_tester_devices set last_seen_at=v_checked_at where id=v_device.id;
        return jsonb_build_object('allowed',true,'state','active','reason_code','active','user_id',v_user_id,'device_id',v_device.id,'device_fingerprint_hash',v_device_hash,'entitlement_id',v_entitlement.id,'expires_at',v_entitlement.expires_at,'checked_at',v_checked_at,'retryable',false);
    end if;

    select count(*) into v_active_devices
      from public.beta_tester_devices
     where user_id=v_user_id and revoked_at is null;

    if v_active_devices>=v_entitlement.max_devices then
        return jsonb_build_object('allowed',false,'state','device_limit_reached','reason_code','device_limit_reached','user_id',v_user_id,'device_fingerprint_hash',v_device_hash,'max_devices',v_entitlement.max_devices,'active_device_count',v_active_devices,'checked_at',v_checked_at,'retryable',false);
    end if;

    insert into public.beta_tester_devices(user_id,device_fingerprint_hash,first_seen_at,last_seen_at)
    values(v_user_id,v_device_hash,v_checked_at,v_checked_at)
    on conflict (user_id,device_fingerprint_hash) do update set last_seen_at=excluded.last_seen_at
    returning id into v_new_device_id;

    insert into public.beta_tester_license_events(user_id,event_type,device_fingerprint_hash,reason,created_at)
    values(v_user_id,'device_register',v_device_hash,'allowed',v_checked_at);

    return jsonb_build_object('allowed',true,'state','active','reason_code','active','user_id',v_user_id,'device_id',v_new_device_id,'device_fingerprint_hash',v_device_hash,'entitlement_id',v_entitlement.id,'expires_at',v_entitlement.expires_at,'checked_at',v_checked_at,'retryable',false);
exception when others then
    return jsonb_build_object('allowed',false,'state','license_unavailable','reason_code','license_service_unavailable','checked_at',v_checked_at,'retryable',true);
end
$function$;;
