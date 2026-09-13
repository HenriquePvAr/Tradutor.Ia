-- Atomic Scan Beta authorization RPC.
--
-- Applies after 20260824120000_beta_tester_licensing_foundation.sql.
-- The function derives the user from auth.uid(), uses database time, serializes
-- same-user device allocation by locking the entitlement row, and never trusts a
-- client-provided user id, entitlement status, expiry, or max device count.

create index if not exists beta_tester_entitlements_user_channel_idx
on public.beta_tester_entitlements (user_id, beta_channel);

create index if not exists beta_tester_devices_user_hash_idx
on public.beta_tester_devices (user_id, device_fingerprint_hash);

create index if not exists beta_tester_devices_active_count_idx
on public.beta_tester_devices (user_id)
where revoked_at is null;

create or replace function public.authorize_beta_tester_device(
    p_device_fingerprint_hash text,
    p_beta_channel text default 'scan-beta'
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
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
        return jsonb_build_object(
            'allowed', false,
            'state', 'auth_required',
            'reason_code', 'auth_required',
            'checked_at', v_checked_at,
            'retryable', false
        );
    end if;

    if v_device_hash !~ '^[a-f0-9]{64}$' then
        return jsonb_build_object(
            'allowed', false,
            'state', 'malformed_license',
            'reason_code', 'malformed_device_fingerprint_hash',
            'user_id', v_user_id,
            'checked_at', v_checked_at,
            'retryable', false
        );
    end if;

    select *
      into v_entitlement
      from public.beta_tester_entitlements
     where user_id = v_user_id
       and beta_channel = v_channel
     for update;

    if not found then
        return jsonb_build_object(
            'allowed', false,
            'state', 'not_entitled',
            'reason_code', 'not_entitled',
            'user_id', v_user_id,
            'device_fingerprint_hash', v_device_hash,
            'checked_at', v_checked_at,
            'retryable', false
        );
    end if;

    if v_entitlement.status = 'revoked' or v_entitlement.revoked_at is not null then
        return jsonb_build_object(
            'allowed', false,
            'state', 'revoked',
            'reason_code', 'license_revoked',
            'user_id', v_user_id,
            'device_fingerprint_hash', v_device_hash,
            'checked_at', v_checked_at,
            'retryable', false
        );
    end if;

    if v_entitlement.status <> 'active' then
        return jsonb_build_object(
            'allowed', false,
            'state', 'not_entitled',
            'reason_code', 'license_not_active',
            'user_id', v_user_id,
            'device_fingerprint_hash', v_device_hash,
            'checked_at', v_checked_at,
            'retryable', false
        );
    end if;

    if v_entitlement.starts_at is not null and v_entitlement.starts_at > v_checked_at then
        return jsonb_build_object(
            'allowed', false,
            'state', 'not_started',
            'reason_code', 'license_not_started',
            'user_id', v_user_id,
            'device_fingerprint_hash', v_device_hash,
            'checked_at', v_checked_at,
            'retryable', false
        );
    end if;

    if v_checked_at >= v_entitlement.expires_at then
        return jsonb_build_object(
            'allowed', false,
            'state', 'expired',
            'reason_code', 'license_expired',
            'user_id', v_user_id,
            'device_fingerprint_hash', v_device_hash,
            'expires_at', v_entitlement.expires_at,
            'checked_at', v_checked_at,
            'retryable', false
        );
    end if;

    select *
      into v_device
      from public.beta_tester_devices
     where user_id = v_user_id
       and device_fingerprint_hash = v_device_hash;

    if found then
        if v_device.revoked_at is not null then
            return jsonb_build_object(
                'allowed', false,
                'state', 'device_revoked',
                'reason_code', 'device_revoked',
                'user_id', v_user_id,
                'device_fingerprint_hash', v_device_hash,
                'checked_at', v_checked_at,
                'retryable', false
            );
        end if;

        update public.beta_tester_devices
           set last_seen_at = v_checked_at
         where id = v_device.id;

        return jsonb_build_object(
            'allowed', true,
            'state', 'active',
            'reason_code', 'active',
            'user_id', v_user_id,
            'device_id', v_device.id,
            'device_fingerprint_hash', v_device_hash,
            'entitlement_id', v_entitlement.id,
            'expires_at', v_entitlement.expires_at,
            'checked_at', v_checked_at,
            'retryable', false
        );
    end if;

    select count(*)
      into v_active_devices
      from public.beta_tester_devices
     where user_id = v_user_id
       and revoked_at is null;

    if v_active_devices >= v_entitlement.max_devices then
        return jsonb_build_object(
            'allowed', false,
            'state', 'device_limit_reached',
            'reason_code', 'device_limit_reached',
            'user_id', v_user_id,
            'device_fingerprint_hash', v_device_hash,
            'max_devices', v_entitlement.max_devices,
            'active_device_count', v_active_devices,
            'checked_at', v_checked_at,
            'retryable', false
        );
    end if;

    insert into public.beta_tester_devices (
        user_id,
        device_fingerprint_hash,
        first_seen_at,
        last_seen_at
    )
    values (
        v_user_id,
        v_device_hash,
        v_checked_at,
        v_checked_at
    )
    on conflict (user_id, device_fingerprint_hash) do update
        set last_seen_at = excluded.last_seen_at
    returning id into v_new_device_id;

    insert into public.beta_tester_license_events (
        user_id,
        event_type,
        device_fingerprint_hash,
        reason,
        created_at
    )
    values (
        v_user_id,
        'device_register',
        v_device_hash,
        'allowed',
        v_checked_at
    );

    return jsonb_build_object(
        'allowed', true,
        'state', 'active',
        'reason_code', 'active',
        'user_id', v_user_id,
        'device_id', v_new_device_id,
        'device_fingerprint_hash', v_device_hash,
        'entitlement_id', v_entitlement.id,
        'expires_at', v_entitlement.expires_at,
        'checked_at', v_checked_at,
        'retryable', false
    );
exception
    when others then
        return jsonb_build_object(
            'allowed', false,
            'state', 'license_unavailable',
            'reason_code', 'license_service_unavailable',
            'checked_at', v_checked_at,
            'retryable', true
        );
end;
$$;

revoke all on function public.authorize_beta_tester_device(text, text) from public;
revoke all on function public.authorize_beta_tester_device(text, text) from anon;
grant execute on function public.authorize_beta_tester_device(text, text) to authenticated;

revoke insert, update, delete on public.beta_tester_entitlements from anon, authenticated;
revoke insert, update, delete on public.beta_tester_devices from anon, authenticated;
revoke insert, update, delete on public.beta_tester_license_events from anon, authenticated;
revoke all on public.beta_tester_entitlements from anon;
revoke all on public.beta_tester_devices from anon;
revoke all on public.beta_tester_license_events from anon;
grant select on public.beta_tester_entitlements to authenticated;
grant select on public.beta_tester_devices to authenticated;
grant select on public.beta_tester_license_events to authenticated;
;
