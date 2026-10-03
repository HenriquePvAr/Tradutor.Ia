# Branch recovery — 2026-10-01

The nominal `research/qwen-visual-auditor` ref existed at `d5a95b3f`, the same Glyph R&D commit as `research/glyph-segmenter-v2`. It was preserved locally as `archive/qwen-visual-auditor-conflicted-20261001`; no history was deleted.

Commit `45a07f47` was audited: all 14 changed files were Qwen research files under `research/qwen-visual-auditor/`; no production, Glyph, secret, token, cache, weight, virtualenv, or large binary files were present. Its parent was `d5a95b3f`, not base `785517cc`, so direct pointer creation was unsafe.

Recovery used the authorized audited cherry-pick path: clean branch at `785517cc6fdf95832f71587a802047c923cc4144`, then only `45a07f47`. Resulting Qwen HEAD: `3a789cb9f14534022a94d411e135df431bf3bb04`.

No history, remote ref, production code, M20 artifact, capacity256 process, push, merge, tag, or release was touched. Glyph remains independent.

V7 accounting is explicit: 60 attempts were reported, but `all_results.json` contains 58 records: 30 baseline, 14 missing, 14 OCR. The two absent records are classified as `RESULT_RECORD_MISSING_OR_TRANSPORT_OMISSION`, not guessed as valid or invalid.
