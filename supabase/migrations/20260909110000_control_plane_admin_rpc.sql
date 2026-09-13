-- Minimal, server-authoritative local admin surface.  All mutations are audited
-- transactionally and require the caller to be present in staff_members.

create or replace function public.admin_create_beta_invite(
    p_max_uses integer default 1,
    p_expires_at timestamptz default (now() + interval '30 days'),
    p_default_license_days integer default 30,
    p_default_plan_id uuid default null,
    p_idempotency_key text default null
) returns jsonb language plpgsql security definer
set search_path = pg_catalog, public as $$
declare v_actor uuid := auth.uid(); v_code text; v_id uuid; v_hash text;
begin
  if v_actor is null or not public.is_current_user_dev() then raise exception 'forbidden'; end if;
  if p_max_uses < 1 or p_default_license_days < 1 or p_expires_at <= now() then raise exception 'invalid_invite_parameters'; end if;
  if p_idempotency_key is not null then
    select (result->>'id')::uuid into v_id from public.admin_idempotency_keys where key=p_idempotency_key and actor=v_actor and operation='admin_create_beta_invite';
    if v_id is not null then return (select result from public.admin_idempotency_keys where key=p_idempotency_key); end if;
  end if;
  v_code := encode(extensions.gen_random_bytes(24), 'hex'); v_hash := encode(extensions.digest(v_code, 'sha256'), 'hex');
  insert into public.beta_invites(code_hash,created_by,max_uses,expires_at,default_license_days,default_plan_id)
  values(v_hash,v_actor,p_max_uses,p_expires_at,p_default_license_days,p_default_plan_id) returning id into v_id;
  insert into public.admin_audit_log(actor,action,target,sanitized_metadata) values(v_actor,'create_beta_invite',v_id::text,jsonb_build_object('max_uses',p_max_uses));
  if p_idempotency_key is not null then insert into public.admin_idempotency_keys(key,actor,operation,result) values(p_idempotency_key,v_actor,'admin_create_beta_invite',jsonb_build_object('id',v_id,'code',v_code)); end if;
  return jsonb_build_object('id',v_id,'code',v_code,'expires_at',p_expires_at);
end $$;
create or replace function public.admin_grant_beta_license(
    p_target_user_id uuid, p_duration_days integer default 30, p_max_devices integer default 1,
    p_plan_id uuid default null, p_idempotency_key text default null
) returns jsonb language plpgsql security definer
set search_path = pg_catalog, public as $$
declare v_actor uuid := auth.uid(); v_id uuid; v_plan uuid; v_result jsonb;
begin
  if v_actor is null or not public.is_current_user_dev() then raise exception 'forbidden'; end if;
  if p_duration_days < 1 or p_max_devices < 1 then raise exception 'invalid_license_parameters'; end if;
  if p_idempotency_key is not null then select result into v_result from public.admin_idempotency_keys where key=p_idempotency_key and actor=v_actor and operation='admin_grant_beta_license'; if v_result is not null then return v_result; end if; end if;
  if exists(select 1 from public.licenses where user_id=p_target_user_id and status='active' and expires_at > now()) then raise exception 'license_already_active'; end if;
  v_plan := coalesce(p_plan_id,(select id from public.plans where slug='free' and active limit 1));
  insert into public.licenses(user_id,status,plan_id,starts_at,expires_at,max_devices,created_by) values(p_target_user_id,'active',v_plan,now(),now()+make_interval(days=>p_duration_days),p_max_devices,v_actor) returning id into v_id;
  insert into public.admin_audit_log(actor,action,target,sanitized_metadata) values(v_actor,'grant_beta_license',p_target_user_id::text,jsonb_build_object('license_id',v_id,'duration_days',p_duration_days));
  v_result:=jsonb_build_object('license_id',v_id,'user_id',p_target_user_id,'status','active');
  if p_idempotency_key is not null then insert into public.admin_idempotency_keys(key,actor,operation,result) values(p_idempotency_key,v_actor,'admin_grant_beta_license',v_result); end if;
  return v_result;
end $$;
create or replace function public.admin_grant_yk(p_target_user_id uuid,p_amount integer,p_reason text,p_idempotency_key text default null)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare v_actor uuid:=auth.uid(); v_result jsonb; v_id uuid;
begin
  if v_actor is null or not public.is_current_user_dev() then raise exception 'forbidden'; end if;
  if p_amount <= 0 or nullif(trim(p_reason),'') is null then raise exception 'invalid_yk_parameters'; end if;
  if p_idempotency_key is not null then select result into v_result from public.admin_idempotency_keys where key=p_idempotency_key and actor=v_actor and operation='admin_grant_yk'; if v_result is not null then return v_result; end if; end if;
  insert into public.yk_ledger(user_id,amount,bucket,event_type,reference_id,metadata_sanitized) values(p_target_user_id,p_amount,'permanent','admin_grant',coalesce(p_idempotency_key,gen_random_uuid()::text),jsonb_build_object('reason',left(p_reason,200),'actor',v_actor)) returning id into v_id;
  insert into public.admin_audit_log(actor,action,target,sanitized_metadata) values(v_actor,'grant_yk',p_target_user_id::text,jsonb_build_object('amount',p_amount,'reason',left(p_reason,200)));
  v_result:=jsonb_build_object('ledger_id',v_id,'amount',p_amount);
  if p_idempotency_key is not null then insert into public.admin_idempotency_keys(key,actor,operation,result) values(p_idempotency_key,v_actor,'admin_grant_yk',v_result); end if;
  return v_result;
end $$;
create or replace function public.admin_revoke_license(p_license_id uuid,p_reason text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare v_actor uuid:=auth.uid(); v_user uuid;
begin
  if v_actor is null or not public.is_current_user_dev() then raise exception 'forbidden'; end if;
  if nullif(trim(p_reason),'') is null then raise exception 'reason_required'; end if;
  update public.licenses set status='revoked',revoked_at=now(),revoked_reason=left(p_reason,200),updated_at=now() where id=p_license_id and status <> 'revoked' returning user_id into v_user;
  if v_user is null then raise exception 'license_not_found_or_already_revoked'; end if;
  update public.license_sessions set status='revoked',ended_at=now(),updated_at=now() where license_id=p_license_id and status='active';
  insert into public.admin_audit_log(actor,action,target,sanitized_metadata) values(v_actor,'revoke_license',p_license_id::text,jsonb_build_object('reason',left(p_reason,200)));
  return jsonb_build_object('license_id',p_license_id,'status','revoked');
end $$;
revoke all on function public.admin_create_beta_invite(integer,timestamptz,integer,uuid,text), public.admin_grant_beta_license(uuid,integer,integer,uuid,text), public.admin_grant_yk(uuid,integer,text,text), public.admin_revoke_license(uuid,text) from public, anon, authenticated;
grant execute on function public.admin_create_beta_invite(integer,timestamptz,integer,uuid,text), public.admin_grant_beta_license(uuid,integer,integer,uuid,text), public.admin_grant_yk(uuid,integer,text,text), public.admin_revoke_license(uuid,text) to authenticated;
