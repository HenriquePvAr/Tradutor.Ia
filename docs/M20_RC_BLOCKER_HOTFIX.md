# M20 RC blocker: translation coverage and CLEAN integrity

## Scope

This hotfix separates three independent outcomes for every region classified as
story text:

1. provider-backed translation coverage;
2. source-text removal for the `CLEAN` artifact; and
3. translated-page reconstruction.

It does not alter YK pricing, settlement, PSD Type Layers, or a source adapter.

## Reservation and retry ordering

A Quality job has one held reservation for its bounded initial batches and
bounded regional retries. A retry that still needs the provider must run before
the durable provider result is settled. After settlement, recovery may replay
only the exact persisted request; a novel regional request is a systemic
`reservation_already_settled` failure, not a bad translation for that region.

The pipeline therefore resolves layout-shortening retries in an in-memory
preflight before settlement. Final artifact rendering never invokes the
provider. Reservation/auth/transport/settlement failures propagate as
job-scoped failures and cannot become a fan-out of source fallbacks.

Regional candidate, OCR, semantic, or typography failures remain reviewable
regional outcomes.

## CLEAN is independent from a target candidate

`CLEAN` represents artwork with source text removed from regions that the
classifier authorizes for translation. It is not evidence that the provider
returned usable PT-BR.

When a target is rejected, the translated page remains fail-closed and routes
to Review. A separate bounded cleanup pass still attempts source removal for
the CLEAN artifact. It uses the existing classifier, OCR ownership, glyph mask,
background analysis, inpainting and residual checks; it does not erase SFX,
preserved names, credits, decorative art, or OCR-unintelligible regions.

Each attempt records `clean_artifact_validation`. A clean artifact is accepted
only when the source-removal evidence is complete. This is distinct from the
post-reconstruction validation, which additionally checks the drawn target and
its allowed geometry.

## Background and polarity

Cleanup remains driven by local evidence rather than fixed page colours:

- uniform light and dark backgrounds use a locally estimated fill;
- colored or textured contexts use the existing local inpainting/reconstruction
  path;
- masks remain glyph-scoped with bounded growth;
- existing border, seam, residual-lettering and visual-bounds guards stay in
  force.

The regression matrix includes synthetic light/dark and colored backgrounds,
textured artwork, source-free CLEAN for an unusable target, multi-batch
reservation behavior, post-settlement recovery, and systemic retry propagation.

## Operational policy

`review_required` remains appropriate for localized, bounded uncertainty. A
reservation, authentication, transport, or settlement contract failure is
systemic and must stop the translation path rather than reporting a mostly
source-language output as a normal completed chapter.

No production rule in this hotfix uses a title, URL, chapter, page number,
region identifier, captured phrase, or sampled RGB value.

## Local-background reconstruction hardening

The clean path records a background profile from the pixels surrounding the
owned glyph mask. For low-variation regions it classifies local evidence as
light, dark, midtone, or coloured and fills from a robust local BGR median. This
covers grey, coloured, and neon enclosures without a product-specific palette.
A directional, low-variation profile is treated as a gradient and reconstructed
with bounded local inpainting instead of being flattened. High-variation artwork
remains on the conservative inpainting/fail-closed path.

The profile also records polarity (`dark_text_on_light_bg` or
`light_text_on_dark_bg`) and validation measures reconstructed pixels against
the local profile. A colour mismatch is a review-visible cleanup failure, not a
successful clean artifact. Synthetic tests cover white, black, grey, coloured,
neon, and gradient backgrounds; no chapter, page, phrase, or fixed-colour rule
was added.
