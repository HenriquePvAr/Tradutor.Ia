-- Tracking migration (schema-as-code / source of truth).
-- As RPCs admin_extend_beta_license e admin_revoke_device JÁ ESTÃO aplicadas no
-- projeto remoto de produção (verificadas idênticas a este SQL via pg_get_functiondef).
-- Este arquivo existe apenas para rastreabilidade; é idempotente (create or replace).
-- NÃO aplicar batch de migrations pendentes com supabase db push.

-- Preparação local; não aplicar remotamente sem autorização.
-- Objetos alterados: somente as funções abaixo.

create or replace function public.admin_extend_beta_license(
  p_license_id uuid, p_new_expires_at timestamptz, p_idempotency_key text default null
) returns jsonb language plpgsql security definer
set search_path = pg_catalog, public as $$
declare v_actor uuid := auth.uid(); v_user uuid; v_status text; v_result jsonb;
begin
  if v_actor is null or not public.is_current_user_dev() then raise exception 'forbidden'; end if;
  if p_new_expires_at <= now() then raise exception 'invalid_expiration'; end if;
  if p_idempotency_key is not null then
    select result into v_result from public.admin_idempotency_keys where key=p_idempotency_key and actor=v_actor and operation='admin_extend_beta_license';
    if v_result is not null then return v_result; end if;
  end if;
  select user_id, status into v_user, v_status from public.licenses where id=p_license_id for update;
  if v_user is null or v_status in ('revoked','expired') then raise exception 'license_not_extendable'; end if;
  update public.licenses set expires_at=p_new_expires_at, status='active', updated_at=now() where id=p_license_id;
  insert into public.admin_audit_log(actor,action,target,sanitized_metadata) values(v_actor,'extend_beta_license',p_license_id::text,jsonb_build_object('new_expires_at',p_new_expires_at));
  v_result := jsonb_build_object('license_id',p_license_id,'user_id',v_user,'status','active','expires_at',p_new_expires_at);
  if p_idempotency_key is not null then insert into public.admin_idempotency_keys(key,actor,operation,result) values(p_idempotency_key,v_actor,'admin_extend_beta_license',v_result); end if;
  return v_result;
end $$;

create or replace function public.admin_revoke_device(
  p_device_id uuid, p_reason text
) returns jsonb language plpgsql security definer
set search_path = pg_catalog, public as $$
declare v_actor uuid := auth.uid(); v_user uuid; v_status text;
begin
  if v_actor is null or not public.is_current_user_dev() then raise exception 'forbidden'; end if;
  if nullif(trim(p_reason),'') is null then raise exception 'reason_required'; end if;
  select user_id,status into v_user,v_status from public.license_devices where id=p_device_id for update;
  if v_user is null then raise exception 'device_not_found'; end if;
  if v_status = 'revoked' then return jsonb_build_object('device_id',p_device_id,'status','revoked','idempotent',true); end if;
  update public.license_devices set status='revoked' where id=p_device_id;
  update public.license_sessions set status='revoked', ended_at=now(), updated_at=now() where device_id=p_device_id and status='active';
  insert into public.admin_audit_log(actor,action,target,sanitized_metadata) values(v_actor,'revoke_device',p_device_id::text,jsonb_build_object('reason',left(p_reason,200),'user_id',v_user));
  return jsonb_build_object('device_id',p_device_id,'user_id',v_user,'status','revoked');
end $$;

revoke all on function public.admin_extend_beta_license(uuid,timestamptz,text), public.admin_revoke_device(uuid,text) from public, anon, authenticated;
grant execute on function public.admin_extend_beta_license(uuid,timestamptz,text), public.admin_revoke_device(uuid,text) to authenticated;
