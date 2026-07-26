# Semantic region taxonomy & linguistic audit (BLOCO 2)

This documents the semantic classification taxonomy and the offline linguistic
audit. Neither modifies PDFs, final pages, revisions or publications, and neither
calls a translation provider.

## Problem

The detection classifier lumps different things into the coarse visual buckets
`decorative` and `sfx`. Anything in those buckets is preserved (never
translated). That silently loses **semantically important text** — a styled
out-of-balloon caption such as `REAL COFFEE.`, or narration mislabeled as
decoration — because it is treated as a non-translatable graphic effect.

## Taxonomy — `region_taxonomy.py`

One source of truth. Categories:

| Group | Categories |
|-------|-----------|
| Preserve | `sfx_preserve`, `credit_preserve`, `watermark_preserve`, `url_preserve`, `proper_name_preserve` |
| Translate | `decorative_semantic_translate`, `title_semantic_translate`, `narration_translate`, `dialogue_translate` |
| Uncertain | `unknown_review_required` |

Predicates: `is_preservable`, `is_translatable`, `needs_human_review`,
`suggested_action`. `TAXONOMY_VERSION` versions the mapping.

### Normalization (`normalize(legacy_label, *, text, preserve_as_name)`)

Fail-closed, evidence-driven, no hardcoded pages/phrases:

1. A proven proper name → `proper_name_preserve`.
2. Text shape wins for preserve cases: a URL → `url_preserve`; credit terms →
   `credit_preserve`; a watermark signature → `watermark_preserve`.
3. Direct legacy labels: `speech`/`thought`/`dialogue` → `dialogue_translate`;
   `narration` → `narration_translate`; `credit`/`watermark`/`url`/`proper_name`
   → their preserve category; `title` → `title_semantic_translate` if semantic.
4. The visual/uncertain buckets (`sfx`, `decorative`, `unknown`, empty) are
   **re-evaluated from the text**: onomatopoeia shape → `sfx_preserve`; real
   semantic content → `decorative_semantic_translate`; otherwise
   `unknown_review_required`.
5. An **unrecognised** legacy label → `unknown_review_required` (never silently
   translated or preserved).

Detection is structural: onomatopoeia is a short token in a lexicon or an
elongated repeat (not merely uppercase/stylised); semantic content needs at
least one real word (has a vowel, length, not a repeat run) that is not
onomatopoeia/URL/credit.

### Legacy compatibility

Every historical label (`speech`, `sfx`, `decorative`, `credit`, `watermark`,
`url`, `proper_name`, `title`, `narration`, `dialogue`, `unknown`, empty) maps
into the taxonomy. Reading old data keeps working; nothing is rewritten. Any
future or unknown value falls closed to `unknown_review_required`.

## Offline audit — `linguistic_audit.py`

Read-only, deterministic. Selects the chapter by **real job/run identity** (via
the job store — never by title, date, glob or a latest pointer) and the revision
by reviewed-PDF name (tie-broken on the recorded `updated_at`). For each region
it records: page, region id, `classification_original` vs
`classification_normalized`, source text, current translation, suggested action,
reason codes, `revision_linked`, `report_only`, cache status, `provider_required`,
confidence and `needs_human_review`. Writes JSON + Markdown to a chosen output
dir; touches nothing else.

Run:

```bash
.venv/Scripts/python.exe linguistic_audit.py .runtime/linguistic-audit
```

It is a **derived view**: a region that flips from preserved to translatable is
flagged `needs_human_review`, not corrected. No translation is applied.

## Scope boundary

BLOCO 2 centralizes the taxonomy and produces the audit. Wiring the taxonomy
into the live classifier's runtime translate/preserve decisions is deferred: it
only takes effect on a re-run, which needs re-translation (provider) and is out
of scope here. The next block reviews the audit, selects pages, corrects them
through the per-page revision UI, and only then produces a new PDF.

## Human review through the UI (BLOCO 3)

The audit is reviewable in the chapter UI without touching any PDF, historical
revision or publication.

- **Registration (`audit_registry.py`)** — the report is stored in an additive
  audit area under `quality_revision/linguistic_audit/<revision_id>/` and indexed
  by a small registry. Resolution is by identity (output dir + revision id) and
  verifies the report hash and taxonomy version, failing closed on tamper,
  traversal or a schema mismatch. Historical manifests are never rewritten.
- **Decisions (`audit_decisions.py`)** — a per-user store of `translate`,
  `preserve`, `ocr_invalid`, `needs_review` and `dismissed`, owner-scoped,
  idempotent (one per user/region/revision), updatable and removable. It reuses
  the local jobs sqlite via one additive table.
