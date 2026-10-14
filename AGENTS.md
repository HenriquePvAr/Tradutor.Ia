# YomuSekai agent rules

## Source of truth

- `main` is the canonical source branch for completed product code. A local integration branch is not a release or remote merge.
- Base new work on the current `main`. Use a temporary branch for risky work, then test, document, update the board and prepare a normal integration to `main`.
- Keep unrelated work isolated; do not leave parallel implementation branches indefinitely. Never discard worktree changes, untracked files, evidence or another agent's work without an explicit disposition.
- Treat package modules under `yomu/` as canonical where the current code and tests establish that migration. Root compatibility launchers/shims remain until their consumers are audited.
- Do not duplicate providers, pipeline, mask, or artifact implementations. Reuse the canonical contract or document why a legacy boundary remains.

## Safety and evidence

- Never run destructive Git cleanup/reset/restore or delete a worktree to resolve a conflict. Preserve originals and use a verified backup before reconciliation.
- Do not call paid Gemini/DeepL providers, consume YK, run a full chapter, or mutate production Supabase/VPS without explicit authorization.
- Never place passwords, tokens, cookies, service-role keys, webhook secrets, private keys or user data in source, documentation, logs, screenshots, builds or Git. Public client configuration is not an administrative credential.
- Preserve fail-closed license, ownership, source completeness, mask, certificate and artifact gates. A mock or offline test does not prove physical/provider behavior.
- Report evidence levels distinctly: `IMPLEMENTED`, `INTEGRATED`, `TESTED_OFFLINE`, `TESTED_FROZEN`, `PHYSICALLY_VERIFIED`, `EXTERNAL_GATE`, `DEFERRED`.
- Do not call a feature validated from its code, a historical report, HTTP health, mock-provider success or unrelated test totals alone.

## Database and release

- Supabase is authoritative for persistent data; schema changes require versioned migrations, tests and documentation. Do not mutate production without authorization.
- A main update is not a beta release. Build, installer, updater activation and publication have separate gates.
- No push, remote merge, release or installer unless explicitly approved.

## Completion

- Verify callers and contracts before editing. Run focused tests after each integration group and applicable regression on the exact final snapshot.
- Update technical documentation when architecture, config, worker, state, provider, security, API, packaging or output changes. Update the user guide only when user-visible behavior changes; do not fabricate screenshots.
- Update `docs/status/board/` with stable IDs and verifiable evidence. `VALIDATED` requires exact test/build/physical evidence; `PUBLISHED` means only actually published.
- End reports with documentation impact and list remaining physical/external gates explicitly.
