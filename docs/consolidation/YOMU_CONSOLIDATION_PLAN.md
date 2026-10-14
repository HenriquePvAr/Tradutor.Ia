# Yomu consolidation plan

**Plan source:** read-only delta review by GPT-6.1 Sol, medium, returned 2026-10-14.
**Base:** `origin/main` `121d6285a487a3e2b1a43bc5b504f79e696a75ed`.
**Plan status:** executed as a local consolidation candidate with documented open gates; it is not evidence of main-update readiness.

## Authoritative decisions

1. Keep `origin/main` immutable and do all work on the single temporary branch `integration/yomu-main-consolidation-v1`.
2. Start with the 25-commit local `main` product line, then reconcile the 60-commit package/UI/Comix/SAFE CLEAN line ending at `785f9ee`.
3. Treat `integration/main-candidate-comix-ui-v1` and output-plan/Discord/Comix feature branches as ancestors or sources, not separate merges when their commits are already present.
4. Review and integrate the three current Comix clearance commits separately; compare them with the older owner-visible challenge capture. Also review the three packaged-UI commits and one login asset commit individually.
5. Do not wholesale copy the 738 dirty changes from the Gemini worktree. Compare tracked diffs and untracked files by domain; integrate only the validated shared product core and tests, retain evidence/debug/research/deploy material outside runtime as classified in the matrix.
6. Keep research, experimental model/data, two-slot deployment, and release branches preserved but unpromoted unless their exact production role and gates are demonstrated.
7. Preserve Supabase as the only configured licensing authority and fail-closed behavior. No entitlement/license mutation or privileged secret is allowed.
8. Do not claim physical Gemini/VPS/Discord acceptance from mocked tests, API health, branch ancestry, or historical reports.

## Ordered execution and gates

| Step | Action | Exit gate |
|---|---|---|
| 0 | Verify backup bundle, per-worktree patches and copied untracked files; preserve originals. Recheck remote SHA and dirty-worktree inventory. | SHA-256 verification passes; excluded secret-named files remain untouched at original paths; no source worktree is modified by backup. |
| 1 | Fast-forward consolidation branch through reviewed local `main` (25 commits). | Branch ancestry is origin/main → local main; inspect diff and local-main test/evidence references; no remote mutation. |
| 2 | Integrate candidate package migration and product commits through `785f9ee`, in dependency order: package compatibility → OutputPlan/JobStore/worker → source/Comix → OCR/ownership → Clean/Safe Final → observability → UI/history/outputs. | No duplicate active implementation; entrypoints/shims and PyInstaller imports resolve; inspect `git diff --check` and staged/current diff after each group. |
| 3 | Reconcile current Comix clearance commits and independently validated desktop/login commits. | Human-visible challenge action remains available; automated browser remains hidden where required; frozen/bootstrap/port ownership regressions pass. |
| 4 | Audit dirty Gemini integration changes. Split shared product code, tests/contracts, research/debug/evidence, and VPS deployment. | Only selected files appear in branch diff; no broad copy, no active provider config change, no promotion of two-slot experiment, no secrets/artifact payloads. |
| 5 | Audit each other unmerged product/security branch; record exact commit, files, tests and decision. Quarantine R&D/data/old release lines. | Every branch/worktree has a disposition; no deletion or worktree removal. Missing proof remains `PRESERVE_FOR_LATER` or `BLOCKED`, not pass. |
| 6 | Canonicalize documentation: current architecture, runtime/JobStore/progress, modes, sources, Gemini/VPS, Supabase/license/YK, outputs/PDF/PSD, Discord, training, known issues and beta readiness. Preserve historical audit files with explicit historical status. | `docs/README.md` is the index; claims use IMPLEMENTED/INTEGRATED/TESTED_OFFLINE/TESTED_FROZEN/PHYSICALLY_VERIFIED/EXTERNAL_GATE/DEFERRED with commit/test evidence. |
| 7 | Add main-first development rules to `AGENTS.md` and `CLAUDE.md`; update operational board with stable IDs, domain, paths, contracts, tests, evidence, blockers, dependencies and next action. | Main is described as source-of-truth for code, not as published release; no card is VALIDATED without exact evidence. |
| 8 | Run domain-focused tests after each integration group: import/package, JobStore/progress/modes, Comix child/single-pass, OCR/ownership, Clean/Safe Final, Auth/YK, Discord, PDF/PSD, updater/package. | Zero unexplained failures; pre-existing failures are identified by exact test and baseline. |
| 9 | Run Python regression, frontend tests, secret scan, package/frozen tests and `git diff --check` on the final exact branch. Build isolated Windows candidate only if the selected code supports a reproducible package environment. | Report exact command, interpreter, counts, skip/xfail and SHA. No Gemini/DeepL/YK/provider or production mutation. |
| 10 | Commit locally in organized, reviewable commits after validation. Compare against current `origin/main`, inspect file deletions/secrets, and write rollback instructions. | `READY_FOR_MAIN_UPDATE` only if all local gates pass; remote push/merge/release remain unperformed and require owner approval. |

## Current workflow boundary

The source checkout and every pre-existing worktree are preserved. The temporary branch/worktree is the only integration target. The backup is outside the repository at `C:\Users\henri\YomuMainReconciliationBackups\20261014-105033`; see its `backup-manifest.csv`, `worktree-inventory.json` and `all-refs.bundle`. Its bundle SHA-256 is `66078D1ECCFC4E9AFFE133017515A6A83B4680DC68418E0F93AC72EDDCD019BF`.

Rollback before publication is to leave the original worktrees untouched and switch review to the backup bundle/patch set or the original remote base; do not reset or clean a user worktree. Any local integration commit can be reverted on the temporary branch by a normal revert or the branch can be abandoned after preserving its ref and backup. Never force-push.

## External and physical gates

No paid providers, YK, Supabase production mutation, VPS change, full chapter, or physical customer acceptance is part of this consolidation. A real Gemini generation and authenticated licensed-user smoke are `EXTERNAL_GATE` unless separately authorized. `origin/main` update is explicitly owner-approved only.

## Current closeout boundary (2026-10-14)

The candidate is `integration/yomu-main-consolidation-v1`, local HEAD `63e8bcf` before the documentation closeout commit, 94 commits ahead and 0 behind the fetched `origin/main` (`121d6285...`). The live inventory is 109 worktrees/256 local branch refs; 66 tips are not ancestors of the candidate and are classified by domain in the reconciliation matrix. Eight source worktrees are dirty and remain untouched. The initial backup is integrity-verified but has post-snapshot local generated-file deltas; its scope is explicitly not overstated. The focused device-revocation guard is tested locally but not deployed. Therefore this candidate remains `PARTIAL`, not `READY_FOR_MAIN_UPDATE`, until the dirty overlays are reconciled, backup delta is captured, and the owner reviews the proposed main update.
