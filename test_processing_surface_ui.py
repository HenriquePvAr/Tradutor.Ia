"""The renderer draws the view model and decides nothing.

There is no jsdom in this project, so these run the module in node against a
minimal DOM stub that records what was built. The stub deliberately has no
innerHTML setter: if the renderer ever tried to assign markup from a backend
value, the test run would fail with a TypeError rather than pass quietly.

Nothing here names a chapter, a URL, an episode, an owner or a job id.
"""

import _test_bootstrap  # noqa: F401

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOADING_VIEW = ROOT / "static" / "loading_view.js"
SURFACE = ROOT / "static" / "processing_surface.js"
CSS = ROOT / "static" / "loading_surface.css"
NODE = shutil.which("node")

# A DOM small enough to read and complete enough to render into. No innerHTML.
DOM_STUB = r"""
function mkNode(tag, ns) {
  const node = {
    tagName: String(tag).toLowerCase(), ns: ns || null,
    children: [], attrs: {}, _text: '', dataset: {},
    style: { _p: {}, setProperty(k, v){ this._p[String(k)] = String(v); } }, type: '',
    classList: {
      _set: new Set(),
      add(...v){ v.forEach(x=>this._set.add(x)); node.attrs.class = [...this._set].join(' '); },
      contains(v){ return this._set.has(v); }
    },
    get childNodes(){ return node.children; },
    get className(){ return node.attrs.class || ''; },
    set className(v){ node.attrs.class = String(v); String(v).split(/\s+/).filter(Boolean).forEach(c=>node.classList._set.add(c)); },
    get textContent(){ return node._text || node.children.map(c=>c.textContent).join(''); },
    set textContent(v){ node._text = String(v); node.children = []; },
    setAttribute(k, v){ node.attrs[String(k)] = String(v); },
    getAttribute(k){ return Object.prototype.hasOwnProperty.call(node.attrs, k) ? node.attrs[k] : null; },
    appendChild(child){ node.children.push(child); return child; },
    replaceChildren(...kids){
      node.children = [];
      kids.forEach(k => { if (k && k.__fragment) node.children.push(...k.children); else if (k) node.children.push(k); });
    }
  };
  return node;
}
global.document = {
  createElement: (tag) => mkNode(tag, null),
  createElementNS: (ns, tag) => mkNode(tag, ns),
  createDocumentFragment: () => { const f = mkNode('#fragment'); f.__fragment = true; return f; }
};
function serialise(node, depth) {
  const pad = '';
  const cls = node.attrs.class ? '.' + node.attrs.class.split(/\s+/).join('.') : '';
  const data = Object.keys(node.dataset).map(k => `[data-${k}=${node.dataset[k]}]`).join('');
  const attrs = Object.keys(node.attrs).filter(k => k !== 'class')
    .map(k => `[${k}=${node.attrs[k]}]`).join('');
  const own = node._text ? ` "${node._text}"` : '';
  let out = `${pad}${node.tagName}${cls}${data}${attrs}${own}\n`;
  node.children.forEach(c => { out += serialise(c, depth + 1); });
  return out;
}
"""


