create or replace function public.redeem_beta_invite(p_code text)
returns jsonb language plpgsql security definer
set search_path = pg_catalog, public as $$
declare v_user uuid:=auth.uid(); v_hash text; v_inv public.beta_invites%rowtype; v_license uuid;
begin
  if v_user is null then raise exception 'auth_required'; end if;
  if nullif(trim(p_code),'') is null then raise exception 'invite_invalid_or_expired'; end if;
  v_hash:=encode(extensions.digest(trim(p_code),'sha256'),'hex');
  select * into v_inv from public.beta_invites where code_hash=v_hash for update;
  if not found or v_inv.status<>'active' or v_inv.expires_at<=now() or v_inv.uses>=v_inv.max_uses then raise exception 'invite_invalid_or_expired'; end if;
  if exists(select 1 from public.licenses where user_id=v_user and status='active' and expires_at>now()) then raise exception 'license_already_active'; end if;
  update public.beta_invites set uses=uses+1,status=case when uses+1>=max_uses then 'exhausted' else status end,updated_at=now() where id=v_inv.id;
  insert into public.licenses(user_id,status,plan_id,starts_at,expires_at,max_devices,created_by)
    values(v_user,'active',v_inv.default_plan_id,now(),now()+make_interval(days=>v_inv.default_license_days),1,v_user) returning id into v_license;
  return jsonb_build_object('license_id',v_license,'status','active','plan_id',v_inv.default_plan_id,'expires_at',now()+make_interval(days=>v_inv.default_license_days));
end $$;
revoke all on function public.redeem_beta_invite(text) from public, anon;
grant execute on function public.redeem_beta_invite(text) to authenticated;