- **Bridge + endpoints** — the chapter is selected by real job/run identity and
  its canonical base revision; the audit is resolved or lazily built and
  registered. `/api/ui/audit/review` returns a per-user view (each region
  overlaid with the caller's decision); `/api/ui/audit/decision` and
  `.../decision/delete` record and remove decisions, validating that the region
  belongs to the loaded report (lineage) and that the caller owns the decision.
- **UI** — a data-driven summary (counts come from the API), filters
  (classification, action, needs-human-review, report-only, provider-required,
  cache, page), per-region cards with the full audit fields, the five decision
  actions plus remove, and `OPEN PAGE` / `REVISAR ESTA PÁGINA` that reuse the
  per-page revision panel. The panel restores after F5 via the URL.

Nothing is chapter-specific: pages, ids, phrases and versions are always derived
from the report and the job/run/revision, proven against two unrelated synthetic
chapters. A `provider_required` region is flagged but never triggers a provider
call by being opened; applying an approved correction stays a later, explicitly
authorized step.

## Live taxonomy integration (BLOCO 4)

The audit and the live targeted review used to disagree: `_is_reviewable`
decided from the coarse legacy label alone, so anything bucketed `decorative`
(or sfx/credit/watermark/editorial) was unreviewable even when the audit had
normalized it to `decorative_semantic_translate`.

`resolve_region_policy` in `region_taxonomy.py` is now the single source of
truth. It returns the whole decision for a region:

| field | meaning |
|-------|---------|
| `normalized_classification` / `semantic_role` | what the region is |
| `reviewable` | may enter the targeted review |
| `translatable` / `preservable` | may be translated / must be kept |
| `ocr_retry_allowed` | targeted OCR may retry it |
| `provider_required` | translatable and not answered by cache |
| `needs_human_review` | ambiguous, unreadable, or preserved-but-semantic |
| `suggested_action`, `reason_codes`, `confidence`, `taxonomy_version` | evidence trail |

Categories added in taxonomy v2: `thought_translate`,
`system_message_translate`, `location_translate`, `editorial_translate`,
`logo_preserve`, `branding_preserve` and `ocr_invalid`.

Every consumer reads that policy — `_is_reviewable`, `list_page_regions`,
`search_forgotten_text` and the audit records — so the audit and the live review
can no longer contradict each other. A human decision (`translate`, `preserve`,
`ocr_invalid`, `needs_review`, `dismissed`) overrides the inferred class but
never makes a region auto-apply. The frontend renders the backend's booleans and
normalized class; it does not infer policy from a classification string.

A registered audit built under an older taxonomy version is rebuilt on read;
tamper, traversal and schema breakage still fail closed.

## Human linguistic triage (BLOCO 5)

`linguistic_triage.py` adds three pure layers on top of the taxonomy policy:

- **Per-region linguistic gate**, deliberately separate from the visual gate.
  Checks: residual source language, mixed language, candidate equal to source,
  empty candidate, encoding artefacts, truncation, dropped sentence punctuation,
  semantic inversion and terminology hints; on the preserve side, an altered sfx
  or watermark; and an unreadable source. Status is `passed`, `failed`,
  `needs_review` or `not_applicable`. A clean render never implies a correct
  translation, and neither gate alone approves anything.
- **Explainable triage queue.** Every region carries a weighted score *and* the
  labels that produced it (unreadable source, undetermined class, flagged
  preservable, failed gate, translatable with cache, needs human review,
  provider required). A region already decided by a human drops to the bottom
  but stays visible. Ordering is deterministic.
- **Minimal provider set.** Keeps only regions that genuinely still need a call,
  recording why each other one was excluded (preservable class, unreadable,
  resolved by cache, human decision, not translatable). Estimated requests is
  one per region.

The audit panel exposes these as three modes (list, triage queue, provider set).
Bulk decisions apply one verdict to many regions atomically: the audit hash is
revalidated, regions outside the report and policy-incompatible selections are
rejected, and a failure rolls back. The provider mode ends in a button that only
records a **pending** authorization request — it requires explicit confirmation,
reads no credential and never contacts the provider. Running that set stays a
separate, human-authorized step.

## Final triage before provider authorization (BLOCO 6A)

Two conflations were corrected here, both of which had been quietly distorting
what still needs a provider call:

1. **"A cache entry exists" ≠ "a fix exists" ≠ "must re-ask".**
   `evaluate_cache_proposal` now reports `usable` (a correction that can be
   drafted) and `answered` (the provider already ruled) separately, and refuses
   an answer recorded for a different region. `cache_status` follows *answered*;
   `cache_correction_available` follows *usable*.
2. **An answered region is only resolved if the answer held up.** A cached
   answer that left the region reading like its source, or empty, resolved
   nothing, so it is no longer excluded from the provider set when the
   linguistic gate failed on it.

`ocr_reprocessing_candidates` lists regions whose source text cannot be trusted
— an explicit human `ocr_invalid` verdict, an unreadable class, or a gate that
flagged the source as unreadable. It only *requests* targeted OCR; it never runs
OCR and never proposes a translation.

The practical consequence on a real chapter: a provider set can be dominated by
corrupted OCR reads that merely *look* word-like. Triaging those to
`ocr_invalid` is what legitimately shrinks the set — never dropping items to
make the number smaller.
