create or replace function public.begin_translation_request(p_request_id text, p_device_id uuid, p_job_id text default null, p_character_count integer default 0)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare u uuid:=auth.uid(); l uuid; r uuid; existing jsonb;
begin
  if u is null then raise exception 'unauthorized'; end if;
  if nullif(trim(p_request_id),'') is null or p_character_count < 0 then raise exception 'invalid_translation_request'; end if;
  select id into l from public.licenses where user_id=u and status='active' and (expires_at is null or expires_at>now()) order by expires_at desc nulls last limit 1;
  if l is null then raise exception 'license_not_found'; end if;
  if not exists(select 1 from public.license_devices where id=p_device_id and user_id=u and license_id=l and status='active') then raise exception 'device_not_found'; end if;
  select jsonb_build_object('request_id',request_id,'status',status,'provider',provider,'reservation_id',reservation_id) into existing from public.translation_requests where request_id=trim(p_request_id) and user_id=u;
  if existing is not null then return existing || jsonb_build_object('idempotent',true); end if;
  insert into public.translation_requests(request_id,user_id,license_id,device_id,job_id,provider,status,character_count,started_at)
    values(trim(p_request_id),u,l,p_device_id,nullif(trim(p_job_id),''),'mock', 'processing',p_character_count,now()) returning id into r;
  return jsonb_build_object('id',r,'request_id',trim(p_request_id),'status','processing','provider','mock','idempotent',false);
end $$;
create or replace function public.complete_translation_request(p_request_id text) returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare u uuid:=auth.uid(); s text;
begin
  if u is null then raise exception 'unauthorized'; end if;
  select status into s from public.translation_requests where request_id=trim(p_request_id) and user_id=u for update;
  if s is null then raise exception 'translation_not_found'; end if;
  if s='completed' then return jsonb_build_object('request_id',trim(p_request_id),'status',s,'idempotent',true); end if;
  if s<>'processing' then raise exception 'translation_not_processing'; end if;
  update public.translation_requests set status='completed',completed_at=now() where request_id=trim(p_request_id) and user_id=u;
  perform public.award_translation_xp(trim(p_request_id),10);
  return jsonb_build_object('request_id',trim(p_request_id),'status','completed','idempotent',false);
end $$;
create or replace function public.fail_translation_request(p_request_id text,p_error_code text default 'provider_failed') returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare u uuid:=auth.uid(); s text;
begin
  if u is null then raise exception 'unauthorized'; end if;
  select status into s from public.translation_requests where request_id=trim(p_request_id) and user_id=u for update;
  if s is null then raise exception 'translation_not_found'; end if;
  if s='failed' then return jsonb_build_object('request_id',trim(p_request_id),'status',s,'idempotent',true); end if;
  update public.translation_requests set status='failed',failed_at=now(),error_code=left(coalesce(p_error_code,'provider_failed'),80) where request_id=trim(p_request_id) and user_id=u;
  return jsonb_build_object('request_id',trim(p_request_id),'status','failed','idempotent',false);
end $$;
revoke all on function public.begin_translation_request(text,uuid,text,integer),public.complete_translation_request(text),public.fail_translation_request(text,text) from public,anon;
grant execute on function public.begin_translation_request(text,uuid,text,integer),public.complete_translation_request(text),public.fail_translation_request(text,text) to authenticated;
create or replace function public.beta_bootstrap() returns jsonb language sql security definer set search_path=pg_catalog,public as $$
select jsonb_build_object(
 'profile',jsonb_build_object('user_id',auth.uid()),
 'license',(select to_jsonb(x) - 'user_id' from (select id,status,expires_at from public.licenses where user_id=auth.uid() order by expires_at desc nulls last limit 1) x),
 'wallet',coalesce(public.wallet_summary(),'{}'::jsonb),
 'rank',coalesce(public.progression_summary(),'{}'::jsonb),
 'feature_flags',coalesce((select jsonb_object_agg(name,enabled) from public.beta_feature_flags),'{}'::jsonb),
 'maintenance',coalesce((select (value #>> '{}')::boolean from public.beta_remote_config where key='maintenance'),false),
 'minimum_app_version',coalesce((select value #>> '{}' from public.beta_remote_config where key='minimum_app_version'),'0.0.0')
); $$;
revoke all on function public.beta_bootstrap() from public,anon;
grant execute on function public.beta_bootstrap() to authenticated;
