# Archived migration sources

These SQL files are preserved for audit/history but are intentionally outside
the active `supabase/migrations` chain.

- `20260824120000`, `20260824130000`, and `20260824140000` are prior local
  timestamp copies of beta tester migrations already active locally and applied
  remotely as `20260824192515`, `20260824192601`, and `20260824192729`.
- `20260912100000_canonical_translation_commit_rpc.sql` is an older local
  iteration superseded by the two canonical RPC migrations at their exact
  applied remote versions, `20260913005346` and `20260913005557`.
- `20260915153000_ayet_rewarded_sessions.sql` is a provider-gated, currently
  inactive product migration. Its table/RPCs are absent remotely; do not apply
  or re-activate without explicit product/provider approval.

These archived sources are not part of a clean baseline replay or db-push plan.
