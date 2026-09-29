# PSD editor compatibility matrix — Mission 15

**Status: PASS (owner-reported physical acceptance).** Structural validation
passed on 2026-09-29. The physical results below were supplied by the owner
after the original audit commit; no screenshots or additional observations were
created by this audit.

## Scope and implementation

The production artifact `EDITABLE_PSD` is written by
`editable_psd_writer.write_editable_psd` (`editable_psd_writer.py`). It creates
`ORIGINAL/ORIGINAL_ART`, `CLEAN/CLEAN_ARTWORK`, and `TEXT` groups. Text layers
carry Photoshop `TySh` / `TypeToolObjectSetting` data. `professional_psd.py` is
the separate legacy raster-per-region PSD path and is not the writer for the
`EDITABLE_PSD` artifact. No writer or production code was changed for this audit.

The raster preview appearance of each text region is stored in a pixel layer
alongside its TypeTool data; structural recognition by `psd-tools` is evidence
of a native type record, but only Photoshop/Photopea can establish actual
editing and round-trip behavior. The writer emits `FauxBold`/`FauxItalic`,
alignment, font family, size and fill color. The current writer does not emit a
stroke effect; this was not changed by M15.

## Synthetic fixture set

Generated outside the repository at
`%LOCALAPPDATA%\Temp\yomu-m15-psd-fixtures\`:

| Fixture | Coverage | Structural result |
|---|---|---|
| `A_simple.psd` | Centered PT-BR balloon text | 1 native Type Layer |
| `B_multiple.psd` | Balloon, narration, caption, title and footnote | 5 native Type Layers; expected names |
| `C_style.psd` | Faux bold/italic, color, size and left/center/right alignment | 3 native Type Layers |
| `D_utf8.psd` | Accents, ã/õ/ç, em dash, curly quotes, punctuation, R$ and Japanese sample | Exact text recovered |
| `E_missing_font.psd` | Deliberately nonexistent font family | Type Layer and text retained structurally |

All five files had a valid `8BPS` header, 1000×700 canvas, the three required
groups, raster ORIGINAL/CLEAN artwork, and native TypeTool records readable as
`kind == "type"`. The CLEAN and ORIGINAL pixel layers were present. These checks
used `psd-tools==1.10.9`, independent of the editor applications but also the
project's PSD parser; they are structural, not a second editor implementation.

## Physical application matrix

| Operation | Photoshop 2025 | Photopea |
|---|---|---|
| Open / groups / recognized Type Layers | PASS (owner-reported) | PASS (owner-reported) |
| Edit text | PASS (owner-reported) | PASS (owner-reported) |
| Change font / size | PASS (owner-reported) | PASS (owner-reported) |
| Alignment | PASS (owner-reported) | NOT REPORTED |
| Move / hide-show | PASS (owner-reported) | PASS (owner-reported hide/show; move not reported) |
| Save / close / reopen / editability retained | PASS (owner-reported) | PASS (owner-reported) |
| Export | PASS (owner-reported) | PASS (owner-reported) |
| Missing-font warning/substitution retains Type Layer | PASS (owner-reported) | PASS (owner-reported) |
| Cross-editor round-trip | NOT RUN | NOT RUN |

Photoshop product version reported: **2025**. Photopea test date: after the
M15 commit, as reported by the owner; exact calendar date/version was not
provided. The owner also reported UTF-8 PT-BR displayed correctly for the
characters supported by the selected font. A Type Layer was rasterized
manually as an experiment; this was not attributed to Yomu and does not count
as automatic rasterization or loss in the original file. The supplied evidence
does not establish a direct Photoshop→Photopea→Photoshop round-trip, nor does
it separately report every operation for every synthetic fixture; those are
not claimed here.

## Owner physical acceptance checklist

The owner-reported physical checks satisfy the stated M15 editor-compatibility
gate. For stronger per-fixture or cross-editor evidence, use the five synthetic
fixtures above; do not use or overwrite user PSDs. On B/C, edit with the Type
tool, change font and size, test alignment, move a layer, toggle visibility,
save a copy, close, reopen and edit again; export PNG. On E, record the
missing-font warning and confirm the Type Layer and text remain after
save/reopen. A direct cross-editor round-trip remains NOT RUN. Any corruption
or Type Layer loss is a hard failure; a raster preview alone is not proof of
editability.

## Automated checks

M15 focused test selection: **183 passed, 0 failed**, plus **6 subtests passed**.
The test selection covered the editable PSD writer, artifact contract and
persistence, PSD output, review manifest/Professional PSD integration,
typography propagation, artifact immutability, selective reconstruction, and
local benchmark pipeline integration. No skips or xfails were reported in
these selections. No full project suite was run.

DeepL calls: 0. YK changed: no. Remote Supabase changed: no.
