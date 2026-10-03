# Kaggle Qwen fallback lab (experimental)

Isolated DEV/LAB benchmark. This package is deliberately not imported by Yomu production code.

The runner selects a non-M20 image, creates a temporary staging directory, creates a **private** Kaggle dataset, pushes a GPU notebook, waits for completion, downloads and validates `result.json`, then deletes the dataset and local staging in a `finally` block. The worker loads `Qwen/Qwen3-VL-8B-Instruct` in FP16 and writes only `result.json`.

## Run

```powershell
python experiments/kaggle_qwen_fallback_lab/run.py --image path\to\image.png --prompt "Audit this image" --kaggle-slug your-user/qwen-fallback-lab-<unique>
```

Requirements for a real run: Kaggle CLI on PATH, authenticated Kaggle account, GPU notebook quota, and a unique `--kaggle-slug`. The runner refuses an M20 image, non-private datasets, and a slug without an owner. `--dry-run` exercises planning without network access.

## Actual run (2026-10-01)

The isolated venv uses Python 3.11.9 and `kaggle==2.2.4`. OAuth authentication was completed locally with `kaggle auth login`; no token was copied into this repository. A private upload probe succeeded and was deleted. The real NON-M20 fixture was `test_fixtures/quality_regression_diagnostics/real/sfx_slurp_p006/source_crop.png` (SHA-256 recorded in the report).

The real kernel completed on Kaggle with `Qwen/Qwen3-VL-8B-Instruct`, returned `result.json`, and reported 161.901 seconds runtime including model load/inference. The kernel was deleted. The explicit dataset-delete API call returned HTTP 403, but a subsequent private dataset listing found no matching temporary dataset; this is recorded as a cleanup limitation, not silently treated as a perfect delete confirmation. No production routing, provider, quality mode, YK, DeepL, or Supabase code was changed.

Credentials are handled by Kaggle's local OAuth cache. Never commit that cache, API tokens, cookies, or environment secrets. This package is DEV/LAB/BENCHMARK only and is not a commercial or production backend.

## Contract

`result.json` must contain `schema_version`, `status`, `image_sha256`, `model`, `prompt`, `answer`, and `runtime`. Cleanup is best-effort but failures are recorded in the report and cause a non-zero exit.