def render(state: dict, extra_view: dict | None = None):
    """Map a job state, render it, and return a description of the DOM."""
    patch = json.dumps(extra_view or {})
    script = (
        DOM_STUB
        + f"const L = require({json.dumps(str(LOADING_VIEW))});\n"
        + f"const S = require({json.dumps(str(SURFACE))});\n"
        + f"const view = Object.assign(L.mapJobStateToLoadingView({json.dumps(state)}), {patch});\n"
        + "const root = document.createElement('div');\n"
        + "S.renderProcessingSurface(root, view);\n"
        + "process.stdout.write(JSON.stringify({tree: serialise(root, 0), "
        + "text: root.textContent, mode: root.dataset.lsMode, tone: root.dataset.lsTone}));\n"
    )
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise AssertionError(f"node failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def code_of(path: Path) -> str:
    """Source with comments removed, so an assertion is about code.

    A rule named in a comment that explains why it is not used would otherwise
    fail its own test. The SVG namespace is also dropped: createElementNS
    requires that exact W3C constant, and it is not a network address.
    """
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(?m)^\s*//.*$", "", text)
    return text.replace("http://www.w3.org/2000/svg", "SVG_NAMESPACE")


RUNNING = {"status": "running", "stage": "ocr"}


@unittest.skipUnless(NODE, "node is required to execute the renderer")
class TwoModesRenderDifferently(unittest.TestCase):
    def test_bootstrap_is_compact(self):
        out = render({"mode": "bootstrap", "status": "running", "stage": "session"})
        self.assertEqual(out["mode"], "bootstrap")
        # No preview, no banner, no event log in bootstrap.
        self.assertNotIn("ls-preview", out["tree"])
        self.assertNotIn("ls-banner", out["tree"])
        self.assertNotIn("ls-events", out["tree"])

    def test_pipeline_has_the_full_composition(self):
        out = render(RUNNING)
        self.assertEqual(out["mode"], "pipeline")
        for marker in ("ls-head", "ls-preview", "ls-progress", "ls-activity",
                       "ls-band", "ls-banner"):
            self.assertIn(marker, out["tree"], marker)

    def test_bootstrap_never_shows_pipeline_stages_it_is_not_running(self):
        out = render({"mode": "bootstrap", "status": "running", "stage": "session"})
        for word in ("OCR", "Tradução", "PDF"):
            self.assertNotIn(word, out["text"], word)


@unittest.skipUnless(NODE, "node is required to execute the renderer")
class ProgressIsDrawnOnlyWhenReal(unittest.TestCase):
    def test_indeterminate_has_no_aria_value_and_no_number(self):
        out = render(RUNNING)
        self.assertIn("is-indeterminate", out["tree"])
        self.assertNotIn("aria-valuenow", out["tree"])
        self.assertNotIn("role=progressbar", out["tree"])
        self.assertNotIn("0%", out["text"])
        self.assertIn("Preparando", out["text"])

    def test_determinate_exposes_the_real_value(self):
        out = render({**RUNNING, "progress": {"completed_pages": 8, "total_pages": 12}})
        self.assertIn("[role=progressbar]", out["tree"])
        self.assertIn("[aria-valuenow=67]", out["tree"])
        self.assertIn("[aria-valuemin=0]", out["tree"])
        self.assertIn("[aria-valuemax=100]", out["tree"])
        self.assertIn("8 de 12", out["text"])

    def test_null_progress_never_renders_a_zero(self):
        out = render({**RUNNING, "progress": {"fraction": None, "total_pages": 0}})
        self.assertNotIn("aria-valuenow", out["tree"])
        self.assertNotIn("0%", out["text"])

    def test_a_duration_appears_only_when_the_view_model_has_one(self):
        without = render(RUNNING)
        self.assertNotIn("ls-duration", without["tree"])
        with_duration = render({**RUNNING, "started_at": "2026-01-01T10:00:00Z",
                                "updated_at": "2026-01-01T10:00:09Z"})
        self.assertIn("ls-duration", with_duration["tree"])
        self.assertIn("9s", with_duration["text"])


@unittest.skipUnless(NODE, "node is required to execute the renderer")
class TerminalStatesRenderTheirOwnPanel(unittest.TestCase):
    def test_review_required_is_not_a_failure(self):
        out = render({"status": "review_required", "stage": "quality_review",
                      "pending_review_count": 3})
        self.assertEqual(out["tone"], "review")
        self.assertIn("ls-terminal", out["tree"])
        self.assertIn("[role=status]", out["tree"])
        self.assertNotIn("[role=alert]", out["tree"])
        self.assertIn("3 item(ns) para revisar", out["text"])

    def test_finished_shows_success_and_no_result_button_without_one(self):
        out = render({"status": "finished", "stage": "pdf"})
        self.assertEqual(out["tone"], "success")
        self.assertNotIn("open-result", out["tree"])
        self.assertNotIn("open-pdf", out["tree"])

    def test_finished_offers_a_result_only_when_declared(self):
        out = render({"status": "finished", "stage": "pdf",
                      "result_available": True, "pdf_available": True})
        self.assertIn("[data-lsAction=open-result]", out["tree"].replace("lsAction", "lsAction"))
        self.assertIn("open-pdf", out["tree"])

    def test_failure_is_an_alert_with_its_reason(self):
        out = render({"status": "failed", "stage": "source_analysis",
                      "reason_code": "source_not_ready",
                      "message": "A análise não avançou."})
        self.assertIn("[role=alert]", out["tree"])
        self.assertIn("source_not_ready", out["text"])
        self.assertIn("A análise não avançou.", out["text"])

    def test_retry_appears_only_when_permitted(self):
        refused = render({"status": "failed", "stage": "ocr"})
        self.assertNotIn("retry", refused["tree"])
        self.assertIn("não está disponível", refused["text"])
        allowed = render({"status": "failed", "stage": "ocr", "retry_available": True})
        self.assertIn("retry", allowed["tree"])

    def test_cancelled_is_neutral(self):
        out = render({"status": "cancelled", "stage": "ocr"})
        self.assertEqual(out["tone"], "neutral")
        self.assertNotIn("[role=alert]", out["tree"])


@unittest.skipUnless(NODE, "node is required to execute the renderer")
class GroupsAndEventsFollowTheViewModel(unittest.TestCase):
    def test_groups_render_in_the_view_model_order(self):
        out = render(RUNNING)
        labels = re.findall(r"ls-band-label[^\"]*\"([^\"]+)\"", out["tree"])
        self.assertEqual(labels, ["Preparação", "Páginas", "OCR", "Tradução",
                                  "Reconstrução", "PDF", "Revisão"])

    def test_each_group_state_is_carried_in_words(self):
        out = render(RUNNING)
        for word in ("concluído", "em andamento", "aguardando"):
            self.assertIn(word, out["text"], word)

    def test_only_sanitised_events_reach_the_dom(self):
        out = render({**RUNNING, "events": [
            {"at": "2026-01-01T10:00:00Z", "stage": "ocr", "message": "OCR concluído"},
            {"message": "Authorization: Bearer abc"},
            {"message": "Traceback (most recent call last)"},
            {"message": "C:\\Users\\alguem\\.env"},
        ]})
        self.assertIn("OCR concluído", out["text"])
        for secret in ("Authorization", "Bearer", "Traceback", "C:\\Users", ".env"):
            self.assertNotIn(secret, out["text"], secret)

    def test_no_events_means_no_log_section(self):
        self.assertNotIn("ls-events", render(RUNNING)["tree"])


@unittest.skipUnless(NODE, "node is required to execute the renderer")
class AccessibilityStructure(unittest.TestCase):
    def test_the_progress_panel_is_a_live_status(self):
        out = render(RUNNING)
        self.assertIn("[role=status]", out["tree"])
        self.assertIn("[aria-live=polite]", out["tree"])

    def test_decorative_nodes_are_hidden_from_assistive_tech(self):
        out = render(RUNNING)
        hidden = out["tree"].count("[aria-hidden=true]")
        self.assertGreater(hidden, 5, "illustration and markers must be hidden")

    def test_svg_illustration_is_never_focusable(self):
        out = render(RUNNING)
        # No tabindex is introduced anywhere by the renderer.
        self.assertNotIn("tabindex", out["tree"])

    def test_actions_are_real_buttons(self):
        out = render({"status": "failed", "stage": "ocr", "retry_available": True})
        self.assertIn("button.ls-action", out["tree"])


@unittest.skipUnless(NODE, "node is required to execute the renderer")
class UnknownStatesDoNotBreakTheSurface(unittest.TestCase):
    def test_an_unknown_stage_still_renders_the_whole_surface(self):
        out = render({"status": "running", "stage": "brand_new_stage"})
        for marker in ("ls-head", "ls-progress", "ls-band"):
            self.assertIn(marker, out["tree"], marker)
        self.assertIn("Brand new stage", out["text"])

    def test_an_empty_state_renders_without_throwing(self):
        out = render({})
        self.assertIn("ls-head", out["tree"])


class RendererDecidesNothing(unittest.TestCase):
    """A renderer that computes state would be a second source of truth."""

    def test_the_renderer_never_assigns_innerhtml(self):
        self.assertNotIn("innerHTML", code_of(SURFACE))

    def test_the_renderer_does_not_recompute_the_view_model(self):
        text = code_of(SURFACE)
        for forbidden in ("resolveProgress", "resolveDuration", "sanitiseEvents",
                          "resolveGroups", "mapJobStateToLoadingView",
                          "elapsed_seconds", "eta_seconds", "completed_pages",
                          "total_pages", "reason_code"):
            self.assertNotIn(forbidden, text, f"renderer must not touch {forbidden}")

    def test_pipeline_presentation_is_not_reintroduced(self):
        self.assertFalse((ROOT / "static" / "pipeline_presentation.js").exists())
        for name in ("processing_surface.js", "loading_view.js"):
            text = (ROOT / "static" / name).read_text(encoding="utf-8")
            self.assertNotIn("pipeline_presentation", text, name)

    def test_no_percentage_literal_is_used_as_a_fallback(self):
        text = code_of(SURFACE)
        self.assertIsNone(re.search(r"width\s*=\s*['\"]\d+%", text))

    def test_the_renderer_carries_no_concrete_data(self):
        text = code_of(SURFACE)
        for needle in ("the-extras-academy", "episode-105", "title_no=", "episode_no=",
                       "REAL COFFEE", "shadow_slave", "http://", "https://",
                       "C:\\Users", "job_id", "owner_id", "run_id"):
            self.assertNotIn(needle, text, needle)


class StylesheetCoversLayoutAndMotion(unittest.TestCase):
    def test_the_stylesheet_exists(self):
        self.assertTrue(CSS.exists(), "loading_surface.css must ship with the renderer")

    def test_reduced_motion_is_honoured(self):
        text = CSS.read_text(encoding="utf-8")
        self.assertIn("prefers-reduced-motion: reduce", text)
        block = text[text.index("prefers-reduced-motion: reduce"):]
        self.assertIn("animation: none", block.replace("animation:none", "animation: none"))

    def test_every_required_breakpoint_is_present(self):
        text = CSS.read_text(encoding="utf-8")
        for width in ("1366px", "1024px", "768px", "430px"):
            self.assertIn(width, text, width)

    def test_mobile_collapses_to_one_column(self):
        text = CSS.read_text(encoding="utf-8")
        self.assertIn("grid-template-columns: 1fr", text.replace("grid-template-columns:1fr",
                                                                "grid-template-columns: 1fr"))

    def test_state_tokens_exist(self):
        text = CSS.read_text(encoding="utf-8")
        for token in ("--ls-success", "--ls-warning", "--ls-danger", "--ls-accent"):
            self.assertIn(token, text, token)


if __name__ == "__main__":
    unittest.main()


class IntegrationIsWiredThroughTheViewModel(unittest.TestCase):
    """State reaches the DOM only by way of the canonical mapper."""

    BUNDLE = ROOT / "static" / "tradutor_ui.js"
    SHELL = ROOT / "ui" / "ui_shell.html"
    APP = ROOT / "app_ui.py"

    def test_the_bundle_maps_before_it_renders(self):
        text = code_of(self.BUNDLE)
        self.assertIn("mapJobStateToLoadingView", text)
        self.assertIn("renderProcessingSurface", text)
        self.assertLess(text.index("mapJobStateToLoadingView"),
                        text.index("surface.renderProcessingSurface"))

    def test_the_surface_has_a_container(self):
        self.assertIn('id="loadingSurface"', self.SHELL.read_text(encoding="utf-8"))

    def test_assets_load_view_then_renderer_then_bundle(self):
        text = self.APP.read_text(encoding="utf-8")
        for asset in ("loading_view.js", "processing_surface.js", "loading_surface.css"):
            self.assertIn(asset, text, asset)
        order = [text.index("LOADING_VIEW_ASSET)}\" defer"),
                 text.index("PROCESSING_SURFACE_ASSET)}\" defer"),
                 text.index("TRADUTOR_UI_ASSET)}\" defer")]
        self.assertEqual(order, sorted(order), "deferred order must be view, renderer, bundle")

    def test_the_integration_point_forwards_and_does_not_decide(self):
        text = code_of(self.BUNDLE)
        start = text.index("function renderLoadingSurface")
        body = text[start:text.index("function renderProgress", start)]
        # Forwarding only: no arithmetic on progress, no percentage, no clocks.
        for forbidden in ("Math.round", "Math.min", "Math.max", "/ 100", "* 100",
                          "Date.now", "setTimeout", "setInterval"):
            self.assertNotIn(forbidden, body, f"integration must not compute {forbidden}")

    def test_local_test_is_passed_through_not_sniffed(self):
        text = code_of(self.BUNDLE)
        start = text.index("function renderLoadingSurface")
        body = text[start:text.index("function renderProgress", start)]
        self.assertIn("local_test", body)
        # Never inferred from where the page happens to be served.
        for sniff in ("location.hostname", "location.port", "127.0.0.1", "localhost"):
            self.assertNotIn(sniff, body, sniff)
