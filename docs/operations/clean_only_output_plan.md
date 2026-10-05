# Preset `Apenas Clean`

`Apenas Clean` is an explicit output plan, not merely a translation-disabled
legacy job. The UI sends `output_plan=apenas_clean`; the bridge normalizes it to
`translation_enabled=false`, `output_format=png`, and
`effective_operations=["clean"]`. The job configuration must persist those
plan fields before the worker starts.

If only the legacy translation flag is persisted, `plan_from_config` cannot
distinguish clean-only from an empty custom plan and the worker will skip CLEAN.
The bridge therefore persists `output_plan`, `requested_operations`, and
`effective_operations` for explicit plans. The entitlement boundary also charges
zero YK for this translation-free preset.
