# M16 corpus — offline quality replay

This note records the offline replay used during M18.5. The M16 artifacts were
read-only; this was not a new translation run, and it made no provider, YK, or
remote calls. Counts below describe the available persisted job/run metadata,
not a newly generated set of artifacts.

## Corpus and replay coverage

The source download contained 142 images and the output represented 88 logical
pages. The persisted Quality Report recorded 22 source/visual residual issues,
3 visual validation failures, 1 incomplete region-coverage issue, and 8 regions
without a final valid translation candidate. A separate terminal-region review
identified 30 `SOURCE_FALLBACK` outcomes; those were not assumed acceptable
merely because the fallback had been recorded.

Offline OCR/geometry inspection covered the 88 translated images and 88 clean
images. It confirmed no additional source-text residual in the checks that
could be confidently associated with a region. Thirteen target-text OCR
non-detections were inconclusive, not evidence of correctness. The available
metadata and safe reconstruction path did not support a complete isolated
rerender of all pages, so no before/after rerender result is claimed. The
original artifacts were not changed.

These findings show why residual and candidate status must feed QC rather than
be silently treated as clean success. They do not establish a human-reviewed
error rate, and the historic residual count is not a ceiling on possible
issues.

## Model experiments and decision

The optional NLI trial caught 6 of 11 synthetic drift cases but incorrectly
flagged 5 of 11 faithful cases. Lightweight E5/MiniLM experiments rejected
large portions of faithful holdout pairs; the available holdout was synthetic
and not human gold data. MetricX loaded in a local service smoke test, but its
runtime and memory cost were unsuitable for the desktop path, and a complete
corpus calibration was not completed. None is approved or shipped as a
production semantic judge. Experiment code and service scaffolding were
removed when M18.5 was consolidated.

The accepted offline work is deterministic quantity/magnitude hardening plus
bounded regional retry, re-score, and explicit Review fallback. See
[`TRANSLATION_QUALITY_HARDENING.md`](TRANSLATION_QUALITY_HARDENING.md) for the
current contract and known limitation. Fluent semantic drift that evades
deterministic invariants can still pass automatically; Review/QC remains
necessary.
