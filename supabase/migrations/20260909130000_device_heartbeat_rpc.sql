create or replace function public.register_device(p_device_id text,p_public_key text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare v_user uuid:=auth.uid(); v_license public.licenses%rowtype; v_id uuid; v_count integer;
begin
  if v_user is null then raise exception 'unauthorized'; end if;
  if nullif(trim(p_device_id),'') is null or nullif(trim(p_public_key),'') is null then raise exception 'invalid_device'; end if;
  select * into v_license from public.licenses where user_id=v_user and status='active' and (expires_at is null or expires_at>now()) order by expires_at desc nulls last limit 1;
  if not found then raise exception 'license_not_found'; end if;
  select id into v_id from public.license_devices where license_id=v_license.id and user_id=v_user and device_id=trim(p_device_id);
  if v_id is null then
    select count(*) into v_count from public.license_devices where license_id=v_license.id and user_id=v_user and status='active';
    if v_count >= v_license.max_devices then raise exception 'device_limit_reached'; end if;
    insert into public.license_devices(license_id,user_id,device_id,public_key,status,last_seen_at) values(v_license.id,v_user,trim(p_device_id),trim(p_public_key),'active',now()) returning id into v_id;
  else update public.license_devices set public_key=trim(p_public_key),status='active',last_seen_at=now() where id=v_id;
  end if;
  return jsonb_build_object('device_id',v_id,'license_id',v_license.id,'status','active');
end $$;
create or replace function public.create_device_challenge(p_device_uuid uuid,p_nonce_hash text,p_ttl_seconds integer default 60)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare v_user uuid:=auth.uid(); v_id uuid; v_exp timestamptz;
begin
  if v_user is null then raise exception 'unauthorized'; end if;
  if p_ttl_seconds not between 15 and 300 or nullif(trim(p_nonce_hash),'') is null then raise exception 'invalid_challenge'; end if;
  if not exists(select 1 from public.license_devices d join public.licenses l on l.id=d.license_id where d.id=p_device_uuid and d.user_id=v_user and d.status='active' and l.status='active' and (l.expires_at is null or l.expires_at>now())) then raise exception 'device_not_found'; end if;
  v_exp:=now()+make_interval(secs=>p_ttl_seconds);
  insert into public.device_challenges(user_id,device_id,nonce_hash,expires_at) values(v_user,p_device_uuid,trim(p_nonce_hash),v_exp) returning id into v_id;
  return jsonb_build_object('challenge_id',v_id,'expires_at',v_exp);
end $$;
create or replace function public.device_heartbeat(p_device_uuid uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare v_user uuid:=auth.uid(); v_license uuid; v_session uuid; v_now timestamptz:=now();
begin
  if v_user is null then raise exception 'unauthorized'; end if;
  select l.id into v_license from public.license_devices d join public.licenses l on l.id=d.license_id where d.id=p_device_uuid and d.user_id=v_user and d.status='active' and l.status='active' and (l.expires_at is null or l.expires_at>v_now);
  if v_license is null then raise exception 'license_revoked'; end if;
  update public.license_devices set last_seen_at=v_now where id=p_device_uuid and user_id=v_user;
  select id into v_session from public.license_sessions where license_id=v_license and device_id=p_device_uuid and status='active' limit 1;
  if v_session is null then insert into public.license_sessions(license_id,user_id,device_id,status,started_at,last_heartbeat_at) values(v_license,v_user,p_device_uuid,'active',v_now,v_now) returning id into v_session; else update public.license_sessions set last_heartbeat_at=v_now,updated_at=v_now where id=v_session; end if;
  return jsonb_build_object('licensed',true,'license_id',v_license,'device_id',p_device_uuid,'session_id',v_session,'server_time',v_now);
end $$;
revoke all on function public.register_device(text,text),public.create_device_challenge(uuid,text,integer),public.device_heartbeat(uuid) from public,anon;
grant execute on function public.register_device(text,text),public.create_device_challenge(uuid,text,integer),public.device_heartbeat(uuid) to authenticated;
