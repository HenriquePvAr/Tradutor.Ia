import _test_bootstrap  # noqa: F401

from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _css_rule(selector: str) -> str:
    text = (ROOT / "static" / "tradutor_ui.css").read_text(encoding="utf-8")
    start = text.index(f"\n  {selector}{{")
    brace = text.index("{", start)
    depth = 0
    for index in range(brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[brace + 1:index]
    raise AssertionError(f"unterminated CSS rule: {selector}")


def test_active_panel_visibility_is_not_owned_by_css_animation():
    """A background tab can pause animations before the first frame.

    The selected application view must therefore be visible by stateful CSS
    declarations alone, not by waiting for ``panelIn`` to reach its final frame.
    """

    rule = _css_rule(".panel-view.active")
    compact = "".join(rule.split()).lower()
    assert "display:block" in compact
    assert "opacity:1" in compact
    assert "clip-path:inset(0000)" in compact
    assert "animation:" not in compact
