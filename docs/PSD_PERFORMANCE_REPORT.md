# M17 — Professional PSD performance and size report

## Scope and provenance

This report measures the existing `professional_qc` output from the M16 job
`51c813f392704423b28967a63faf66a5` / run
`0e9f749c-406b-4116-bbe5-61f4b0bf638b`. The selected source was Webtoon — *The
Stellar Swordmaster*, Season 2 Episode 130, as consciously selected by the
owner. The source produced 142 downloaded images and 88 logical output pages.
No translation, provider request, YK reservation, or remote mutation was
started for M17.

Environment: Windows 10, 12 logical CPUs, 31.4 GiB RAM; Python environment
used `psd-tools 1.10.9`, Pillow 12.3.0, and psutil 7.2.2. CPU model was not
collected.

`EDITABLE_PSD` is produced by `editable_psd_writer.write_editable_psd` in
`editable_psd_writer.py`; `professional_psd.py` remains a separate legacy
path. The editable writer stores native Type Layer data. No Type Layer was
rasterized or replaced for this measurement.

## Measured artifacts

Sizes are MiB (bytes / 1,048,576), not decimal MB.

| Artifact | Count | Total MiB |
| --- | ---: | ---: |
| Translated PDF | 1 | 12.25 |
| Translated images | 88 | 111.67 |
| Clean images | 88 | 107.11 |
| Editable PSDs | 88 | 978.66 |
| Quality Report + Review Manifest | 2 | 4.62 |
| Entire run output directory | 697 files | 1,621.84 |

PSD sizes: minimum 4.13 MiB; maximum 35.96 MiB; mean 11.12 MiB; median
10.06 MiB; observed p95 18.55 MiB (lower observed order statistic,
`floor(0.95 × (n−1))`). The 35.96 MiB largest PSD is an outlier relative to
the median (`psd/085.psd`); individual page size is content/dimension
dependent.

The automatic artifact baseline (PDF + translated images) is 123.92 MiB.
Professional-only artifacts measured here (PSD + clean images + QC files) add
1,090.40 MiB, or 879.90% of that automatic baseline. The professional
artifact subset totals 1,214.32 MiB. The full run directory is larger because
it also contains downloaded inputs, intermediate page data, and manifests.
No `.tmp`/`.temp` files remained in the run directory at inspection time;
transient process scratch outside that directory was not separately measured.

## Isolated current-writer benchmark

The existing `tools/harnesses/large_strip_psd_memory_probe.py` imports `psd_exporter`, a
different/legacy writer, and is not valid evidence for the current editable
PSD implementation. Instead, a one-pass local benchmark invoked
`editable_psd_writer.write_editable_psd` for each of the 88 pages, using the
job's existing source and clean images and Review Manifest text/bbox records.
Each page used the production writer's structural validation. Outputs were
created under a temporary directory and removed automatically at process exit;
the official M16 output was not overwritten. This isolates writer + structural
validation cost, but is not the original pipeline's full `_export_editable_psds`
orchestration timing.

| Metric | Observed |
| --- | ---: |
| Pages written | 88 |
| PSD export + structural validation total | 74.077 s |
| Average per page | 0.842 s |
| Minimum / maximum per page | 0.420 / 2.448 s |
| Median per page | 0.773 s |
| p95 per page | 1.209 s (lower observed order statistic, same method as above) |
| Process RSS baseline / peak / delta | 38.1 / 563.6 / 525.5 MiB |
| Process CPU sampled average / peak | 98.0% / 113.6% |
| Temporary PSD bytes written | 1,014,691,152 bytes |

CPU percent is process utilization sampled during this run and can exceed
100% on a multi-logical-CPU machine; it is not a whole-machine utilization
measurement. RSS is for the isolated benchmark process, not the desktop or
the complete M16 pipeline.

The M16 quality report records 690.839 s total and 432.105 s in `render`, but
has no dedicated PSD-export timer. The isolated writer benchmark divided by
the reported total is approximately 10.72%; treat this as a cross-measurement
estimate, not an instrumented M16 stage percentage. The `render` bucket must
not be interpreted as PSD-only time.

## 208-page linear estimate

These are arithmetic estimates (`208 / 88`) from the measured 88-page job,
not a 208-page run. They assume similar per-page dimensions, text density,
compression, and output format; real chapter variance can be substantial.

| Metric | Estimated at 208 pages |
| --- | ---: |
| PSD total | 2,313.20 MiB |
| PSD writer + validation time | 175.09 s |
| Professional artifact subset (PDF + translated + clean + PSD + QC) | 2,870.22 MiB |
| Full run directory | Not estimated: source download and intermediate-data volume do not scale reliably from this 142-input/88-output run. |

## Validation and product conclusion

All 88 generated PSD files opened with the independent `psd-tools` parser.
All had `ORIGINAL`, `CLEAN`, and `TEXT` groups, with pixel artwork in the
first two groups. The parser found 162 native Type Layers across the set.
This is structural validation only; Photoshop physical editability evidence
for the real M16 PSD was supplied by the owner and is tracked as M16 evidence,
not newly repeated in M17.

Focused tests: 70 passed for artifact contract, editable writer, output-profile
selection, page manifest, Review Manifest, and run identity; 13 passed in the
Professional PSD suite (83 total, 0 failures). No full regression was run
because production code was unchanged. No new skips or xfails were introduced.
Provider calls added: 0. YK changed: no.

**Conclusion:** keeping Automatic as PDF + translated images and limiting PSD,
clean pages, and QC artifacts to Professional/QC remains the technically
appropriate split. In this run, PSDs dominate professional storage (about
80.6% of the measured professional artifact subset), while preserving native
Type Layers and the ORIGINAL/CLEAN/TEXT contract. Do not reduce this cost by
rasterizing text.

## M16 retry/re-export accounting audit

The local job store contains the single M16 job and no child/retry job for the
same source. The active Supabase project has exactly the two completed
translation requests for that job, one consumed reservation of amount 1, and
one matching ledger debit of −1 YK. Read-only Edge logs after the job's
terminal time contain zero `translation-execute` and zero
`wallet-settle-job` events for the account. Thus the available backend evidence
supports 0 additional provider executions and 0 additional YK for the owner's
reported physical retry/re-export. It does not independently prove the UI
action itself; that physical action was reported by the owner.
