# Better Auth local preparation

This repository keeps Supabase as the default authentication provider. Better Auth is prepared behind an explicit provider flag and a same-origin proxy so the UI can be exercised locally without exposing cookies, tokens, or admin credentials to the browser.

## Current state

- Default provider: `supabase`
- Optional provider: `better_auth`
- Public browser path: `/api/auth/*`
- Internal local service path: `http://127.0.0.1:<port>/api/auth/*`
- Canonical app session check: `/api/community/auth/session`

The Python UI only enables the proxy when `AUTH_PROVIDER=better_auth` or `COMMUNITY_AUTH_PROVIDER=better_auth`. The internal Better Auth URL must point to a loopback HTTP host. Non-loopback URLs fail closed.

## Required local variables

Do not commit values.

```text
AUTH_PROVIDER=better_auth
BETTER_AUTH_SECRET=<at least 32 bytes>
BETTER_AUTH_URL=http://127.0.0.1:8080
BETTER_AUTH_INTERNAL_URL=http://127.0.0.1:8787
BETTER_AUTH_DATABASE_URL=C:\Projetos\Tradutor.Ia\.cache\runtime\better-auth.sqlite3
BETTER_AUTH_TRUSTED_ORIGINS=http://127.0.0.1:8080
```

Google OAuth remains disabled unless both `BETTER_AUTH_GOOGLE_CLIENT_ID` and `BETTER_AUTH_GOOGLE_CLIENT_SECRET` are configured.

## Local commands

From `apps/auth-service`:

```powershell
npm ci
npm run typecheck
npm test
npm run build
```

No remote migration is executed by these commands.

## Migration scripts

The files under `scripts/auth-migration/` are intentionally dry-run first:

- `audit.ts` reads planned inputs and reports safe metadata.
- `dry-run.ts` validates that migration settings are present without writing remotely.
- `migrate.ts` is disabled and throws until an explicit remote migration plan is approved.
- `verify.ts` describes the post-cutover checks.

Rollback remains Supabase by changing the provider flag back to `supabase` and restarting the local UI/backend.
