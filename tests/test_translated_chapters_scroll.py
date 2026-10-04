"""Translated-chapters (history) scroll-state + layout fix.

Root cause of the owner-reported "scroll volta/salta": commitHistoryMarkup re-restored the
captured scrollTop on the NEXT animation frame, overriding a user scroll that happened in
between -- so during 850ms polling the view fought the user. The fix: restore only to
compensate a real layout shift, and never when the user scrolled after the capture.
"""
from pathlib import Path

JS = Path("static/tradutor_ui.js").read_text(encoding="utf-8")
CSS = Path("static/tradutor_ui.css").read_text(encoding="utf-8")


def test_user_scroll_is_tracked():
    assert "lastUserHistoryScrollAt" in JS
    assert "function markUserHistoryScroll" in JS
    for ev in ("'wheel'", "'touchmove'", "'keydown'"):
        assert ev in JS
    # the tracker only arms while the history view is active
    assert "$('#view-hist')?.classList.contains('active')) lastUserHistoryScrollAt = performance.now()" in JS


def test_refresh_restore_does_not_fight_the_user():
    # The rAF restore is guarded: skip when the user scrolled after capture, and only act on a
    # real shift. This is what stops the poll from yanking the user back.
    commit = JS[JS.index("function commitHistoryMarkup"):JS.index("function renderHistory(")]
    assert "const startedAt = performance.now();" in commit
    assert "if (lastUserHistoryScrollAt > startedAt) return;" in commit
    assert "if (owner.scrollTop !== before.scroll_top) restoreHistoryScroll" in commit
    # the old unconditional double-restore is gone
    assert "restoreHistoryScroll(owner, before.scroll_top);\n      window.requestAnimationFrame(() => restoreHistoryScroll(owner, before.scroll_top));" not in JS


def test_polling_still_renders_history():
    # We did not remove polling/refresh -- renderHistory is still driven by the refresh path.
    assert "renderHistory('bootstrap_refresh')" in JS
    assert "commitHistoryMarkup" in JS


def test_history_layout_handles_long_paths_and_timestamps():
    assert ".hist-meta{min-width:0;}" in CSS
    assert "small.rg-path" in CSS and "text-overflow:ellipsis" in CSS[CSS.index("small.rg-path"):CSS.index("small.rg-path") + 200]
    # generation path carries a full-path tooltip, shown ellipsized
    assert 'class="rg-path" title="${escapeAttr(gen.output_path)}"' in JS


def test_output_presets_ui_not_reopened():
    # This mission must not touch the approved preset selector.
    assert 'id="outputPlanField"' in Path("ui/ui_shell.html").read_text(encoding="utf-8")
    assert ">Completo<" in Path("ui/ui_shell.html").read_text(encoding="utf-8")
