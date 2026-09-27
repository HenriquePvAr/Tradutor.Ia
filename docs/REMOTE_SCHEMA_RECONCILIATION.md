# Remote schema and migration-history reconciliation

Read-only snapshot of Supabase project `mimrsxnhqbqkffsekxuw`
(`Tradutor IA Community`) reconciled against local migration sources on
2026-09-27. No remote writes, migration repair, or Edge Function deployment
were performed.

## History result

- Remote applied versions: 44.
- Local active migration files after reconciliation: 46.
- Exact version matches: 44.
- Remote-only versions after reconciliation: none.
- Local-only versions: `20260924060000` and `20260926175232`; these are the
  license/device-preservation and atomic YK-settlement migrations intentionally
  pending local deployment.
- A future migration-list/db-push plan should therefore show only those two
  pending versions, provided the remote history has not changed.

## Divergent history mapping

| Remote version | Remote name | Previous local version/source | Equivalence | Local action |
|---|---|---|---|---|
| 20260824192515 | beta_tester_licensing_foundation | 20260824120000 | NORMALIZED_EXACT | Old duplicate preserved under `docs/archive/migrations/`; remote-version file remains active |
| 20260824192601 | beta_tester_authorization_rpc | 20260824130000 | NORMALIZED_EXACT | Old duplicate preserved under `docs/archive/migrations/`; remote-version file remains active |
| 20260824192729 | beta_tester_grants_hardening | 20260824140000 | NORMALIZED_EXACT | Old duplicate preserved under `docs/archive/migrations/`; remote-version file remains active |
| 20260913005346 | canonical_translation_commit_rpc | 20260912100000 | SUPERSEDED; remote version is exact local source | Older iteration archived; do not replay it |
| 20260914051341 | device_revocation_hardening | Remote history statements | EXACT remote source | Reconstructed from stored remote statements |
| 20260914115919 | prepare_safe_user_deletion | Remote history statements | EXACT remote source | Reconstructed from stored remote statements |
| 20260914115942 | rollback_safe_user_deletion | Remote history statements | EXACT remote source | Reconstructed from stored remote statements |
| 20260914121505 | prepare_safe_user_deletion_v2 | Remote history statements | EXACT remote source | Reconstructed from stored remote statements |
| 20260914150347 | admin_enable_license_device_operations | 20260923150000 | SEMANTIC_EQUIVALENT | Local file renamed to the already-applied remote version |
| 20260916041829 | yk_ads_foundation | 20260915140000 | EXACT_SQL | Local file renamed to remote version |
| 20260916041836 | yk_ads_product_contract | 20260916100000 | NORMALIZED_EXACT | Local file renamed to remote version |
| 20260917023046 | daily_cross_cycle_accounting | 20260917021000 | NORMALIZED_EXACT | Local file renamed to remote version |
| 20260915013128 | chapter_level_translation_finalization | Same-version local file | REMOTE_SQL_RECONSTRUCTED | Active file now matches stored remote SQL; prior local variant archived; live job overload is a separate out-of-history test overlay |

The two canonical translation migrations, `20260913005346` and
`20260913005557`, are each present locally at their exact remote versions.
The earlier `20260912100000` variant combined/preceded that contract and is
not an additional production migration.

The YK ads pair maps one-to-one by stored SQL, not by date inference. The YK
foundation file includes the same SQL held remotely, including its historical
comment; its filename now reflects the applied timestamp.

## Ayet rewarded sessions

`20260915153000_ayet_rewarded_sessions.sql` is preserved at
`docs/archive/migrations/`, outside the active migration chain. The current
remote database has `ad_reward_events`, but does not have
`rewarded_ad_sessions`, `create_rewarded_ad_session(text)`, or
`credit_ayet_rewarded_ad(text,text,integer,text)`. The rewarded-video flow
remains provider/feature-flag gated and the migration is classified
`DEV_ONLY / PROVIDER_APPROVAL_PENDING`, not a migration required to reproduce
the current remote baseline. It must be deliberately reviewed before enabling
that product flow. Read-only function inventory showed the rewarded-ad Edge
handlers are deployed; because their supporting session RPCs are absent, do not
treat that path as operational or invoke it until separately reconciled.

## Out-of-history schema drift

`OUT_OF_HISTORY_REMOTE_DRIFT` includes:

- `translation_requests.license_id` is nullable and its FK uses
  `ON DELETE SET NULL`, although no applied migration statement in the queried
  history explains that change. Origin remains `UNKNOWN`.
- Live overload `finalize_translation_job(text,uuid)` exists in addition to
  `finalize_translation_job(text)`. The recorded `20260915013128` statement
  defines the one-argument overload; no migration row explains the two-argument
  overload. Its live body was captured as a test-only overlay, not converted
  into a migration.

