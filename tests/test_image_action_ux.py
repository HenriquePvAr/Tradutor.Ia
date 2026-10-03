from pathlib import Path


ROOT = Path(__file__).resolve().parent


def test_images_mode_has_one_primary_add_action() -> None:
    shell = (ROOT / "ui" / "ui_shell.html").read_text(encoding="utf-8")
    block = shell[shell.index('id="localImagesSourceField"'):shell.index('id="localPageManagerField"')]
    assert 'id="addLocalImagesBtn"' in block
    assert 'Adicionar imagens' in block
    assert 'id="selectLocalImagesBtn"' not in block
    assert 'id="clearLocalImagesBtn"' in block
    assert '>Limpar<' in block


def test_same_add_handler_supports_empty_and_append_states() -> None:
    js = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
    assert "$('#addLocalImagesBtn')?.addEventListener('click', chooseLocalImages)" in js
    assert "for (const item of items)" in js
    assert "appState.localImages.push({mediaId" in js
    assert "addButton.textContent = count ? '+ Adicionar mais' : 'Adicionar imagens'" in js
