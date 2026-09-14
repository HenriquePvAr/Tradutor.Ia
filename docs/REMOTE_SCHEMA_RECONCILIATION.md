# Remote schema reconciliation ledger

Snapshot read-only of Supabase project `mimrsxnhqbqkffsekxuw` (Tradutor IA
Community), captured during the beta/release-cleanup audit. No migration was
applied by this audit.

## Inventory

- Remote applied migrations: 38
- Local migration files: 35
- Functional schema objects confirmed remotely: `licenses`,
  `beta_tester_entitlements`, `license_devices`, `license_sessions`,
  `device_challenges`, `translation_requests`, `beta_feature_flags`, and
  `beta_remote_config`.

The remote history contains the current shared schema. The local folder is not
currently a complete replay source for that history; this is a history/source
drift, not proof that the live schema is missing objects.

## Ownership classification

| Remote version | Remote name | Owner | Local source | Match/status |
|---|---|---|---|---|
| 20260716120000 | social_schema | YOMU_CORE | local | EXACT |
| 20260716120100 | social_rls | YOMU_CORE | local | EXACT |
| 20260717120000 | fix_chapter_read_policy | YOMU_CORE | local | EXACT |
| 20260722120000 | public_profile_identity | YOMU_CORE | local | EXACT |
| 20260806120000 | legacy_publication_provenance | YOMU_CORE | local | EXACT |
| 20260806130000 | legacy_publication_migration_runtime | YOMU_CORE | local | EXACT |
| 20260807140000 | legacy_storage_upload_reservations | YOMU_CORE | local | EXACT |
| 20260811120000 | community_publication_artifacts | YOMU_CORE | local | EXACT |
| 20260811130000 | community_publication_metadata_backend_access | YOMU_CORE | local | EXACT |
| 20260811140000 | community_social_source_identity_mapping | YOMU_CORE | local | EXACT |
| 20260824192515 | beta_tester_licensing_foundation | YOMU_CORE | local | EXACT |
| 20260824192601 | beta_tester_authorization_rpc | YOMU_CORE | local | EXACT |
| 20260824192729 | beta_tester_grants_hardening | YOMU_CORE | local | EXACT |
| 20260908195000 | commercial_beta_foundation | SHARED_SCHEMA | local | EXACT |
| 20260909090000–20260909200000 | beta/control-plane, device, wallet and translation RPCs | YOMU_CORE | local | EXACT |
| 20260910172717–20260911140000 | entitlement sync, pricing and translation recovery | YOMU_CORE | local | EXACT |
| 20260913005346 | canonical_translation_commit_rpc | YOMU_CORE | `supabase/migrations/20260913005346_canonical_translation_commit_rpc.sql` | EXACT; source recovered from `supabase_migrations.schema_migrations` |
| 20260913005557 | canonical_translation_commit_rpc_grants | YOMU_CORE | `supabase/migrations/20260913005557_canonical_translation_commit_rpc_grants.sql` | EXACT; source recovered from `supabase_migrations.schema_migrations` |
| 20260914051341 | device_revocation_hardening | YOMU_CORE | `C:\Projetos\Tradutor.Ia-device-revocation\supabase\migrations\20260914120000_device_revocation_hardening.sql` | EQUIVALENT; hash `75BE965D44CC519D21015C1518E363B9AD29FD19B379B2C97320E67763EE6422` |
| 20260914115919 | prepare_safe_user_deletion | SHARED_SCHEMA / ADMIN_OWNED | `C:\Projetos\Yomu-Admin-Backend\sql\prepare_safe_user_deletion.sql` | EQUIVALENT |
| 20260914115942 | rollback_safe_user_deletion | HISTORICAL_ROLLBACK / ADMIN_OWNED | `C:\Projetos\Yomu-Admin-Backend\sql\rollback_safe_user_deletion.sql` | ROLLBACK |
| 20260914121505 | prepare_safe_user_deletion_v2 | SHARED_SCHEMA / ADMIN_OWNED | `C:\Projetos\Yomu-Admin-Backend\sql\prepare_safe_user_deletion_v2.sql` | EXACT; hash `B5736E79F24AC7876CF2E1DD1F6AEBD02BC104A372F3A87FD83328D5A437E9DC` |
| 20260914150347 | admin_enable_license_device_operations | SHARED_SCHEMA / ADMIN_OWNED | `C:\Projetos\Yomu-Admin-Backend\sql\admin_enable_license_device_operations.sql` | EXACT; hash `E0B3CD0AF888DD33800BF1CE62A61DBC71C718A2E65ED1844DA7EE79C502DA45` |

The remote list also contains the current versions of all intermediate
entries in the ranges above; they are present locally unless explicitly marked
otherwise. The three older local licensing filenames with timestamps
20260824120000/130000/140000 are superseded naming/versions of the applied
20260824192515/192601/192729 entries and must not be deleted without a separate
history decision.

## Important conclusions

- `canonical_translation_commit_rpc` now has both exact remote statements in
  the canonical Core history. The older `20260912100000` file remains for
  historical auditability; it is not a substitute for the two applied remote
  versions.
- The current remote function signature is
  `commit_translation_success(text, integer, text, text, jsonb)`. ACL is
  restricted to `postgres` and `service_role`; `public`, `anon`, and
  `authenticated` do not have EXECUTE.
- The user-delete migrations and `admin_enable_license_device_operations` are
  real changes to the shared Supabase schema, but remain Admin-owned rather
  than Yomu Core runtime code.
- No SQL was reconstructed from assumptions. Both previously missing statements
  were recovered directly from the remote migration history.

## Audit actions

- Database mutations: 0
- Migrations applied: 0
- Edge deploys: 0
- Auth/storage/license/device mutations: 0
- Translation calls: 0
- Current remote function matches the canonical migration: YES
- Current remote grants match the grants migration: YES