No historical migration was fabricated for either drift. The pending
`20260924060000_preserve_translation_requests_on_license_delete` was tested
on PostgreSQL 15 against the exact partial `license_id` pre-state.

## Deploy-critical closeout (2026-09-27)

The deployment decision is scoped to the object graph used by the two pending
migrations and the three Edge Functions (`wallet-settle-job`,
`translation-execute`, `wallet-finalize`): `translation_requests`,
`yk_reservations`, `yk_ledger`, `progression_ledger`, `licenses`,
`license_devices`, `license_sessions`, `plans`, and the called RPCs for request
claim/persistence, reservation/release, job finalization, and settlement.

The read-only remote catalog was compared for relevant columns/types/nullability
and defaults, request/reservation/ledger constraints and indexes, function
identity arguments/return types, `SECURITY DEFINER`, `search_path`, owner, and
function ACLs. PostgreSQL 15 replay applies all active local history, then only
the two observed out-of-history overlays, then each pending migration. The
integration suite validates the resulting schema and financial state machine.
This is a deploy-critical comparison, not a claim that every unrelated object
in the entire project schema has been exhaustively compared.

### Same-version historical SQL differences

These 15 same-version SQL pairs had formatting/comment or historical-source
differences. Their classification is based on final object effects and whether
they touch the deploy-critical graph, not byte-for-byte identity.

| Version | Name | Classification | Critical graph? | Blocking? | Reason |
|---|---|---|---|---|---|
| 20260716120000 | social_schema | NON_BLOCKING_HISTORICAL_DIFFERENCE | NO | NO | Social schema only; no dependency into licensing, translation settlement, or YK. |
| 20260716120100 | social_rls | NON_BLOCKING_HISTORICAL_DIFFERENCE | NO | NO | Social RLS is outside the translation/wallet call graph. |
| 20260717120000 | fix_chapter_read_policy | NON_BLOCKING_HISTORICAL_DIFFERENCE | NO | NO | Chapter read policy; no settlement table/RPC changes. |
| 20260722120000 | public_profile_identity | NON_BLOCKING_HISTORICAL_DIFFERENCE | NO | NO | Profile identity is not called by these migrations/functions. |
| 20260806120000 | legacy_publication_provenance | NON_BLOCKING_HISTORICAL_DIFFERENCE | NO | NO | Publication provenance only. |
| 20260806130000 | legacy_publication_migration_runtime | NON_BLOCKING_HISTORICAL_DIFFERENCE | NO | NO | Publication runtime objects only. |
| 20260807140000 | legacy_storage_upload_reservations | NON_BLOCKING_HISTORICAL_DIFFERENCE | NO | NO | Separate from YK translation reservations. |
| 20260811120000 | community_publication_artifacts | NON_BLOCKING_HISTORICAL_DIFFERENCE | NO | NO | Publication artifacts do not participate in settlement. |
| 20260811130000 | community_publication_metadata_backend_access | NON_BLOCKING_HISTORICAL_DIFFERENCE | NO | NO | Publication metadata access only. |
| 20260811140000 | community_social_source_identity_mapping | NON_BLOCKING_HISTORICAL_DIFFERENCE | NO | NO | Social identity mapping only. |
| 20260911120000 | resolve_reserve_rpc_overload | FUNCTIONALLY_EQUIVALENT | YES | NO | Final reserve signatures/behavior match the remote catalog; later YK migrations re-establish final reserve implementation. |
| 20260911130000 | translation_replay_authority | FUNCTIONALLY_EQUIVALENT | YES | NO | Claim-recovery migration supplies final persistence contract; deployed signatures/behavior are exercised by baseline tests. |
| 20260911140000 | translation_operation_claim_recovery | FUNCTIONALLY_EQUIVALENT | YES | NO | Final claim, provider-start, and persisted-result contracts match remote catalog and Edge callers. |
| 20260917012623 | wallet_summary_plan_diagnostic | NON_BLOCKING_HISTORICAL_DIFFERENCE | NO | NO | Diagnostic wallet-summary evolution is not called by settlement or these Edge Functions. |
| 20260917015949 | wallet_summary_auth_reconciliation | NON_BLOCKING_HISTORICAL_DIFFERENCE | NO | NO | Wallet-summary diagnostic path is outside this deploy's RPC call graph. |

### Legacy finalizer overload

