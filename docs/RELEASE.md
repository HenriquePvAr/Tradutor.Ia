# Release

Este é o documento autoritativo para preparar uma release do Yomu Sekai.

- `PRODUCT_VERSION` (`0.9.0`) é a versão semântica usada pelo updater.
- `BUILD_VERSION` (`0.9.0-beta.32`) identifica o payload de pré-release.
- Ambos são definidos em `app_version.py`.

O pipeline, quality/provenance/PDF e accounting foram validados. Signing, canal do updater,
installer final, E2E em máquina limpa e distribuição controlada permanecem pendentes.
Consulte [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md).
