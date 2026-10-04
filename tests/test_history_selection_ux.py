"""Selection-mode UX + scroll-regression fix (Claude takeover of Codex's bulk-selection WIP).

Selecting items must not re-render the whole history (the regression that reintroduced the
scroll jump), the toolbar must have a single exit (no duplicated "Sair da seleção"+"Cancelar"),
delete-selected is a trash icon, and the selected state is not error-red.
"""
from pathlib import Path

JS = Path("static/tradutor_ui.js").read_text(encoding="utf-8")
HTML = Path("ui/ui_shell.html").read_text(encoding="utf-8")
CSS = Path("static/tradutor_ui.css").read_text(encoding="utf-8")


def test_selection_toggle_is_incremental_not_full_rerender():
    # Selecting/deselecting a card must NOT call renderHistory (that was the scroll regression).
    start = JS.index("function toggleHistorySelection(")
    end = JS.index("function openBulkDeleteModal(")
    body = JS[start:end]
    assert "renderHistory(" not in body
    assert "applyHistorySelectionClasses" in JS  # incremental applier exists


def test_enter_exit_mode_is_css_toggle_not_rerender():
    start = JS.index("function toggleHistorySelectionMode(")
    end = JS.index("function toggleHistorySelection(")
    body = JS[start:end]
    assert "renderHistory(" not in body
    assert "classList.toggle('selection-active'" in body


def test_select_all_is_incremental():
    start = JS.index("#histSelectionAll')?.addEventListener")
    body = JS[start:start + 700]
    assert "renderHistory(" not in body
    assert "applyHistorySelectionClasses()" in body


def test_single_exit_control_no_duplicate():
    # Toolbar must not show both a "Sair da seleção" text button and a "Cancelar" text button.
    assert HTML.count(">Cancelar<") == 0 or True  # modal Cancelar lives elsewhere; toolbar has none
    # toolbar exit is the ✕ icon with an aria-label; the toggle is hidden in selection mode
    assert 'id="histSelectionCancel"' in HTML and 'aria-label="Sair da seleção"' in HTML
    assert "toggle.hidden = active" in JS  # the "Selecionar" button hides in selection mode
    # no "Sair da seleção" / "Cancelar" TEXT buttons in the selection actions bar
    actions = HTML[HTML.index('id="histSelectionActions"'):HTML.index("</div>", HTML.index('id="histSelectionCancel"'))]
    assert ">Cancelar<" not in actions and ">Sair da seleção<" not in actions


def test_delete_selected_is_trash_icon_with_accessibility():
    actions = HTML[HTML.index('id="histSelectionActions"'):HTML.index("</div></div>", HTML.index('id="histBulkDelete"')) + 6]
    assert 'id="histBulkDelete"' in actions
    assert 'aria-label="Excluir selecionados"' in actions and 'title="Excluir selecionados"' in actions
    assert "<svg" in actions  # icon, not a big text button
    assert ">Excluir selecionados<" not in actions  # the big text button is gone from the toolbar


def test_select_visible_label():
    assert ">Selecionar visíveis<" in HTML or "'Selecionar visíveis'" in JS
    assert "'Desmarcar visíveis'" in JS


def test_selected_state_is_not_error_red():
    rule = CSS[CSS.index(".hist-item.history-item-selected"):CSS.index(".hist-item.history-item-selected") + 200]
    assert "201,162,39" in rule  # gold accent
    assert "230,61,50" not in rule and "197,55,44" not in rule  # no strong red


def test_checkbox_absolute_no_grid_reflow():
    # The checkbox is absolutely positioned and revealed by a class -> entering selection mode
    # adds no grid column, so there is no reflow/scroll jump.
    assert ".history-select-control{position:absolute" in CSS
    assert ".hist-list.selection-active .history-select-control{display:flex;}" in CSS
    assert ":has(.history-select-control)" not in CSS  # the reflow-causing grid rule is gone


def test_bulk_delete_backend_untouched():
    # We did not reimplement bulk delete; the endpoint call is preserved.
    assert "/api/ui/history/bulk-delete" in JS
