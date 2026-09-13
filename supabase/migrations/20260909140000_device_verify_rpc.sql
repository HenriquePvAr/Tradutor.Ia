create or replace function public.get_device_challenge_for_verify(p_challenge_id uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare v_user uuid:=auth.uid(); v jsonb;
begin
  if v_user is null then raise exception 'forbidden'; end if;
  select jsonb_build_object('id',c.id,'user_id',c.user_id,'device_id',c.device_id,'nonce_hash',c.nonce_hash,'expires_at',c.expires_at,'consumed_at',c.consumed_at,'public_key',d.public_key,'device_status',d.status,'license_status',l.status,'license_expires_at',l.expires_at) into v from public.device_challenges c join public.license_devices d on d.id=c.device_id join public.licenses l on l.id=d.license_id where c.id=p_challenge_id and c.user_id=v_user;
  if v is null then raise exception 'device_challenge_not_found'; end if; return v;
end $$;
create or replace function public.consume_device_challenge(p_challenge_id uuid,p_nonce_hash text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare v_user uuid:=auth.uid(); v_id uuid;
begin
  if v_user is null then raise exception 'forbidden'; end if;
  update public.device_challenges set consumed_at=now() where id=p_challenge_id and user_id=v_user and consumed_at is null and expires_at>now() and nonce_hash=p_nonce_hash returning id into v_id;
  if v_id is null then if exists(select 1 from public.device_challenges where id=p_challenge_id and user_id=v_user and consumed_at is not null) then raise exception 'device_challenge_consumed'; elsif exists(select 1 from public.device_challenges where id=p_challenge_id and user_id=v_user and expires_at<=now()) then raise exception 'device_challenge_expired'; else raise exception 'device_challenge_invalid'; end if; end if;
  return jsonb_build_object('challenge_id',v_id,'consumed',true);
end $$;
revoke all on function public.get_device_challenge_for_verify(uuid),public.consume_device_challenge(uuid,text) from public,anon;
grant execute on function public.get_device_challenge_for_verify(uuid),public.consume_device_challenge(uuid,text) to authenticated;
