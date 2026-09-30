# M18.5 — Translation quality hardening

Status: **PARTIAL_ACCEPTED_LIMITATION**. The offline hardening work is closed
for progression to M19; this is not a claim of perfect or human-equivalent
translation. The final real Quality E2E required by the Quality Freeze remains
a separate owner-authorized gate.

## Implemented safeguards

- Deterministic semantic checks remain the first gate. They cover supported
  quantities and composed magnitudes, negation and other locally provable
  invariants, source echoes, candidate validity, and translatable versus
  preservable classifications.
- Written number phrases are compared by composed value across English and
  Brazilian Portuguese, including numeric digits versus words and supported
  hundred/thousand/million and approximate-magnitude forms. A mismatch uses
  the existing bounded regional retry and the candidate is checked again.
- Unusable source/candidate evidence can request the existing targeted retry.
  A useful candidate that remains uncertain is retained only with
  `review_required`; uncertainty is not reported as a successful semantic
  proof. Retry limits and provider/billing orchestration were not changed.
- Preservable artwork text is not treated as translated prose solely because
  it is not Portuguese. Classification remains the authority for SFX, logos,
  credits, and other non-translatable regions.

No model was selected for production. NLI, E5, MiniLM, and MetricX experiments
were rejected: the tested semantic models had unacceptable false-positive
behavior on the available synthetic calibration, while MetricX was too heavy
and slow for the desktop runtime and did not complete corpus calibration. The
experimental adapters, service, training/benchmark scripts, and related
dependencies were removed. There is no model download, model weight, network
judge, or new heavyweight runtime dependency in this implementation.

## Known limitation

Fluent, plausible translations that do not violate deterministic invariants
can still contain semantic drift that is not detected automatically. Review/QC
remains the final barrier for those cases. The system does not promise perfect
semantic equivalence or automatic detection of every mistranslation.

## Safety and scope

The changes contain no title-, chapter-, page-, region-, character-, or
phrase-specific production rule. Corpus examples belong only in regression
material and diagnostic documentation. No real provider call, YK operation,
remote mutation, PSD contract change, or Type Layer rasterization is part of
this hardening work. M19 is the formal full-regression gate; the real Quality
E2E remains separately required by the Quality Freeze.
