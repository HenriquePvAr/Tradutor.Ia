# Repository structure

The canonical Python runtime package is `yomu/`. Root modules are limited to documented
entrypoints and Python startup hooks; runtime/domain implementations live in packages.

```text
yomu/
  auth/                    authentication and licensing
  clean/                   semantic ownership, masks, inpainting, reconstruction
  community/               community APIs, storage, publication
  commands/                user-facing launchers and CLI implementations
  config/                  runtime settings
  core/                    shared domain contracts and utilities
  desktop/                 desktop shell
  download/                download transport, local intake, readers, sources
  export/                  PDF, PNG, PSD, manifests
  ocr/                     OCR engines, scheduling, splitting
  pipeline/                orchestration and pipeline state
  review/                  review contracts, reports, workflows
  runtime/                 jobs, workers, process/runtime services
  security/                URL and security policies
  translation/             language decisions and provider adapters
  ui/                      NiceGUI app, bridge, helpers, history
test_support/               hermetic runtime and synthetic subprocess fixtures
tests/                      Python, frontend, integration and contract tests
tools/                      audits, research, reports, harnesses
scripts/                    explicit operational commands
```

## Where does a new Python module go?

- OCR engine, policy, or page splitting: `yomu/ocr/` (`splitting/` for page splitting).
- CLEAN ownership, masks, or review decisions: `yomu/clean/ownership/`.
- Inpainting or reconstruction: `yomu/clean/inpainting/` or `yomu/clean/reconstruction/`.
- Translation behavior: `yomu/translation/`; provider transport/adapters: `yomu/translation/providers/`.
- Download intake, source identity, or reader adapter: `yomu/download/` under its matching domain.
- Jobs, workers, process policy, or filesystem runtime paths: `yomu/runtime/`.
- UI implementation: `yomu/ui/`; desktop entrypoint implementation: `yomu/desktop/`.
- Tests: `tests/`; test-only helpers: `test_support/`.
- R&D and one-off diagnostics: `tools/research/` or `experiments/<mission>/`, never runtime packages.
- Supported product CLI implementations: `yomu/commands/`; operational scripts: `scripts/`.

Historical command names such as `python app_ui.py`, `python start_tradutor.py`, and
`python run_webtoon.py` remain thin compatibility launchers. Their implementations live
in `yomu/`. Root exceptions and their rationale are declared in
[`config/repository_root_allowlist.json`](../config/repository_root_allowlist.json) and
checked with `python tools/repository/check_root_policy.py`.

See [DEVELOPMENT_WORKFLOW.md](DEVELOPMENT_WORKFLOW.md) for test and packaging commands.
