with candidates as (
  select
    e.id as entitlement_id,
    e.user_id,
    coalesce(e.starts_at, timezone('utc', now())) as starts_at,
    e.expires_at,
    e.max_devices,
    (select p.id from public.plans p where p.slug='free' and p.active limit 1) as plan_id
  from public.beta_tester_entitlements e
  where e.beta_channel='scan-beta'
    and e.source_license_id is null
    and e.status='active'
    and e.revoked_at is null
    and e.expires_at > timezone('utc', now())
    and not exists (
      select 1
      from public.licenses l
      where l.user_id=e.user_id
        and l.status='active'
        and (l.expires_at is null or l.expires_at > timezone('utc', now()))
    )
), inserted as (
  insert into public.licenses(
    user_id,status,plan_id,starts_at,expires_at,max_devices,created_by,notes
  )
  select
    c.user_id,
    'active',
    c.plan_id,
    c.starts_at,
    c.expires_at,
    c.max_devices,
    null,
    'beta_channel=scan-beta;backfill=entitlement_sync'
  from candidates c
  returning id,user_id
)
update public.beta_tester_entitlements e
   set source_license_id=i.id,
       notes=coalesce(e.notes,'{}'::jsonb) || jsonb_build_object('backfilled_to_license',true),
       updated_at=timezone('utc',now())
  from inserted i
 where e.user_id=i.user_id
   and e.beta_channel='scan-beta'
   and e.source_license_id is null;;
