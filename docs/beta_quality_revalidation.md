# Beta quality revalidation

Branch: `beta/packaging`. Start: `292f032514201d0beb7132172c55716ae04a5702`.
Frozen main/tag commit: `5ebd77e7f0f107f5492902353dddde7ff044d9ce`.

## Authoritative runtime

`.venv-beta/Scripts/python.exe`, Python 3.11.9, `PYTHONNOUSERSITE=1`, user site disabled.
Only `opencv-python==5.0.0.93` is installed; imported cv2 is 5.0.0 from the venv.
RapidOCR 1.4.4 instantiated with its packaged models. PaddleOCR, PaddlePaddle,
PaddleX and duplicate OpenCV distributions are absent. No Paddle module was imported.
Runtime profile check and pip check passed. Global Python was not changed.

## Correct historical comparison

Historical run: `C:/Projetos/Tradutor.Ia-main-promotion/output/shadow_slave_chapter_1_5/d11c7d3b-65be-4e86-974e-9b9ce731bf99`.
Its manifest records `ee71cfa7c3951cdb517cfc38bb67a78bc7ad2e24`.
Recent Beta run: `output/shadow_slave_chapter_1_5/b3c03e69-dfe6-4668-b6ed-cc06dbb8bd66`,
manifest commit `0323031200850469ac51469ea3b18f7a6adad7a7`.
Both have 35 pages. The three relevant source images are 800x5000 in both runs.
Decoded RGB SHA256 values match exactly:

| Page | SHA256 (both runs) |
|---|---|
| 28 | af6a17f653047d6d57138bdf8e85f4ffcac2e9c6868064ab5094b552c29ce9cc |
| 31 | d6aa35c2e09ba32081cc51871de85293523dc044880264661fca654ea6090303 |
| 32 | 70045aefa13eac9e0ca7d55db5be3baccccd314f157071f17faefa2da3a86c18 |

Ordinary-region OCR text and bounding boxes also match. Thus source content,
page splitting and crop geometry drift do not explain these cases relative to
this historical run. The earlier smaller-page comparison used a different
artifact and must not be treated as evidence of a Beta regression.

Both contexts have 3 proper names, 120 term bindings and 102 remembered
translations. The historical SPELL binding is DO; the recent binding is PESADELO.
Both have `learned_term` / `recurring_region_alignment` provenance without
unambiguous alignment evidence. The successful historical label does not mean
these particular product-quality gates passed: its P31 was already manual review
and its P32 was already suppressed.

## Offline replay of persisted candidates

The replay loaded the old session-context implementation from Git HEAD into an
isolated module and compared it with the WIP using separate copies of the same
persisted context. It did not save either copy or call a provider.

| Case | Before | Current WIP | Product interpretation |
|---|---|---|---|
| P28/BALAO_1 | recovered segmentation | `source_segmentation_recovered:BUTYOUWILLALSO`, renderable | Candidate survives; historical pixels inspected |
| P28/BALAO_2..6 | accepted | accepted | BALAO_5 has a human-observed tense error in both runs |
| P31/BALAO_1 | `terminology_conflict:SPELL` | no terminology/fidelity rejection | Poisoned-memory gate closed offline; new final pixels not yet proven |
| P32/BALAO_2 | incomplete segmentation | `source_segmentation_incomplete:AGATETHROUGHWHICH`, `review_unusable` | Safety passes; release quality fails |

P28/BALAO_5 source is `YOU MIGHT HAVETOKILL THEM, KID.` but candidate says
`TALVEZ VOCÊ TENHA TIDO QUE MATÁ-LOS, GAROTO.` This changes prospective obligation
to a past action. The local fidelity gate does not detect it. It is present in
both historical and recent artifacts, not demonstrated to originate in packaging.

