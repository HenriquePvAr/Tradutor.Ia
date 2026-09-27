-- Canonical device revocation policy: a revoked identity is terminal until
-- an explicit administrative re-authorisation contract exists.
create or replace function public.register_device(p_device_id text,p_public_key text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare v_user uuid:=auth.uid(); v_license public.licenses%rowtype; v_id uuid; v_count integer; v_status text;
begin
  if v_user is null then raise exception 'unauthorized'; end if;
  if nullif(trim(p_device_id),'') is null or nullif(trim(p_public_key),'') is null then raise exception 'invalid_device'; end if;
  select * into v_license from public.licenses where user_id=v_user and status='active' and (expires_at is null or expires_at>now()) order by expires_at desc nulls last limit 1;
  if not found then raise exception 'license_not_found'; end if;
  select id,status into v_id,v_status from public.license_devices where license_id=v_license.id and user_id=v_user and device_id=trim(p_device_id);
  if v_id is not null and v_status='revoked' then raise exception 'device_revoked'; end if;
  if v_id is null then
    select count(*) into v_count from public.license_devices where license_id=v_license.id and user_id=v_user and status='active';
    if v_count >= v_license.max_devices then raise exception 'device_limit_reached'; end if;
    insert into public.license_devices(license_id,user_id,device_id,public_key,status,last_seen_at) values(v_license.id,v_user,trim(p_device_id),trim(p_public_key),'active',now()) returning id into v_id;
  else update public.license_devices set public_key=trim(p_public_key),last_seen_at=now() where id=v_id;
  end if;
  return jsonb_build_object('device_id',v_id,'license_id',v_license.id,'status','active');
end $$;
revoke all on function public.register_device(text,text) from public,anon;
grant execute on function public.register_device(text,text) to authenticated;
