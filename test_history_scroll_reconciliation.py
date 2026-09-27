"""Real-DOM regression for translated-history incremental rendering.

The test uses an already-installed Chromium-family browser only; it never navigates
to a network URL and skips cleanly when no browser is available in a test runner.
"""

from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - optional browser test dependency
    sync_playwright = None


ROOT = Path(__file__).resolve().parent
UI_JS = ROOT / "static" / "tradutor_ui.js"


def _browser_executable() -> str | None:
    candidates = [
        shutil.which("msedge"),
        shutil.which("chrome"),
        shutil.which("brave"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
    ]
    return next((candidate for candidate in candidates if candidate and Path(candidate).is_file()), None)


def _extract_function(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.index(marker)
    brace = source.index("{", start)
    depth = 0
    quote = ""
    escaped = False
    template = False
    # Count braces while ignoring ordinary quoted strings and comments. Template
    # substitutions are balanced by the same brace counter and contain no braces
    # that terminate the outer function.
    for index in range(brace, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "'\"`":
            quote = char
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index)
            if newline < 0:
                break
            # Skip comment characters without altering the outer scan index.
            comment_end = newline
            if depth == 0:
                break
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"Could not extract {name}")


def _history_helpers() -> str:
    source = UI_JS.read_text(encoding="utf-8")
    names = [
        "historyRenderKey",
        "historyNodesCompatible",
        "patchHistoryNode",
        "syncHistoryChildren",
        "getHistoryScrollOwner",
    ]
    return "\n".join(_extract_function(source, name) for name in names)


@unittest.skipUnless(sync_playwright and _browser_executable(), "installed Chromium browser required")
class HistoryScrollRealDomTests(unittest.TestCase):
    def test_36_chapters_patch_without_replacing_scroll_owner_or_unchanged_cards(self):
        records = []
        for group in range(3):
            records.append(f'<section class="community-folder" data-folder="series-{group}"><button class="cf-header">Series {group}</button><div class="cf-body">')
            for chapter in range(12):
                key = group * 12 + chapter
                records.append(
                    f'<article class="hist-item" data-id="chapter-{key}">'
                    f'<span class="hm-title">Chapter {key}</span>'
                    f'<span class="hm-sub" data-field="meta">stable</span></article>'
                )
            records.append("</div></section>")
        html = """<!doctype html><html><head><style>
          html,body{height:100%;margin:0;overflow:hidden} main{height:480px;overflow-y:auto}
          #histList{display:flex;flex-direction:column;gap:10px}
          .community-folder{min-height:500px}.hist-item{height:45px}
        </style></head><body><main><div id="histList">""" + "".join(records) + """</div></main></body></html>"""

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=_browser_executable(),
                headless=True,
                args=["--disable-gpu", "--no-first-run", "--no-default-browser-check"],
            )
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.set_content(html)
                page.add_script_tag(content=_history_helpers())
                result = page.evaluate("""async () => {
                  const list = document.querySelector('#histList');
                  const main = document.querySelector('main');
                  const originalCards = [...list.querySelectorAll('.hist-item')];
                  main.scrollTop = 1400;
                  const before = main.scrollTop;
                  const desired = document.createElement('div');
                  desired.innerHTML = list.innerHTML;
                  desired.querySelector('[data-id="chapter-17"] [data-field="meta"]').textContent = 'updated status';
                  const changes = {count: 0};
                  syncHistoryChildren(list, desired, changes);
                  await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
                  return {
                    owner: getHistoryScrollOwner(list).tagName.toLowerCase(),
                    before,
                    after: main.scrollTop,
                    chapterCount: list.querySelectorAll('.hist-item').length,
                    preservedNodes: originalCards.filter((card, index) => card === list.querySelectorAll('.hist-item')[index]).length,
                    updatedText: list.querySelector('[data-id="chapter-17"] [data-field="meta"]').textContent,
                    changedNodes: changes.count,
                  };
                }""")
                self.assertEqual("main", result["owner"])
                self.assertGreater(result["before"], 1000)
                self.assertEqual(result["before"], result["after"])
                self.assertEqual(36, result["chapterCount"])
                self.assertEqual(36, result["preservedNodes"])
                self.assertEqual("updated status", result["updatedText"])
                self.assertGreater(result["changedNodes"], 0)
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