P32 normalizes `..YOUBECOME AGATETHROUGHWHICH ...` to `..YOU BE COME ...`.
The persisted candidate contains `GÁTTA`; current fidelity rejects it. Inspection
of the final page confirms visible English in that balloon. Safety and release
quality are therefore separate outcomes. No target was forced into the region.
The recovery record contains a correct-looking canonical `A GATE` proposal but
marks it untrusted with `material_variant_disagreement`: alternate crops also
produce an extra E after YOU. It is not an absent recovery attempt.

## Test isolation

The word-sense corpus test now uses deterministic in-source fixtures with positive
and negative cases instead of developer `.cache/processed` contents.
Windows venv launchers have a distinct PID from the interpreter that publishes a
worker lease or HTTP identity. Live identity assertions now require membership in
the launched process tree; the hard-exit test compares the lease with the PID
reported by the child and verifies it is gone. Teardown kills owned descendants.
This keeps the Beta interpreter and does not replace it with global Python.

Two leftover processes from the prior suite were verified to use the same
`tradutor-test-runtime-6bo2c1zx/jobs.sqlite3` and stopped. They were not real jobs.

An audited replay traced the remaining detached-worker leak specifically to
`test_ui_persistent_queue.UiPersistentQueueTests.test_authenticated_start_stamps_owner_only_from_principal`:
`UiBridge.start -> ensure_worker -> start_worker -> spawn_worker_process`.
That persistence-only fixture mocked source analysis but not worker launch.
It now stubs `ensure_worker` and has a tripwire on `spawn_worker_process`.
Actual lifecycle behavior stays covered by the real isolated worker tests.

A subsequent read-only path probe found that the WIP isolated the queue but not
the default cache/output/temp roots. `default_user_data_root()` now resolves to
the temporary test root while the runtime guard is enabled, failing closed when
that root is missing. The guard additionally protects the installed Beta's real
runtime/cache/output/temp trees. Focused verification: 119 passed and no surviving
Python processes. Production model assets remain outside this mutable-state guard.

## Pending packaging findings (not release approval)

Provider-aware UI authorization is wired and configured Supabase mode fails
closed when public config is missing. An unset provider still selects local
development in the existing factory; deployment provisioning must explicitly
select/enforce the Beta provider.

Better Auth uses the Node service through the loopback `/api/auth` proxy when
that auth provider is selected. Supabase license RPC authorization itself is a
Python path and does not require Node. The launcher does not currently start
the Node service. Environment provisioning is still manual; no tester onboarding
replacement for `.env`/`.env.local` was established here.

Writable-path migration remains partial: production `profile_root`, typography
asset lookup and confirmed mask lookup still reference the repo; community and
local-input defaults and CLI output defaults also retain repo/CWD paths.
Do not claim Program Files safety or installer readiness from the current WIP.

Historical PDF SHA256 values, read-only:

- Historical: `a26b0e525fc1c960036f44c164c9c2026c038ad783b51d5e5e553a6085c7c2e3`
- Recent Beta: `7dd2bc906777f13998b9f6299369ef1ad0b14c306254c8fc040d08efde8b33ed`

## Final offline validation

- Full Beta pytest: 4488 passed, 74 skipped, 0 failed, 1 dependency deprecation
  warning, 679 subtests; 374.40 seconds. Log: `.cache/beta_quality_final_pytest.log`.
- Post-suite Python/worker/UI processes: zero.
- JS: 14/14 actual `.mjs` files with `--experimental-vm-modules`.
- Auth service: 4 tests / 2 files passed.
- pip check, changed-file compilation, and diff whitespace check passed.
- Skips: 40 unavailable historical image/model cases, 31 Docker/PostgreSQL cases,
  2 opt-in forensic cases, 1 historical E2E artifact case. No Paddle absence skip.
  Count unchanged from the pre-fix Beta run; an independently verified frozen
  full-suite skip count is not available in this audit.

These results authorize the requested one-job verification, not a product-quality
or installer approval. No real job has been created as of this offline record.