The read-only remote catalog contains `finalize_translation_job(text)` and
`finalize_translation_job(text,uuid)`, both `SECURITY DEFINER`, owned by
`postgres`, with fixed `search_path=pg_catalog, public`; execute ACL is limited
to `service_role` and `postgres`. The two-argument overload locks job requests
and reservation, requires all requests to be completed and DeepL-backed, and
returns idempotently with zero debit if the reservation is already consumed.
Remote `translation-execute` v15 explicitly invokes it with `p_job_id` and
`p_reservation_id` on the legacy path. The local replacement commits the
provider result without finalizing; the job then settles through
`wallet-settle-job`. The new `settle_translation_job(text,uuid,text,text)` calls
the two-argument overload inside its transaction, preserving it as the
canonical ledger writer. PostgreSQL replay with the live overload fixture
passed retry, legacy-finalize-after-settle, provider failure/recovery, and
concurrent settlement with exactly one debit. RPC identity arguments are
distinct; settlement is a single signature and its caller sends all four named
arguments, so no PostgREST ambiguity was found.

Remote `wallet-finalize` v10 calls `finalize_yk_reservation(uuid,boolean)`.
The existing owner-scoped release RPC checks for processing/completed requests
before releasing. The local replacement only calls `release_yk_reservation`
and rejects client consume requests. The YK settlement RPC also rejects
release after processing/completed/persisted provider output.

| Caller | Remote current behavior | Local release/deploy behavior |
|---|---|---|
| translation-execute | v15 can call `finalize_translation_job(text,uuid)` when legacy `finalize_job` is not false | New source commits result only; settlement is delegated to `wallet-settle-job` |
| wallet-finalize | v10 calls `finalize_yk_reservation(uuid,boolean)` | New source calls `release_yk_reservation`; consume is rejected |
| wallet-settle-job | Absent | Calls the one `settle_translation_job(text,uuid,text,text)` RPC |

Classification of `finalize_translation_job(text,uuid)`:
**REQUIRED_LEGACY_COMPATIBILITY**. It serves the deployed v15 compatibility path
and the new settlement RPC; do not remove it during this rollout. No
double-charge path was found: both finalizers lock/check the same terminal
reservation and a second call on `consumed` is idempotent.

### Deploy state and remaining drift

- `DEPLOY_CRITICAL_SCHEMA_COMPARE=PASS`: critical live schema, signatures,
  guards/ACLs, the two explicit drift overlays, pending migrations, and local
  Edge callers are aligned for this release.
- `KNOWN_OUT_OF_HISTORY_DRIFT`: `translation_requests.license_id` nullable with
  `ON DELETE SET NULL` (origin unknown); live
  `finalize_translation_job(text,uuid)` overload (no migration-history source).
- `NON_BLOCKING_REMOTE_DRIFT`: the historical pairs above; none is a dependency
  of the pending migrations or the three Edge Functions.
- `EXPECTED_PENDING_MIGRATIONS`: exactly `20260924060000` and
  `20260926175232`; version comparison is 44/44, zero remote-only. Supabase CLI
  was unavailable, so this is a direct version-set comparison, not an actual
  CLI dry run.

### Settlement security advisor disposition

After the YK deployment, Supabase reported
`authenticated_security_definer_function_executable` for
`public.settle_translation_job(text,uuid,text,text)`. This is
**REVIEWED_EXPECTED / ACCEPTED**, not an instruction to broaden or redesign the
settlement contract: the caller-JWT path invokes the RPC with the caller's
Bearer token so `auth.uid()` remains the actual user. The function uses an
empty `search_path`, requires `auth.uid()`, matches the reservation owner and
job, constrains the action and idempotency key, and derives the amount from
server-side reservation state. `anon` has no execute privilege. No security
change is required; do not revoke `authenticated`, switch to
`SECURITY INVOKER`, or route this RPC through a service-role caller as part of
this disposition.

## Validation and safety

The PostgreSQL integration chain applies every active migration file in
timestamp order after the platform bootstrap, including all 44 remote versions
and the two pending migrations. The active `20260915013128` source now matches
the stored remote SQL; its prior local variant is archived. A separate baseline
test replays the remote history then applies explicit fixtures for the two
observed drifts. The license/device-preservation and YK settlement suite,
including concurrency and idempotency, passed against PostgreSQL 15. The
deploy-critical subset was mechanically inspected and functionally replayed;
unrelated historical schema is deliberately outside this closeout's scope. The
origin of the second live overload remains unknown and is recorded as
out-of-history drift, but its callers and terminal-state/idempotency behavior
are classified and tested.

No migration repair is indicated by the timestamp inventory: the repository
now uses the already-applied remote versions and has files for all remote
timestamps. The Supabase CLI was unavailable, so CLI migration-list and dry-run
were not run; timestamp comparison was performed directly against the
read-only remote migration inventory.

## Audit boundaries

- Remote schema/migration reads: performed.
- Remote mutations / migration repair / Edge deploys: 0.
- Git push, tag, or release: 0.
- No user balances, licenses, devices, or translation data were changed.
