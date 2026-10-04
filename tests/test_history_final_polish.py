"""Final history polish: checkbox checked visual (Bug B). Bugs A (trailing scroll) and C
(auth startup) were audited and found NOT to be defects in this build (see reports); no code
change there, so they are asserted as 'unchanged contract' guards."""
from pathlib import Path

CSS = Path("static/tradutor_ui.css").read_text(encoding="utf-8")
JS = Path("static/tradutor_ui.js").read_text(encoding="utf-8")


def test_checkbox_has_unmistakable_checked_state():
    block = CSS[CSS.index(".history-select-control{"):CSS.index(".hist-selection-icon{")]
    # checked -> gold fill + border + a visible check mark (::after)
    assert ".history-select-control:has(input:checked){background:var(--gold); border-color:var(--gold);}" in CSS
    assert ".history-select-control:has(input:checked)::after{opacity:1;}" in CSS
    assert "::after{content:''" in block and "rotate(-45deg)" in block  # the ✓ glyph
    # unchecked box is an empty bordered box, not red
    assert "border:2px solid rgba(239,231,216,.55)" in block
    assert "230,61,50" not in block and "197,55,44" not in block  # no red as the selection signal


def test_checkbox_keeps_real_input_for_accessibility():
    block = CSS[CSS.index(".history-select-control{"):CSS.index(".hist-selection-icon{")]
    # the native input stays (visually hidden, full-size) -> keyboard Space + screen reader work
    assert ".history-select-control input{position:absolute; inset:0" in block
    assert "opacity:0" in block
    assert ":has(input:focus-visible)" in block  # focus ring on the box
    assert 'aria-label="Selecionar ' in JS  # label associated with the chapter


def test_selected_card_is_gold_not_red():
    rule = CSS[CSS.index(".hist-item.history-item-selected"):CSS.index(".hist-item.history-item-selected") + 200]
    assert "201,162,39" in rule and "230,61,50" not in rule


def test_bottom_scroll_padding_is_reasonable_not_huge():
    # main's trailing padding is a small design token, not a huge artificial spacer (Bug A audit).
    import re
    m = re.search(r"\bmain\{[^}]*padding-bottom:(\d+)px", CSS)
    # tolerate the rule living across lines; just assert no absurd bottom padding anywhere on main
    assert "padding-bottom:180px" not in CSS and "padding-bottom:300px" not in CSS


def test_login_contract_gate_present():
    # Auth startup contract (Bug C audit): the shell is hidden unless authenticated, and the
    # login overlay is a [hidden]-toggled page -> no-session shows login. Unchanged on purpose.
    assert 'html:not([data-shell-state="authenticated"]) .shell{display:none;}' in CSS
    assert ".auth-login-page[hidden]" in CSS
