"""Contract tests for authentication network isolation and polling lifecycle.

Two defects motivated these:

* the login screen fetched the Supabase SDK from a CDN on every load, because a
  module injected unconditionally into the shell statically imported the SDK
  module - even when the active provider was local_test;
* the pending-preview request ran on every bootstrap refresh regardless of
  authentication, so a logged-out browser produced thousands of 401s.

Both are checked at the source level, which is where the guarantee lives: an
import that exists is fetched, whatever the runtime later decides.
"""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"

# Any origin that is not the loopback the runtime serves from.
REMOTE_IMPORT = re.compile(r"""["'`](https?:)?//(?!127\.0\.0\.1|localhost)[^"'`]+["'`]""")
LOCAL_IMPORT = re.compile(r"""(?:^|\s)(?:import|export)[^;\n]*?from\s+['"](/static/[^'"]+)['"]""")
BARE_IMPORT = re.compile(r"""(?:^|\s)import\s+['"](/static/[^'"]+)['"]""")
DYNAMIC_IMPORT = re.compile(r"""import\(\s*[`'"](/static/[^`'"?]+)""")


# Matches a whole string literal, or a line comment. Order matters: strings are
# consumed first so the "//" inside a URL is never mistaken for a comment - the
# naive line.split("//") version silently truncated
# `from 'https://esm.sh/...'` to `from 'https:` and made this suite pass while
# the CDN import was still there.
_STRING_OR_COMMENT = re.compile(
    r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|//[^\n]*""")


def strip_comments(source: str) -> str:
    """Drops comments so documentation mentioning a URL cannot fail a check."""
    source = re.sub(r"/\*[\s\S]*?\*/", "", source)
    return _STRING_OR_COMMENT.sub(
        lambda m: "" if m.group(0).startswith("//") else m.group(0), source)


def shell_entry_assets() -> list[Path]:
    """The scripts app_ui.py injects into the shell, read from app_ui.py itself."""
    app_ui = (ROOT / "app_ui.py").read_text(encoding="utf-8")
    names = set(re.findall(r"ROOT / \"static\" / \"([A-Za-z0-9_.-]+\.js)\"", app_ui))
    injected = set(re.findall(r"_asset_url\((\w+_ASSET)\)", app_ui))
    for constant in injected:
        match = re.search(
            rf"{constant}\s*=\s*ROOT / \"static\" / \"([A-Za-z0-9_.-]+\.js)\"", app_ui)
        if match:
            names.add(match.group(1))
    return sorted(STATIC / name for name in names if (STATIC / name).exists())


def static_import_graph(entries: list[Path]) -> dict[Path, str]:
    """Every module reachable from the entries through *static* imports.

    Dynamic imports are deliberately excluded: those are the mechanism the
    provider layer uses to keep a provider's code out of the page until the
    backend says that provider is active.
    """
    seen: dict[Path, str] = {}
    queue = list(entries)
    while queue:
        path = queue.pop()
        if path in seen or not path.exists():
            continue
        source = path.read_text(encoding="utf-8")
        seen[path] = source
        code = strip_comments(source)
        for pattern in (LOCAL_IMPORT, BARE_IMPORT):
            for target in pattern.findall(code):
                candidate = ROOT / target.lstrip("/")
                if candidate.exists():
                    queue.append(candidate)
    return seen


class AuthNetworkIsolationTest(unittest.TestCase):
    def test_shell_assets_never_statically_import_a_remote_origin(self):
        """No module the shell loads may pull code from another origin.

        A static import is fetched as soon as the module is parsed, so a remote
        import anywhere in this graph is a network call on every page load,
        before anyone has authenticated and whatever the provider turns out
        to be.
        """
        graph = static_import_graph(shell_entry_assets())
        self.assertTrue(graph, "no shell assets were resolved")
        offenders = []
        for path, source in graph.items():
            code = strip_comments(source)
            for line in code.splitlines():
                if "import" not in line and "from" not in line:
                    continue
                for match in REMOTE_IMPORT.findall(line):
                    offenders.append(f"{path.name}: {line.strip()[:110]}")
        self.assertEqual(
            offenders, [],
            "shell modules must not import from a remote origin:\n" + "\n".join(offenders))

    def test_supabase_sdk_reaches_the_page_only_through_the_provider_gate(self):
        """The SDK module may only be reached by a dynamic, provider-gated import."""
        reachable = sorted(path.name for path in static_import_graph(shell_entry_assets()))
        self.assertNotIn(
            "supabase_auth.js", reachable,
            "supabase_auth.js must not be statically reachable from the shell; "
            "auth_provider.js already imports it dynamically once the backend "
            f"reports the Supabase provider. Reachable: {reachable}")

    def test_provider_choice_comes_from_the_backend_contract(self):
        """Never from the hostname, the port, or the URL."""
        code = strip_comments((STATIC / "auth_provider.js").read_text(encoding="utf-8"))
        self.assertIn("cfg.provider", code)
        gate = re.search(r"async function provider\(\)[\s\S]*?\n}", code)
        self.assertIsNotNone(gate)
        for forbidden in ("location.hostname", "location.port", "location.host",
                          "127.0.0.1", "localhost"):
            self.assertNotIn(forbidden, gate.group(0),
                             f"provider selection must not inspect {forbidden}")


class PendingPreviewPollingTest(unittest.TestCase):
    source = (STATIC / "tradutor_ui.js").read_text(encoding="utf-8")

    def test_pending_previews_are_not_requested_without_authentication(self):
        """The request must be refused inside the function that makes it.

        Guarding only the call site leaves every future caller free to
        reintroduce the logged-out request, which is how thousands of 401s
        appeared in the first place.
        """
        code = strip_comments(self.source)
        body = re.search(
            r"async function loadPendingHumanPreviews\(\)\s*\{([\s\S]*?)\n  \}", code)
        self.assertIsNotNone(body, "loadPendingHumanPreviews not found")
        guard_region = body.group(1).split("api(", 1)[0]
        self.assertRegex(
            guard_region, r"(?i)authenticated|auth[A-Za-z]*state",
            "loadPendingHumanPreviews must return before calling the API when "
            "there is no authenticated session")
        self.assertIn("return", guard_region,
                      "the guard must return, not merely branch")

    def test_pending_preview_call_site_is_inside_the_authenticated_branch(self):
        code = strip_comments(self.source)
        # refreshBootstrap is a single-flight wrapper; the work lives in
        # runBootstrapRefresh.
        refresh = re.search(r"async function runBootstrapRefresh\(\)([\s\S]*?)\n  \}", code)
        self.assertIsNotNone(refresh, "runBootstrapRefresh not found")
        block = re.search(r"if \(authenticated\) \{([\s\S]*?)\n      \}", refresh.group(1))
        self.assertIsNotNone(block, "the authenticated branch was not found")
        self.assertIn("loadPendingHumanPreviews", block.group(1))

    def test_logging_out_clears_the_pending_preview_state(self):
        """A stale list must not survive the session that produced it."""
        code = strip_comments(self.source)
        self.assertRegex(
            code, r"clearPendingHumanPreviews|pendingHumanPreviews\s*=\s*\{\s*items:\s*\[\]",
            "signing out must reset the cached preview list")


class BootstrapRefreshLifecycleTest(unittest.TestCase):
    """A signed-out shell issued ~164 bootstrap requests a second.

    refreshBootstrap renders on its way out, rendering compares the history
    revision, and the comparison treated "not reported" as "changed" - so every
    render asked for another bootstrap, forever.
    """

    source = (STATIC / "tradutor_ui.js").read_text(encoding="utf-8")

    def test_refresh_bootstrap_is_single_flight(self):
        code = strip_comments(self.source)
        body = re.search(r"async function refreshBootstrap\(\)\s*\{([\s\S]*?)\n  \}", code)
        self.assertIsNotNone(body, "refreshBootstrap not found")
        self.assertRegex(
            body.group(1), r"(?i)inflight|pending|singleflight",
            "refreshBootstrap must reuse an in-flight run instead of starting "
            "a second one, or any trigger during its own render becomes a loop")

    def test_history_revision_absence_is_not_treated_as_change(self):
        code = strip_comments(self.source)
        trigger = re.search(
            r"if \([^\n]*history_revision[\s\S]{0,220}?refreshBootstrap\(\)", code)
        self.assertIsNotNone(trigger, "the revision trigger was not found")
        self.assertIn("isReportedRevision", trigger.group(0),
                      "only compare revisions both sides actually reported")

    def test_reported_zero_revision_is_stored(self):
        """`x || fallback` silently discards a reported 0 and keeps a stale value."""
        code = strip_comments(self.source)
        self.assertNotRegex(
            code, r"appState\.historyRevision\s*=\s*data\.history_revision\s*\|\|",
            "a reported 0 is a revision, not an absence")

    def test_state_polling_requires_authentication(self):
        code = strip_comments(self.source)
        body = re.search(r"async function pollState\(\)\s*\{([\s\S]*?)\n  \}", code)
        self.assertIsNotNone(body, "pollState not found")
        guard = body.group(1).split("api(", 1)[0]
        self.assertRegex(
            guard, r"(?i)authenticated",
            "/api/ui/state answers 401 without a session; it must not be polled "
            "from the login screen")
        self.assertIn("return", guard, "the guard must return, not merely branch")


if __name__ == "__main__":
    unittest.main()